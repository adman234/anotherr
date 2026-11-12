"""One executor for every action that edits track headers: retag_audio, set_default_audio, set_default_subtitle.

An action's payload lists files; each file entry carries `ops`:
    {"type": "a"|"s", "index": <n-th audio/subtitle track, from 0>,
     "language": "kor"            # optional: new language tag (with "old": tag expected now)
     "default": true|false}       # optional: set/clear the default flag
Setting a default automatically clears the default flag on every other track of the same type, read from the
file itself (not from the scan report). Forced flags are never touched.

All of a file's ops are applied in ONE write - mkvpropedit in place for Matroska/WebM, otherwise an ffmpeg stream
copy that replaces the original atomically - then verified with ffprobe.

Reliability (an action must never sit in "running" with no explanation):
  * every action ends in done or failed, with a result for each file
  * a watchdog per file (60 s for header edits; copies get time in proportion to size)
  * progress ("2 of 5 files") and one activity-log line per file, with the tool's own error text on failure
  * the whole run is wrapped: an unexpected exception marks the action failed with the traceback summary
  * a reaper marks actions still "running" after 10 minutes without progress as "failed (stalled)" and frees the lock
  * a full snapshot of every track's language/default/forced flags is logged before each edit; undo restores it
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import shlex
import subprocess
import threading
import time
import traceback

from . import arr, db, langs, media, settings

log = logging.getLogger("anotherr.trackedit")

KINDS = ("retag_audio", "set_default_audio", "set_default_subtitle")
MATROSKA = (".mkv", ".mka", ".mk3d", ".webm")
# AVI has no per-track language or default flag, and ffmpeg can't pick a muxer for .divx: a copy would be
# written in full and then fail verification every time, so these files are never proposed or edited.
NO_TRACK_EDIT = (".avi", ".divx")
LOG_PATH = os.path.join(db.CONFIG_DIR, "retag_log.jsonl")  # rollback records (name kept from the first version)
MKVPROPEDIT = os.environ.get("ANOTHERR_MKVPROPEDIT", "mkvpropedit")
EDIT_TIMEOUT = 60
STALL_AFTER = 600
SLOW_KINDS = {"whisper_subtitle": 2 * 3600, "subgen_transcribe": 1800}  # subgen heartbeats every poll
_lock = threading.Lock()
_owner: dict = {"action": None, "since": 0.0}
_pool = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="anotherr-trackedit-file")


# ====================================================================== ops

def entry_ops(e: dict) -> list[dict]:
    """Ops for a payload entry (retag proposals made before ops existed carry a_index/old/new)."""
    if e.get("ops"):
        return [dict(o) for o in e["ops"]]
    return [{"type": "a", "index": e["a_index"], "language": e["new"], "old": e.get("old", "")}]


def _tracks(info: dict, t: str) -> list[dict]:
    return info["audio"] if t == "a" else [s for s in info["subs"] if not s.get("external")]


def snapshot(info: dict) -> list[dict]:
    return [{"type": t, "index": i, "language": x["lang"], "default": bool(x["default"]), "forced": bool(x["forced"])}
            for t in ("a", "s") for i, x in enumerate(_tracks(info, t))]


def expand(ops: list[dict], info: dict) -> list[dict]:
    """Merge ops per track and clear the default flag on every other track of a type that gets a new default."""
    merged: dict[tuple, dict] = {}
    for o in ops:
        merged.setdefault((o["type"], o["index"]), {"type": o["type"], "index": o["index"]}).update(
            {k: v for k, v in o.items() if k in ("language", "default", "old", "expect_lang")})
    for o in list(merged.values()):
        if o.get("default") is True:
            for i, x in enumerate(_tracks(info, o["type"])):
                if i != o["index"] and x["default"] and (o["type"], i) not in merged:
                    merged[(o["type"], i)] = {"type": o["type"], "index": i, "default": False}
    return sorted(merged.values(), key=lambda o: (o["type"], o["index"]))


def mkv_args(path: str, ops: list[dict]) -> list[str]:
    args = [MKVPROPEDIT, path]
    for o in ops:
        args += ["--edit", f"track:{o['type']}{o['index'] + 1}"]
        if o.get("language"):
            args += ["--set", f"language={o['language']}"]
        if "default" in o:
            args += ["--set", f"flag-default={1 if o['default'] else 0}"]
    return args


def copy_args(path: str, tmp: str, ops: list[dict]) -> list[str]:
    args = [media.FFMPEG, "-nostdin", "-v", "error", "-y", "-i", path, "-map", "0", "-map_metadata", "0", "-c", "copy"]
    for o in ops:
        if o.get("language"):
            args += [f"-metadata:s:{o['type']}:{o['index']}", f"language={o['language']}"]
        if "default" in o:
            args += [f"-disposition:{o['type']}:{o['index']}", "+default" if o["default"] else "-default"]
    return args + [tmp]


def can_edit(path: str | None) -> bool:
    """False for containers whose track headers can't hold a language or default flag (see NO_TRACK_EDIT)."""
    return not (path or "").lower().endswith(NO_TRACK_EDIT)


