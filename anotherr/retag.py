"""retag_audio: write a detected audio language into the file's track tag (with approval).

Two cases:
  untagged  - track has no tag (or `und`); detection unanimous and confident
  wrong_tag - track is tagged, but every window says another language, which is the series' own language

Proposals are batched per series + season (+ case). Nothing is ever written automatically; editing needs
`retag.enabled`, a writable media mount, and the file under one of `retag.allowed_paths`.
Matroska/WebM files are edited in place with mkvpropedit (header only); anything else, or hardlinked files
when `break_hardlinks` is on, goes through an ffmpeg stream copy that atomically replaces the original.
Every change is logged first (rollback record) and verified with ffprobe afterwards; undo restores old tags.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import statistics
import subprocess
import threading
import time

from . import arr, db, langs, media, settings

log = logging.getLogger("anotherr.retag")

KIND = "retag_audio"
UNTAGGED, WRONG = "untagged", "wrong_tag"
# a file that's about to be replaced (or can't be trusted) is not worth editing
BLOCKING = {"wrong_content", "truncated", "unreadable", "duration_short", "duration_short_warn", "double_length",
            "no_dialogue", "file_not_found", "decode_errors_severe", "decode_stopped_early"}
MATROSKA = (".mkv", ".mka", ".mk3d", ".webm")
LOG_PATH = os.path.join(db.CONFIG_DIR, "retag_log.jsonl")
MKVPROPEDIT = os.environ.get("ANOTHERR_MKVPROPEDIT", "mkvpropedit")
EDIT_TIMEOUT = 60
_run_lock = threading.Lock()


# ====================================================================== evidence & rules

def _assess(windows: list[dict]) -> dict:
    langs_ = [langs.norm(w.get("lang")) for w in windows]
    probs = [float(w.get("prob") or 0) for w in windows]
    if not langs_:
        return {"det": "", "n": 0, "agree": 0, "all_agree": False, "conf": 0.0}
    det = max(set(langs_), key=langs_.count)
    agree = langs_.count(det)
    conf = statistics.mean(p for l, p in zip(langs_, probs) if l == det)
    return {"det": det, "n": len(langs_), "agree": agree, "all_agree": agree == len(langs_), "conf": round(conf, 3)}


def _evidence(f: dict) -> dict | None:
    ev = db.unj(f.get("audio_evidence"))
    if ev:
        return ev
    # scans from before the evidence column existed
    if not f.get("last_scan_id"):
        return None
    s = db.q1("SELECT started_at, result FROM scans WHERE id=?", (f["last_scan_id"],))
    res = db.unj(s["result"], {}) if s else {}
    audio = (res.get("streams") or {}).get("audio") or []
    by_idx = {t["a_index"]: t for t in res.get("audio_tracks") or []}
    if not audio:
        return None
    return {"scan_started": s["started_at"], "tracks": [
        {"a_index": a["a_index"], "tag": a.get("lang", ""), "title": a.get("title", ""),
         "commentary": bool(a.get("commentary")), "forced": bool(a.get("forced")),
         "hi": bool(a.get("hearing_impaired")), "speech_s": by_idx.get(a["a_index"], {}).get("speech_s"),
         "windows": by_idx.get(a["a_index"], {}).get("windows", [])} for a in audio]}


def _season(f: dict) -> int | None:
    eps = db.unj(f.get("episodes"), []) or []
    s = [e.get("season") for e in eps if e.get("season") is not None]
    return min(s) if s else None


def strong_untagged(a: dict) -> bool:
    return a["n"] >= 2 and a["all_agree"] and a["conf"] >= 0.90


def strong_wrong(a: dict) -> bool:
    return a["n"] >= 3 and a["all_agree"] and a["conf"] >= 0.95


def assess_item(item: dict) -> list[dict]:
    """Every track of the series that qualifies for a retag, with the reasons."""
    orig = langs.norm(item.get("language_override")) or langs.norm(item.get("original_language"))
    files = db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item["id"],))
    info = {}
    for f in files:
        ev = _evidence(f)
        if not ev:
            continue
        codes = {r["code"]: r["status"] for r in db.q("SELECT code, status FROM issues WHERE file_id=? AND "
                                                       "status IN ('open','ignored')", (f["id"],))}
        info[f["id"]] = {"file": f, "ev": ev, "codes": codes, "season": _season(f),
                         "group": f.get("release_group") or "",
                         "tracks": {t["a_index"]: dict(t, a=_assess(t.get("windows") or [])) for t in ev["tracks"]}}

    def siblings(fid: int, a_index: int, det: str) -> list[str]:
        me = info[fid]
        out = []
        for oid, o in info.items():
            if oid == fid or o["season"] != me["season"]:
                continue
            if me["group"] and o["group"] and me["group"] != o["group"]:
                continue  # another release
            t = o["tracks"].get(a_index)
            if t and t["a"]["det"] == det and strong_untagged(t["a"]):
                out.append(o["file"].get("label") or str(oid))
        return out

    out = []
    for fid, x in info.items():
        if set(x["codes"]) & BLOCKING and any(x["codes"][c] == "open" for c in set(x["codes"]) & BLOCKING):
            continue
        rows = []
        for idx, t in x["tracks"].items():
            a = t["a"]
            if t.get("commentary") or t.get("forced") or t.get("hi"):
                continue
            if (t.get("speech_s") or 0) < 10 or not a["det"] or a["det"] not in langs.NAMES:
                continue
            tag = langs.norm(t.get("tag"))
            if not tag:
                if x["codes"].get("audio_untagged") == "ignored":
                    continue
                sib = siblings(fid, idx, a["det"])
                if strong_untagged(a):
                    why = f"{a['agree']}/{a['n']} windows"
                elif a["agree"] * 2 > a["n"] and len(sib) >= 2:
                    why = f"{a['agree']}/{a['n']} windows, confirmed by {len(sib)} other files of the release"
                else:
                    continue
                rows.append({"case": UNTAGGED, "a_index": idx, "old": "", "new": a["det"], **a, "why": why,
                             "siblings": sib[:5], "mismatch": bool(orig) and a["det"] != orig})
            elif tag != a["det"]:
                if x["codes"].get("audio_mislabeled") == "ignored" or not orig or a["det"] != orig:
                    continue  # a wrong tag is only corrected toward the series' own language
                sib = siblings(fid, idx, a["det"])
                if strong_wrong(a) and len(sib) >= 2:
                    rows.append({"case": WRONG, "a_index": idx, "old": tag, "new": a["det"], **a,
                                 "why": f"{a['agree']}/{a['n']} windows at {a['conf']:.0%}, {len(sib)} other files agree",
                                 "siblings": sib[:5], "mismatch": False})
        # a file whose tracks don't match the series' language needs a person to look at the whole file
        review = any(r["mismatch"] for r in rows if r["case"] == UNTAGGED)
        for r in rows:
            r.update(file_id=fid, label=x["file"].get("label") or "", season=x["season"],
                     review=review and r["case"] == UNTAGGED, scan_started=x["ev"].get("scan_started"))
            out.append(r)
    return out


# ====================================================================== proposals

def _plan(f: dict, row: dict, cfg: dict) -> dict:
    path = f.get("local_path") or f.get("arr_path")
    try:
        nlink = os.stat(path).st_nlink
    except OSError:
        nlink = 1
    if nlink > 1 and not cfg["retag"].get("break_hardlinks"):
        return {"method": "skip", "command": f"(skipped: hardlinked to {nlink - 1} other path(s) - see retag.break_hardlinks)"}
    if path.lower().endswith(MATROSKA) and nlink == 1:
        cmd = [MKVPROPEDIT, path, "--edit", f"track:a{row['a_index'] + 1}", "--set", f"language={row['new']}"]
        return {"method": "mkvpropedit", "command": shlex.join(cmd)}
    tmp = os.path.join(os.path.dirname(path), ".anotherr-retag." + os.path.basename(path))
    cmd = [media.FFMPEG, "-i", path, "-map", "0", "-map_metadata", "0", "-c", "copy",
           f"-metadata:s:a:{row['a_index']}", f"language={row['new']}", tmp]
    return {"method": "copy", "command": shlex.join(cmd) + "  # then replace the original"}


def _reason(item: dict, case: str, season, review: bool, rows: list[dict]) -> str:
    where = f"{item['title']}" + (f" Season {season}" if season is not None else "")
    tracks = sorted({r["a_index"] + 1 for r in rows})
    news = sorted({langs.name(r["new"]) for r in rows})
    files = len({r["file_id"] for r in rows})
    conf = statistics.mean(r["conf"] for r in rows)
    every = all(r["all_agree"] for r in rows)
    if case == WRONG:
        olds = ", ".join(sorted({langs.name(r["old"]) for r in rows}))
        return (f"{where}: set audio track {'/'.join(map(str, tracks))} to {', '.join(news)} on {files} file(s). "
                f"Was tagged {olds}. {', '.join(news)} detected in every window, average confidence {conf:.2f}. "
                "Manual approval only.")
    lead = "Manual review - detected language differs from the series' language. " if review else ""
    return (f"{lead}{where}: set audio track {'/'.join(map(str, tracks))} to {', '.join(news)} on {files} file(s). "
            f"Was untagged. {', '.join(news)} detected in "
            + ("every window" if every else "most windows (confirmed by sibling files)")
            + f", average confidence {conf:.2f}.")


def refresh_item(item_id: int) -> dict:
    """Rebuild this series' retag proposals from the latest evidence; withdraw ones nothing supports anymore."""
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        return {}
    cfg = settings.get()
    rows = assess_item(item)
    # files already edited recently are waiting for their read-back rescan
    busy = set()
    for a in db.q("SELECT payload FROM actions WHERE kind=? AND status IN ('running','done') AND updated_at > ?",
                  (KIND, time.time() - 7 * 86400)):
        for f in (db.unj(a["payload"], {}) or {}).get("files", []):
            if f.get("status") == "edited" and not f.get("verified"):
                busy.add((f["file_id"], f["a_index"]))
    rows = [r for r in rows if (r["file_id"], r["a_index"]) not in busy]
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["case"], r["season"], r["review"]), []).append(r)
    existing = {}
    for a in db.q("SELECT * FROM actions WHERE kind=? AND status='proposed'", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == item_id:
            existing[tuple(p.get("key") or ())] = a
    now = time.time()
    made = {"created": 0, "updated": 0, "withdrawn": 0}
    files = {f["id"]: f for f in db.q("SELECT * FROM files WHERE item_id=?", (item_id,))}
    for key, grp in groups.items():
        case, season, review = key
        grp.sort(key=lambda r: (r["label"], r["a_index"]))
        payload = {"item_id": item_id, "key": list(key), "case": case, "season": season, "review": review,
                   "series_language": langs.norm(item.get("language_override")) or langs.norm(item.get("original_language")),
                   "files": [dict(r, **_plan(files[r["file_id"]], r, cfg), path=files[r["file_id"]].get("local_path"),
                                  status="pending") for r in grp]}
        reason = _reason(item, case, season, review, grp)
        codes = ["audio_mislabeled" if case == WRONG else "audio_untagged"]
        a = existing.pop(tuple(key), None) or existing.pop(tuple(list(key)), None)
        if a:
            db.x("UPDATE actions SET reason=?, payload=?, file_id=?, updated_at=? WHERE id=?",
                 (reason, db.j(payload), grp[0]["file_id"], now, a["id"]))
            made["updated"] += 1
        else:
            db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                 "VALUES (?,?,?,?, 'proposed', 0, ?, ?, ?)", (grp[0]["file_id"], KIND, reason, db.j(codes), db.j(payload),
                                                              now, now))
            made["created"] += 1
    for a in existing.values():
        db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: no files in this group qualify anymore', "
             "updated_at=? WHERE id=?", (now, a["id"]))
        made["withdrawn"] += 1
    return made