def method_for(path: str, nlink: int) -> str:
    return "mkvpropedit" if path.lower().endswith(MATROSKA) and nlink == 1 else "copy"


def tmp_path(path: str) -> str:
    return os.path.join(os.path.dirname(path), ".anotherr-edit." + os.path.basename(path))


def preview(path: str, ops: list[dict], cfg: dict) -> dict:
    """Dry-run command for the proposal (default clears are added from the file at run time)."""
    try:
        nlink = os.stat(path).st_nlink
    except OSError:
        nlink = 1
    if not can_edit(path):
        return {"method": "skip", "command": f"(skipped: {os.path.splitext(path)[1]} files can't store track languages or default flags)"}
    if nlink > 1 and not cfg["retag"].get("break_hardlinks"):
        return {"method": "skip", "command": f"(skipped: hardlinked to {nlink - 1} other path(s) - see retag.break_hardlinks)"}
    m = method_for(path, nlink)
    if m == "mkvpropedit":
        return {"method": m, "command": shlex.join(mkv_args(path, ops))}
    return {"method": m, "command": shlex.join(copy_args(path, tmp_path(path), ops)) + "  # then replace the original"}


def describe(ops: list[dict]) -> str:
    parts = []
    for o in ops:
        what = []
        if o.get("language"):
            what.append(f"language={o['language']}")
        if "default" in o:
            what.append("default" if o["default"] else "not default")
        parts.append(f"{o['type']}{o['index'] + 1} " + "+".join(what))
    return ", ".join(parts)


# ====================================================================== one file

def _rm(p: str):
    try:
        os.remove(p)
    except OSError:
        pass


def _shape(info: dict) -> tuple:
    return (len(info["video"]), len(info["audio"]), len(_tracks(info, "s")))


def apply(path: str, ops: list[dict], method: str, size: int) -> tuple[bool, str]:
    """Write all ops in one go and verify. The original is untouched on any failure, and a copy's temp file
    never outlives the call (an exception mid-copy used to leave a full-size .anotherr-edit.* behind)."""
    if method != "copy":
        return _apply(path, ops, method, size)
    try:
        return _apply(path, ops, method, size)
    finally:
        _rm(tmp_path(path))  # already renamed over the original on success; leftovers on any other way out