def refresh_all() -> int:
    ids = [r["item_id"] for r in db.q(
        "SELECT DISTINCT f.item_id FROM issues x JOIN files f ON f.id=x.file_id WHERE x.status IN ('open','ignored') "
        "AND x.code IN ('audio_untagged','audio_mislabeled') AND f.missing=0")]
    ids += [r["item_id"] for r in db.q(
        "SELECT DISTINCT f.item_id FROM actions a JOIN files f ON f.id=a.file_id WHERE a.kind=? AND a.status='proposed'",
        (KIND,))]
    for i in set(ids):
        try:
            refresh_item(i)
        except Exception:  # noqa: BLE001
            log.exception("retag refresh failed for item %s", i)
    return len(set(ids))


# ====================================================================== execution

def _log(rec: dict):
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _allowed(paths: list[str], cfg: dict) -> bool:
    roots = [r.rstrip("/") + "/" for r in (cfg["retag"].get("allowed_paths") or []) if r and r.strip()]
    return any(p and os.path.normpath(p).startswith(r) for p in paths for r in roots)


def _playing(path: str) -> bool:
    from . import scheduler

    names = {os.path.basename((s.get("file") or "").replace("\\", "/")) for s in scheduler._streams_cache.get("sessions") or []}
    return os.path.basename(path) in names