def _apply(path: str, ops: list[dict], method: str, size: int) -> tuple[bool, str]:
    before = media.summarize(media.ffprobe(path))
    if method == "mkvpropedit":
        try:
            p = subprocess.run(mkv_args(path, ops), capture_output=True, text=True, timeout=EDIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"mkvpropedit took longer than {EDIT_TIMEOUT}s"
        except FileNotFoundError:
            return False, "mkvpropedit is not installed in the container"
        if p.returncode not in (0, 1):  # 1 = finished with warnings
            return False, f"mkvpropedit failed: {(p.stdout + p.stderr).strip()[-400:]}"
        target = path
    else:
        target = tmp_path(path)
        st = os.stat(path)
        timeout = max(EDIT_TIMEOUT, size / (40 * 1024 * 1024))  # stream copies need time in proportion to size
        try:
            p = subprocess.run(copy_args(path, target, ops), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _rm(target)
            return False, f"copy took longer than {timeout:.0f}s"
        if p.returncode != 0 or not os.path.exists(target):
            _rm(target)
            return False, f"ffmpeg copy failed: {p.stderr.strip()[-400:]}"
        new_size = os.path.getsize(target)
        if abs(new_size - st.st_size) > 0.01 * st.st_size:
            _rm(target)
            return False, f"copy size {new_size} differs from original {st.st_size} by more than 1%"
        try:
            os.chown(target, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.chmod(target, st.st_mode & 0o7777)
    after = media.summarize(media.ffprobe(target))
    problems = verify(before, after, ops)
    if problems:
        if method == "copy":
            _rm(target)
        return False, "verification failed: " + "; ".join(problems)
    if method == "copy":
        try:
            os.replace(target, path)
        except OSError as e:  # e.g. Unraid's share layer refusing a rename across disks
            _rm(target)
            return False, f"couldn't replace the original ({e}); it was left untouched"
    return True, "ok"


def verify(before: dict, after: dict, ops: list[dict]) -> list[str]:
    problems = []
    if _shape(after) != _shape(before):
        problems.append(f"stream count changed {_shape(before)} -> {_shape(after)}")
    if abs((after["duration"] or 0) - (before["duration"] or 0)) > 0.1:
        problems.append(f"duration changed {before['duration']} -> {after['duration']}")
    problems += readback(after, ops)
    return problems


def readback(info: dict, ops: list[dict]) -> list[str]:
    out = []
    for o in ops:
        tr = _tracks(info, o["type"])
        if o["index"] >= len(tr):
            out.append(f"track {o['type']}{o['index'] + 1} missing")
            continue
        x = tr[o["index"]]
        if o.get("language") and x["lang"] != langs.norm(o["language"]):  # "und" reads back as ""
            out.append(f"track {o['type']}{o['index'] + 1} language is {x['lang'] or 'untagged'}, not {o['language']}")
        if "default" in o and bool(x["default"]) != bool(o["default"]):
            out.append(f"track {o['type']}{o['index'] + 1} default flag is {int(bool(x['default']))}")
    return out


def _allowed(paths: list[str], cfg: dict) -> bool:
    roots = [r.rstrip("/") + "/" for r in (cfg["retag"].get("allowed_paths") or []) if r and r.strip()]
    return any(p and os.path.normpath(p).startswith(r) for p in paths for r in roots)


def _playing(path: str) -> bool:
    from . import scheduler

    names = {os.path.basename((s.get("file") or "").replace("\\", "/")) for s in scheduler._streams_cache.get("sessions") or []}
    return os.path.basename(path) in names


def write_path(f: dict, resolved: str) -> str | None:
    """Where to write: the physical path the file was read from (/mnt/diskN/..., /mnt/cache/...) when that's
    writable, otherwise the user-share path (/mnt/user/...). None when neither is writable.

    The physical path comes first because a copy edit writes a whole new file next to the original: through
    /mnt/user, Unraid puts that new file on the cache pool of a cache-enabled share and the mover has to carry
    it back to the array. Written on the original's own disk it replaces the file in place."""
    from .sync import map_path

    for p in (resolved, map_path(f["arr_path"])):
        if p and os.path.exists(p) and os.access(p, os.W_OK) and os.access(os.path.dirname(p), os.W_OK):
            return p
    return None


def edit_holds() -> dict[int, float]:
    """file_id -> when a track edit last touched it (inf while one is queued/running). A proposal built from a
    scan older than that would repeat an edit that already ran ("file changed since scanned")."""
    out: dict[int, float] = {}
    for a in db.q(f"SELECT status, payload, updated_at FROM actions WHERE kind IN ({','.join('?' * len(KINDS))}) "
                  "AND (status IN ('queued','running') OR (status IN ('done','failed') AND updated_at > ?))",
                  (*KINDS, time.time() - 30 * 86400)):
        for e in (db.unj(a["payload"], {}) or {}).get("files", []):
            if a["status"] in ("queued", "running"):
                out[e["file_id"]] = float("inf")
            elif e.get("status") == "edited":
                out[e["file_id"]] = max(out.get(e["file_id"], 0), a["updated_at"])
    return out


def held(holds: dict[int, float], file_id: int, scan_started) -> bool:
    return file_id in holds and holds[file_id] >= float(scan_started or 0)


def file_facts(f: dict) -> dict:
    """Container, hardlink count and whether a track edit could write this file (for the file page / MCP report)."""
    from .scanner import resolve_path

    path = resolve_path(f)
    if not path:
        return {"found": False}
    out = {"found": True, "container": os.path.splitext(path)[1].lstrip(".").lower(), "nlink": 1, "writable": False,
           "write_path": None, "allowed": None, "edit_method": None}
    try:
        out["nlink"] = os.stat(path).st_nlink
    except OSError:
        pass
    wp = write_path(f, path)
    from .sync import map_path

    # same test edit_file uses: the read path OR the user-share path (allowed paths are usually /mnt/user/...)
    out["allowed"] = _allowed([path, map_path(f["arr_path"]), wp or ""], settings.get())
    out.update(writable=bool(wp), write_path=wp, edit_method=method_for(path, out["nlink"]))
    return out


def _log(rec: dict):
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def edit_file(entry: dict, cfg: dict, action_id: int) -> dict:
    """Safety checks, rollback record, one write, verification. Returns {status, why, method}."""
    from .scanner import resolve_path
    from .sync import map_path

    f = db.q1("SELECT * FROM files WHERE id=?", (entry["file_id"],))
    if not f or f["missing"]:
        return {"status": "skipped", "why": "no longer in the library"}
    read_path = resolve_path(f)
    if not read_path:
        return {"status": "failed", "why": "file not found"}
    if not can_edit(read_path):
        return {"status": "skipped", "why": f"{os.path.splitext(read_path)[1]} files can't store track languages or default flags"}
    if not _allowed([read_path, map_path(f["arr_path"])], cfg):
        return {"status": "skipped", "why": "not under an allowed path (Settings -> Retag audio -> Allowed paths)"}
    path = write_path(f, read_path)
    if not path:
        return {"status": "skipped", "why": "not writable - mount this media folder read/write (see Settings -> Retag audio)"}
    st = os.stat(path)
    if entry.get("scan_started") and st.st_mtime > float(entry["scan_started"]) + 1:
        db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?, 'quick', 'retag', ?)",
             (f["id"], time.time()))
        return {"status": "skipped", "why": "file changed since it was scanned - queued a rescan"}
    if _playing(path):
        return {"status": "skipped", "why": "Plex is playing it right now"}
    if st.st_nlink > 1 and not cfg["retag"].get("break_hardlinks"):
        return {"status": "skipped", "why": f"hardlinked to {st.st_nlink - 1} other path(s) (seeding copy?)"}
    method = method_for(path, st.st_nlink)
    try:
        info = media.summarize(media.ffprobe(path))
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "why": f"ffprobe: {e}"}
    ops = entry_ops(entry)
    for o in ops:  # the file must still be in the state the proposal was based on
        tr = _tracks(info, o["type"])
        if o["index"] >= len(tr):
            return {"status": "skipped", "why": f"track {o['type']}{o['index'] + 1} no longer exists"}
        cur = tr[o["index"]]["lang"]
        if "old" in o and cur != langs.norm(o.get("old")):
            return {"status": "skipped", "why": f"tag changed since the scan (track {o['type']}{o['index'] + 1} is now {cur or 'untagged'})"}
        if o.get("expect_lang") is not None and cur != langs.norm(o["expect_lang"]):
            return {"status": "skipped", "why": f"track {o['type']}{o['index'] + 1} is now {cur or 'untagged'}, not {o['expect_lang']}"}
    ops = expand(ops, info)
    _log({"ts": time.time(), "action_id": action_id, "file_id": f["id"], "path": path, "event": "before",
          "snapshot": snapshot(info), "ops": ops, "size": st.st_size, "mtime": st.st_mtime, "method": method,
          # retag-era fields, still read by older undo records
          "a_index": ops[0]["index"], "old": "", "new": ops[0].get("language", "")})
    ok, msg = apply(path, ops, method, st.st_size)
    _log({"ts": time.time(), "action_id": action_id, "file_id": f["id"], "path": path,
          "event": "applied" if ok else "failed", "message": msg})
    if not ok:
        return {"status": "failed", "why": msg, "method": method}
    if method == "copy":
        from . import frames

        frames.drop_file(f["id"])
    return {"status": "edited", "why": describe(ops), "method": method, "ops_applied": ops}