def _shape(info: dict) -> tuple:
    return (len(info["video"]), len(info["audio"]), len(info["subs"]))


def _write(path: str, a_index: int, code: str, method: str, size: int) -> tuple[bool, str]:
    """Write the tag; returns (ok, message). The original is left untouched on any failure."""
    before = media.summarize(media.ffprobe(path))
    if method == "mkvpropedit":
        try:
            p = subprocess.run([MKVPROPEDIT, path, "--edit", f"track:a{a_index + 1}", "--set", f"language={code}"],
                               capture_output=True, text=True, timeout=EDIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"mkvpropedit took longer than {EDIT_TIMEOUT}s"
        except FileNotFoundError:
            return False, "mkvpropedit is not installed"
        if p.returncode not in (0, 1):  # 1 = finished with warnings
            return False, f"mkvpropedit failed: {(p.stdout + p.stderr).strip()[-300:]}"
        target = path
    else:
        target = os.path.join(os.path.dirname(path), ".anotherr-retag." + os.path.basename(path))
        st = os.stat(path)
        cmd = [media.FFMPEG, "-nostdin", "-v", "error", "-y", "-i", path, "-map", "0", "-map_metadata", "0",
               "-c", "copy", f"-metadata:s:a:{a_index}", f"language={code}", target]
        # a stream copy of a multi-GB file can't finish in 60 s on spinning disks; allow ~40 MB/s
        timeout = max(EDIT_TIMEOUT, size / (40 * 1024 * 1024))
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _rm(target)
            return False, f"copy took longer than {timeout:.0f}s"
        if p.returncode != 0 or not os.path.exists(target):
            _rm(target)
            return False, f"ffmpeg copy failed: {p.stderr.strip()[-300:]}"
        new_size = os.path.getsize(target)
        if abs(new_size - st.st_size) > 0.01 * st.st_size:
            _rm(target)
            return False, f"copy size {new_size} differs from original {st.st_size} by more than 1%"
        try:
            os.chown(target, st.st_uid, st.st_gid)
        except OSError:
            pass  # running as the same user already
        os.chmod(target, st.st_mode & 0o7777)
    after = media.summarize(media.ffprobe(target))
    problems = []
    if _shape(after) != _shape(before):
        problems.append(f"stream count changed {_shape(before)} -> {_shape(after)}")
    if abs((after["duration"] or 0) - (before["duration"] or 0)) > 0.1:
        problems.append(f"duration changed {before['duration']} -> {after['duration']}")
    if a_index >= len(after["audio"]) or after["audio"][a_index]["lang"] != langs.norm(code):  # "und" reads as ""
        problems.append("the new tag doesn't read back")
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


def write_path(f: dict, resolved: str) -> str | None:
    """Where to write: the user-share path (/mnt/user/...) when that's writable - so only the media folders
    need a read/write mount - otherwise the path the file was read from. None when neither is writable."""
    from .sync import map_path

    for p in (map_path(f["arr_path"]), resolved):
        if p and os.path.exists(p) and os.access(p, os.W_OK) and os.access(os.path.dirname(p), os.W_OK):
            return p
    return None


def _rm(p: str):
    try:
        os.remove(p)
    except OSError:
        pass


def edit_one(entry: dict, cfg: dict, action_id: int) -> dict:
    from .scanner import resolve_path
    from .sync import map_path

    f = db.q1("SELECT * FROM files WHERE id=?", (entry["file_id"],))
    if not f or f["missing"]:
        return {"status": "skipped", "why": "no longer in the library"}
    read_path = resolve_path(f)
    if not read_path:
        return {"status": "failed", "why": "file not found"}
    if not _allowed([read_path, map_path(f["arr_path"])], cfg):
        return {"status": "skipped", "why": "not under an allowed path (Settings -> Retag -> Allowed paths)"}
    path = write_path(f, read_path)
    if not path:
        return {"status": "skipped", "why": "not writable - mount this media folder read/write (see Settings -> Retag)"}
    try:
        st = os.stat(path)
    except OSError as e:
        return {"status": "failed", "why": str(e)}
    if entry.get("scan_started") and st.st_mtime > float(entry["scan_started"]) + 1:
        db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?, 'quick', 'retag', ?)",
             (f["id"], time.time()))
        return {"status": "skipped", "why": "file changed since it was scanned - queued a rescan"}
    if _playing(path):
        return {"status": "skipped", "why": "Plex is playing it right now"}
    if st.st_nlink > 1 and not cfg["retag"].get("break_hardlinks"):
        return {"status": "skipped", "why": f"hardlinked to {st.st_nlink - 1} other path(s) (seeding copy?)"}
    method = "mkvpropedit" if path.lower().endswith(MATROSKA) and st.st_nlink == 1 else "copy"
    try:
        info = media.summarize(media.ffprobe(path))
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "why": f"ffprobe: {e}"}
    a = entry["a_index"]
    if a >= len(info["audio"]):
        return {"status": "skipped", "why": "audio track no longer exists"}
    current = info["audio"][a]["lang"]
    if current != langs.norm(entry.get("old")):
        return {"status": "skipped", "why": f"tag changed since the scan (now {current or 'untagged'})"}
    _log({"ts": time.time(), "action_id": action_id, "file_id": f["id"], "path": path, "a_index": a,
          "old": current or "und", "new": entry["new"], "size": st.st_size, "mtime": st.st_mtime, "method": method,
          "event": "before"})
    ok, msg = _write(path, a, entry["new"], method, st.st_size)
    _log({"ts": time.time(), "action_id": action_id, "file_id": f["id"], "path": path, "a_index": a,
          "event": "applied" if ok else "failed", "message": msg})
    if not ok:
        return {"status": "failed", "why": msg, "method": method}
    if method == "copy":
        from . import frames

        frames.drop_file(f["id"])
    return {"status": "edited", "why": f"{current or 'untagged'} -> {entry['new']}", "method": method}