# ====================================================================== action runs

def start(action_id: int) -> dict:
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    n = len((db.unj(a["payload"], {}) or {}).get("files", []))
    if not settings.get()["retag"].get("enabled"):
        db.x("UPDATE actions SET status='failed', result=?, updated_at=? WHERE id=?",
             ("Track editing is turned off (Settings -> Retag audio). Nothing was changed.", time.time(), action_id))
        return {"ok": False, "error": "track editing is turned off in Settings -> Retag audio"}
    db.x("UPDATE actions SET status='running', result=?, updated_at=? WHERE id=?",
         (f"queued: 0 of {n} files done", time.time(), action_id))
    db.log(f"{a['kind']} action {action_id} started ({n} file(s))")
    threading.Thread(target=run, args=(action_id,), daemon=True, name=f"anotherr-trackedit-{action_id}").start()
    return {"ok": True, "result": f"started: {n} file(s)"}


def run(action_id: int):
    """Thread entry point: one action at a time, and whatever happens the action ends done or failed."""
    if not _lock.acquire(timeout=STALL_AFTER):
        _finish(action_id, "failed", "another track edit was still running after 10 minutes - try again")
        return
    _owner.update(action=action_id, since=time.time())
    try:
        _run(action_id)
    except Exception as e:  # noqa: BLE001
        log.exception("track edit action %s crashed", action_id)
        _finish(action_id, "failed", f"crashed: {e} ({traceback.format_exc().strip().splitlines()[-1][:200]})")
    finally:
        _owner.update(action=None, since=0.0)
        if _lock.locked():
            try:
                _lock.release()
            except RuntimeError:
                pass  # the reaper already released it


def _finish(action_id: int, status: str, result: str, payload: dict | None = None) -> bool:
    """Final status - only if the action is still running (the reaper may have given up on it)."""
    sql = "UPDATE actions SET status=?, result=?, updated_at=?" + (", payload=?" if payload is not None else "") + \
          " WHERE id=? AND status='running'"
    args = [status, result, time.time()] + ([db.j(payload)] if payload is not None else []) + [action_id]
    return db.n(sql, args) > 0


def _run(action_id: int):
    from . import scheduler, unraid

    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    p = db.unj(a["payload"], {}) or {}
    cfg = settings.get()
    entries = p.get("files", [])
    ids = sorted({int(e["file_id"]) for e in entries}) or [0]
    disk = {r["id"]: r["disk"] for r in db.q(f"SELECT id, disk FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)}
    labels = {r["id"]: f"{r['title']} {r['label'] or ''}".strip() for r in db.q(
        f"SELECT f.id, f.label, i.title FROM files f JOIN items i ON i.id=f.item_id WHERE f.id IN ({','.join('?' * len(ids))})", ids)}
    spinning = unraid.spun_up_disks()
    # disks already spinning first, then one disk at a time - a batch shouldn't wake the whole array
    entries.sort(key=lambda e: (spinning is not None and disk.get(e["file_id"]) not in spinning,
                                disk.get(e["file_id"]) or "", e.get("label") or ""))
    edited = []
    for n, e in enumerate(entries, 1):
        t0 = time.time()
        fut = _pool.submit(edit_file, e, cfg, action_id)
        limit = EDIT_TIMEOUT if e.get("method") != "copy" else None  # copies have their own size-based timeout
        try:
            r = fut.result(timeout=limit)
        except cf.TimeoutError:
            r = {"status": "failed", "why": f"no result after {limit}s - gave up on this file (if the edit finishes "
                                            "late, the queued rescan reads the real state)"}
            scheduler.enqueue([e["file_id"]], "quick", "retag")
        except Exception as ex:  # noqa: BLE001 - one bad file never stops the batch
            log.exception("track edit failed for file %s", e["file_id"])
            r = {"status": "failed", "why": f"{type(ex).__name__}: {ex}"}
        secs = round(time.time() - t0, 1)
        e.update(status=r["status"], outcome=r.get("why", ""), method=r.get("method", e.get("method")), seconds=secs)
        if r.get("ops_applied"):
            e["ops_applied"] = r["ops_applied"]
        if r["status"] == "edited":
            edited.append(e["file_id"])
        db.log(f"{a['kind']} #{action_id} {labels.get(e['file_id'], e['file_id'])}: {r['status']} - "
               f"{r.get('why', '')} ({secs}s)", "info" if r["status"] == "edited" else "warning")
        db.x("UPDATE actions SET payload=?, result=?, updated_at=? WHERE id=? AND status='running'",
             (db.j(p), f"{n} of {len(entries)} files done", time.time(), action_id))
    item = db.q1("SELECT * FROM items WHERE id=?", (p.get("item_id"),))
    notes = refresh_apps(item, bazarr=a["kind"] == "retag_audio") if edited and item else []
    if edited:
        scheduler.enqueue(sorted(set(edited)), "quick", "retag")  # the rescan reads the change back
    counts = {s: sum(1 for x in entries if x.get("status") == s) for s in ("edited", "skipped", "failed")}
    status = "done" if counts["edited"] and not counts["failed"] else "failed"
    msg = f"edited {counts['edited']}, skipped {counts['skipped']}, failed {counts['failed']} of {len(entries)}" + \
          ("; " + "; ".join(notes) if notes else "") + ("; waiting for rescans to read the change back" if edited else "")
    _finish(action_id, status, msg, p)
    db.log(f"{a['kind']} #{action_id} {(item or {}).get('title', '')}: {msg}", "info" if status == "done" else "warning")


def refresh_apps(item: dict, bazarr: bool = True) -> list[str]:
    """Ask Sonarr/Radarr (and Bazarr, for language changes) to re-read the files. Best effort."""
    notes = []
    client = arr.Sonarr() if item["source"] == "sonarr" else arr.Radarr()
    name = item["source"].title()
    if not client.configured:
        notes.append(f"{name} not configured - skipped its rescan")
    else:
        try:
            if item["source"] == "sonarr":
                client.req("POST", "/api/v3/command", json={"name": "RescanSeries", "seriesId": item["arr_id"]})
            else:
                client.req("POST", "/api/v3/command", json={"name": "RescanMovie", "movieId": item["arr_id"]})
            notes.append(f"{name} rescan requested")
        except arr.ServiceError as e:
            notes.append(f"{name} rescan failed: {e}")
    b = arr.Bazarr()
    if bazarr and b.configured:
        try:
            if item["source"] == "sonarr":
                b.req("PATCH", "/api/series", params={"seriesid": item["arr_id"], "action": "scan-disk"})
            else:
                b.req("PATCH", "/api/movies", params={"radarrid": item["arr_id"], "action": "scan-disk"})
            notes.append("Bazarr disk scan requested")
        except arr.ServiceError as e:
            notes.append(f"Bazarr disk scan failed: {e}")
    return notes


def sweep_temp(days: float = 7) -> int:
    """Remove copy temp files that outlived their edit (container killed or restarted mid-copy, a failed
    delete on the share layer). Temp files only exist while an edit runs, so with none running every
    .anotherr-edit.* next to a file copied in the last `days` is a leftover. Only logged paths are checked:
    no directory walks, so no array disks are woken for this."""
    if _lock.locked():
        return 0  # an edit is running: its temp file is live
    cut = time.time() - days * 86400
    paths: set[str] = set()
    try:
        with open(LOG_PATH) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("event") == "before" and r.get("method") == "copy" and r.get("path") and r.get("ts", 0) >= cut:
                    paths.add(r["path"])
    except OSError:
        return 0
    n = 0
    for path in sorted(paths):
        tmp = tmp_path(path)
        if os.path.exists(tmp):
            _rm(tmp)
            if not os.path.exists(tmp):
                n += 1
                db.log(f"Removed a temp file left by an interrupted track edit: {tmp}", "warning")
    return n


def reap() -> int:
    """Actions still 'running' with no progress for 10 minutes are marked failed (stalled); the lock is freed."""
    n = 0
    for a in db.q("SELECT id, kind, result, updated_at FROM actions WHERE status='running' AND updated_at < ?",
                  (time.time() - STALL_AFTER,)):
        limit = SLOW_KINDS.get(a["kind"], STALL_AFTER)  # Bazarr's Whisper transcription legitimately runs long
        if a["updated_at"] > time.time() - limit:
            continue
        db.x("UPDATE actions SET status='failed', result=?, updated_at=? WHERE id=? AND status='running'",
             (f"failed (stalled): no progress for {limit // 60} minutes; last state: {a['result'] or 'none'}",
              time.time(), a["id"]))
        db.log(f"{a['kind']} action {a['id']} stalled - marked failed", "warning")
        if _owner.get("action") == a["id"] and _lock.locked():
            _owner.update(action=None, since=0.0)
            try:
                _lock.release()
            except RuntimeError:
                pass
        n += 1
    return n


# ====================================================================== read-back & undo

def after_scan(file_id: int, info: dict):
    """A rescan after an edit reads the change back: confirm it, or mark the action failed."""
    if not info.get("audio") and not info.get("subs"):
        return
    for a in db.q(f"SELECT * FROM actions WHERE kind IN ({','.join('?' * len(KINDS))}) AND status IN ('done','failed') "
                  "AND updated_at > ?", (*KINDS, time.time() - 14 * 86400)):
        p = db.unj(a["payload"], {}) or {}
        changed = False
        for e in p.get("files", []):
            if e["file_id"] != file_id or e.get("status") != "edited" or e.get("verified") is not None:
                continue
            problems = readback(info, e.get("ops_applied") or entry_ops(e))
            e["verified"] = not problems
            changed = True
            if problems:
                db.x("UPDATE actions SET status='failed', result=COALESCE(result,'') || ? WHERE id=?",
                     (f"; read-back failed for {e.get('label')}: {problems[0]}", a["id"]))
        if changed:
            db.x("UPDATE actions SET payload=? WHERE id=?", (db.j(p), a["id"]))


def undo(action_id: int) -> dict:
    """Restore every track's language and default flag to the snapshot taken before the edit."""
    recs = []
    try:
        with open(LOG_PATH) as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("action_id") == action_id:
                    recs.append(r)
    except (OSError, ValueError):
        pass
    applied = {r["file_id"] for r in recs if r["event"] == "applied"}
    befores = [r for r in recs if r["event"] == "before" and r["file_id"] in applied]
    if not befores:
        return {"ok": False, "error": "nothing to undo"}
    done, failed = 0, []
    for r in befores:
        path = r["path"]
        try:
            st = os.stat(path)
            info = media.summarize(media.ffprobe(path))
            if r.get("snapshot") is not None:
                ops = []
                for s in r["snapshot"]:
                    tr = _tracks(info, s["type"])
                    if s["index"] >= len(tr):
                        continue
                    cur = tr[s["index"]]
                    o = {"type": s["type"], "index": s["index"]}
                    if cur["lang"] != s["language"]:
                        o["language"] = s["language"] or "und"
                    if bool(cur["default"]) != s["default"]:
                        o["default"] = s["default"]
                    if len(o) > 2:
                        ops.append(o)
            else:  # records from the first retag version: one language change
                ops = [{"type": "a", "index": r["a_index"], "language": r["old"] or "und"}]
            if not ops:
                ok, msg = True, "already as before"
            else:
                ok, msg = apply(path, ops, method_for(path, st.st_nlink), st.st_size)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        _log({"ts": time.time(), "action_id": action_id, "file_id": r["file_id"], "path": path,
              "event": "undone" if ok else "undo_failed", "message": msg})
        if ok:
            done += 1
            db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?, 'quick', 'retag', ?)",
                 (r["file_id"], time.time()))
        else:
            failed.append(f"{os.path.basename(path)}: {msg}")
    db.x("UPDATE actions SET status='undone', result=?, updated_at=? WHERE id=?",
         (f"Undone: restored {done} file(s)" + (f", {len(failed)} failed" if failed else ""), time.time(), action_id))
    db.log(f"Action {action_id} undone: {done} restored, {len(failed)} failed", "warning" if failed else "info")
    return {"ok": not failed, "restored": done, "failed": failed}