def start(action_id: int) -> dict:
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not settings.get()["retag"].get("enabled"):
        db.x("UPDATE actions SET status='failed', result=?, updated_at=? WHERE id=?",
             ("Retagging is turned off (Settings -> Retag audio). Nothing was changed.", time.time(), action_id))
        return {"ok": False, "error": "retagging is turned off in Settings"}
    db.x("UPDATE actions SET status='running', updated_at=? WHERE id=?", (time.time(), action_id))
    threading.Thread(target=run, args=(action_id,), daemon=True, name=f"anotherr-retag-{action_id}").start()
    return {"ok": True, "result": f"started: {len((db.unj(a['payload'], {}) or {}).get('files', []))} track(s)"}


def run(action_id: int):
    with _run_lock:
        _run(action_id)


def _run(action_id: int):
    from . import scheduler, unraid

    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    p = db.unj(a["payload"], {}) or {}
    cfg = settings.get()
    spinning = unraid.spun_up_disks()
    disk = {r["id"]: r["disk"] for r in db.q("SELECT id, disk FROM files WHERE id IN (%s)" %
                                              ",".join(str(int(f["file_id"])) for f in p.get("files", [])) or "0")}
    # spinning disks first, then one disk at a time - a batch shouldn't wake the whole array
    entries = sorted(p.get("files", []), key=lambda e: (spinning is not None and disk.get(e["file_id"]) not in spinning,
                                                        disk.get(e["file_id"]) or "", e.get("label") or ""))
    edited = []
    for e in entries:
        try:
            r = edit_one(e, cfg, action_id)
        except Exception as ex:  # noqa: BLE001 - one bad file never stops the batch
            log.exception("retag failed for file %s", e["file_id"])
            r = {"status": "failed", "why": str(ex)}
        # keep the detection evidence in "why"; the execution outcome goes in "outcome"
        e.update(status=r["status"], outcome=r.get("why", ""), method=r.get("method", e.get("method")))
        if r["status"] == "edited":
            edited.append(e["file_id"])
        db.x("UPDATE actions SET payload=?, updated_at=? WHERE id=?", (db.j(p), time.time(), action_id))
    item = db.q1("SELECT * FROM items WHERE id=?", (p.get("item_id"),))
    notes = _refresh_apps(item) if edited and item else []
    if edited:
        scheduler.enqueue(sorted(set(edited)), "quick", "retag")  # the rescan reads the tag back and closes issues
    counts = {s: sum(1 for e in entries if e.get("status") == s) for s in ("edited", "skipped", "failed")}
    status = "done" if counts["edited"] and not counts["failed"] else "failed"
    msg = f"edited {counts['edited']}, skipped {counts['skipped']}, failed {counts['failed']}" + \
          ("; " + "; ".join(notes) if notes else "") + ("; waiting for rescans to read the tags back" if edited else "")
    db.x("UPDATE actions SET status=?, result=?, payload=?, updated_at=? WHERE id=?",
         (status, msg, db.j(p), time.time(), action_id))
    db.log(f"Retag {(item or {}).get('title', '')}: {msg}", "info" if status == "done" else "warning")


def _refresh_apps(item: dict) -> list[str]:
    """Tell Sonarr/Radarr and Bazarr to re-read the files (best effort; Plex reads tags on its own scans)."""
    notes = []
    client = arr.Sonarr() if item["source"] == "sonarr" else arr.Radarr()
    try:
        if not client.configured:
            raise LookupError
        if item["source"] == "sonarr":
            arr.Sonarr().req("POST", "/api/v3/command", json={"name": "RescanSeries", "seriesId": item["arr_id"]})
        else:
            arr.Radarr().req("POST", "/api/v3/command", json={"name": "RescanMovie", "movieId": item["arr_id"]})
        notes.append(f"{item['source'].title()} rescan requested")
    except LookupError:
        notes.append(f"{item['source'].title()} not configured - skipped its rescan")
    except arr.ServiceError as e:
        notes.append(f"{item['source'].title()} rescan failed: {e}")
    b = arr.Bazarr()
    if b.configured:
        try:
            if item["source"] == "sonarr":
                b.req("PATCH", "/api/series", params={"seriesid": item["arr_id"], "action": "scan-disk"})
            else:
                b.req("PATCH", "/api/movies", params={"radarrid": item["arr_id"], "action": "scan-disk"})
            notes.append("Bazarr disk scan requested")
        except arr.ServiceError as e:
            notes.append(f"Bazarr disk scan failed: {e}")
    return notes


# ====================================================================== read-back & undo

def after_scan(file_id: int, info: dict, scan_id: int):
    """A rescan after a retag reads the tag back: confirm it, or mark the action failed."""
    audio = (info or {}).get("audio") or []
    for a in db.q("SELECT * FROM actions WHERE kind=? AND status IN ('done','failed') AND updated_at > ?",
                  (KIND, time.time() - 14 * 86400)):
        p = db.unj(a["payload"], {}) or {}
        changed = False
        for e in p.get("files", []):
            if e["file_id"] != file_id or e.get("status") != "edited" or e.get("verified") is not None:
                continue
            ok = e["a_index"] < len(audio) and audio[e["a_index"]]["lang"] == e["new"]
            e["verified"] = ok
            changed = True
            if not ok:
                db.x("UPDATE actions SET status='failed', result=COALESCE(result,'') || ? WHERE id=?",
                     (f"; read-back failed for {e.get('label')} track {e['a_index'] + 1}", a["id"]))
        if changed:
            db.x("UPDATE actions SET payload=? WHERE id=?", (db.j(p), a["id"]))


def undo(action_id: int) -> dict:
    """Restore the tags this action changed (from the rollback log)."""
    recs = []
    try:
        with open(LOG_PATH) as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("action_id") == action_id:
                    recs.append(r)
    except (OSError, ValueError):
        pass
    applied = {(r["file_id"], r["a_index"]) for r in recs if r["event"] == "applied"}
    befores = [r for r in recs if r["event"] == "before" and (r["file_id"], r["a_index"]) in applied]
    if not befores:
        return {"ok": False, "error": "nothing to undo"}
    done, failed = 0, []
    for r in befores:
        path = r["path"]
        try:
            st = os.stat(path)
            method = "mkvpropedit" if path.lower().endswith(MATROSKA) and st.st_nlink == 1 else "copy"
            ok, msg = _write(path, r["a_index"], r["old"] if r["old"] != "und" else "und", method, st.st_size)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        _log({"ts": time.time(), "action_id": action_id, "file_id": r["file_id"], "path": path,
              "a_index": r["a_index"], "event": "undone" if ok else "undo_failed", "message": msg})
        if ok:
            done += 1
            db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?, 'quick', 'retag', ?)",
                 (r["file_id"], time.time()))
        else:
            failed.append(f"{os.path.basename(path)}: {msg}")
    db.x("UPDATE actions SET status='undone', result=?, updated_at=? WHERE id=?",
         (f"Undone: restored {done} track(s)" + (f", {len(failed)} failed" if failed else ""), time.time(), action_id))
    db.log(f"Retag action {action_id} undone: {done} restored, {len(failed)} failed", "warning" if failed else "info")
    return {"ok": not failed, "restored": done, "failed": failed}
