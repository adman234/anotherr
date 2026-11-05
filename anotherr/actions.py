"""Fix-up actions: Bazarr re-subtitle, Sonarr/Radarr blocklist + redownload. Automation is opt-in."""
from __future__ import annotations

import logging
import time

from . import arr, db, langs, settings
from .schedule_util import in_windows

log = logging.getLogger("anotherr.actions")
TRACK_KINDS = ("retag_audio", "set_default_audio", "set_default_subtitle")  # run by trackedit
MANUAL_KINDS = ("resub", "redownload", "whisper_subtitle")  # what a person can propose from a file/issue
REARM_AFTER = 7 * 86400


def propose_for_file(file_id: int) -> list[int]:
    """Create action proposals from a file's open issues (deduplicated)."""
    issues = db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (file_id,))
    codes = {i["code"] for i in issues}
    created = []
    # don't re-propose a fix that already ran in the last week (avoids resub -> rescan -> resub loops)
    recent = {r["kind"] for r in db.q("SELECT kind FROM actions WHERE file_id=? AND status IN ('done','failed') "
                                      "AND updated_at > ?", (file_id, time.time() - REARM_AFTER))}
    if "resub" in recent and _rearmed(file_id):
        recent.discard("resub")
    redl = sorted(codes & set(settings.REDOWNLOAD_CODES))
    resub = sorted(codes & set(settings.RESUB_CODES))
    if redl and "redownload" not in recent:
        created += [_propose(file_id, "redownload", "Issues: " + ", ".join(redl), redl)]
    elif resub and not redl and "resub" not in recent:  # a redownload would replace the subs anyway
        item_id = db.scalar("SELECT item_id FROM files WHERE id=?", (file_id,))
        note = (" - Bazarr found nothing useful for this series before; consider Bazarr's Whisper provider "
                "(translate) or the release's official subtitles" if item_id and resub_failed_before(item_id) else "")
        created += [_propose(file_id, "resub", "Issues: " + ", ".join(resub) + note, resub)]
    created = [c for c in created if c]
    if created:
        maybe_auto(created)
    return created


def reconcile(file_id: int | None = None) -> int:
    """Withdraw proposals whose triggering issues are no longer open (they'd replace a file that's now fine)."""
    sql = "SELECT * FROM actions WHERE status='proposed'" + (" AND file_id=?" if file_id else "")
    n = 0
    for a in db.q(sql, (file_id,) if file_id else ()):
        if a["kind"] in TRACK_KINDS:
            continue  # batches are rebuilt (and withdrawn when empty) by their proposal refresh
        f = db.q1("SELECT missing FROM files WHERE id=?", (a["file_id"],))
        if not f or f["missing"]:
            why = "the file is no longer in the library"
        else:
            codes = db.unj(a["codes"], []) or []
            if not codes or _still_open(a["file_id"], codes):
                continue
            why = "the issues behind it are no longer open"
        db.x("UPDATE actions SET status='withdrawn', result=?, updated_at=? WHERE id=?", (f"Withdrawn: {why}", time.time(), a["id"]))
        n += 1
    return n


def _still_open(file_id: int, codes: list[str]) -> bool:
    return bool(db.q1(f"SELECT id FROM issues WHERE file_id=? AND status='open' AND code IN ({','.join('?' * len(codes))})",
                      [file_id, *codes]))


def _rearm_point(file_id: int) -> float:
    """When something changed that makes a new Bazarr search worthwhile: the audio language was retagged (Bazarr
    picks subtitles by it) or a Whisper subtitle was fetched."""
    return db.scalar("SELECT MAX(updated_at) FROM actions a WHERE status='done' AND kind IN ('retag_audio','whisper_subtitle') "
                     "AND (file_id=? OR payload LIKE ?)", (file_id, f'%"file_id": {int(file_id)},%')) or 0


def _rearmed(file_id: int) -> bool:
    last = db.scalar("SELECT MAX(updated_at) FROM actions WHERE file_id=? AND kind='resub' AND status IN ('done','failed')",
                     (file_id,)) or 0
    return _rearm_point(file_id) > last


def resub_failed_before(item_id: int) -> bool:
    """A Bazarr search already ran for this series and the rescan afterwards still found the same problem.
    Re-arms after 7 days, after a retag of that file's audio, or after a Whisper subtitle fetch."""
    for a in db.q("""SELECT a.file_id, a.updated_at, a.codes FROM actions a JOIN files f ON f.id=a.file_id
                     WHERE f.item_id=? AND a.kind='resub' AND a.status='done' AND a.updated_at > ?""",
                  (item_id, time.time() - REARM_AFTER)):
        if _rearm_point(a["file_id"]) > a["updated_at"]:
            continue
        codes = db.unj(a["codes"], []) or list(settings.RESUB_CODES)
        if db.q1(f"SELECT id FROM issues WHERE file_id=? AND status='open' AND created_at > ? AND code IN "
                 f"({','.join('?' * len(codes))})", [a["file_id"], a["updated_at"] + 60, *codes]):
            return True
    return False


def _propose(file_id: int, kind: str, reason: str, codes: list[str], auto: bool = False) -> int | None:
    if db.q1("SELECT id FROM actions WHERE file_id=? AND kind=? AND status IN ('proposed','running')", (file_id, kind)):
        return None
    now = time.time()
    return db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (file_id, kind, reason, db.j(codes), "proposed", int(auto), now, now))


def propose(file_id: int, kind: str, reason: str, codes: list[str] | None = None) -> int | None:
    return _propose(file_id, kind, reason, codes or [])


def whisper_blocker(file_id: int) -> str | None:
    """Why a Whisper subtitle can't be requested for this file (None = it can)."""
    f = db.q1("SELECT f.episodes, f.audio_evidence, i.source FROM files f JOIN items i ON i.id=f.item_id WHERE f.id=?",
              (file_id,))
    if not f:
        return "file not found"
    if f["source"] == "sonarr" and len(db.unj(f["episodes"], []) or []) != 1:
        return "Whisper subtitles are requested for one episode at a time; this file holds several (or none)"
    ev = db.unj(f.get("audio_evidence"), {}) or {}
    tracks = ev.get("tracks") or []
    cur = next((t for t in tracks if t.get("default")), tracks[0] if tracks else None)
    sub = langs.norm(settings.get()["languages"]["subtitle_language"]) or "eng"
    tag = langs.norm((cur or {}).get("tag"))
    if not tag or tag == "und":
        return "the default audio track has no language tag - Bazarr's Whisper provider needs it (retag it first)"
    if tag == sub:
        return f"the audio is already {langs.name(sub)} - Whisper would only transcribe, not translate"
    return None


def _eligible_auto(a: dict, cfg: dict) -> bool:
    acfg = cfg["automation"]
    if a["kind"] in TRACK_KINDS:
        return False  # edits media files: always approved by a person
    if a["kind"] == "resub":
        item_id = db.scalar("SELECT item_id FROM files WHERE id=?", (a["file_id"],))
        return bool(acfg["auto_resub"]) and not (item_id and resub_failed_before(item_id))
    if a["kind"] == "redownload":
        codes = set(db.unj(a["codes"], []))
        return bool(acfg["auto_redownload"]) and bool(codes & set(acfg["redownload_codes"]))
    return False


def automation_active(cfg: dict | None = None) -> bool:
    cfg = cfg or settings.get()
    acfg = cfg["automation"]
    return bool(acfg["enabled"]) and in_windows(acfg.get("windows") or [])


def actions_today() -> int:
    t = time.localtime()
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return db.scalar("SELECT COUNT(*) FROM actions WHERE auto=1 AND status IN ('done','failed','running') "
                     "AND updated_at>=?", (midnight,)) or 0


def maybe_auto(ids: list[int] | None = None) -> int:
    """Execute eligible proposals when automation is enabled and inside its schedule."""
    cfg = settings.get()
    if not automation_active(cfg):
        return 0
    budget = int(cfg["automation"]["max_actions_per_day"]) - actions_today()
    if budget <= 0:
        return 0
    reconcile()
    rows = db.q("SELECT a.*, f.item_id FROM actions a JOIN files f ON f.id=a.file_id WHERE a.status='proposed' "
                "ORDER BY a.created_at")
    if ids:
        rows = [r for r in rows if r["id"] in ids]
    t = time.localtime()
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    used = {r["item_id"] for r in db.q("SELECT DISTINCT f.item_id FROM actions a JOIN files f ON f.id=a.file_id "
                                      "WHERE a.auto=1 AND a.updated_at >= ?", (midnight,))}
    done = 0
    for a in rows:
        if done >= budget:
            break
        if a["item_id"] in used:  # one automatic action per series per day, so one show can't use the quota
            continue
        if _eligible_auto(a, cfg):
            used.add(a["item_id"])
            db.x("UPDATE actions SET auto=1 WHERE id=?", (a["id"],))
            execute(a["id"])
            done += 1
    return done


def reject(action_id: int):
    db.x("UPDATE actions SET status='rejected', updated_at=? WHERE id=? AND status='proposed'", (time.time(), action_id))


GROUPABLE = ("resub", "redownload")  # one approval per series + season + identical reason (UI/MCP)


def approve_many(ids: list[int]) -> dict:
    """Approve a group of proposals: they're marked 'queued' at once and run one after another in the background
    (a Bazarr search can take minutes per episode). Track-edit batches and Whisper keep their own approval."""
    import threading

    claimed = []
    for i in dict.fromkeys(int(x) for x in ids):
        if db.n(f"UPDATE actions SET status='queued', result='queued', updated_at=? WHERE id=? AND status='proposed' "
                f"AND kind IN ({','.join('?' * len(GROUPABLE))})", (time.time(), i, *GROUPABLE)):
            claimed.append(i)
    if claimed:
        threading.Thread(target=_run_queued, args=(claimed,), name="anotherr-approve-many", daemon=True).start()
    return {"ok": bool(claimed), "queued": len(claimed), "skipped": len(ids) - len(claimed)}


def _run_queued(ids: list[int]) -> None:
    ok = failed = 0
    for i in ids:
        try:
            r = execute(i)
        except Exception as e:  # noqa: BLE001
            db.x("UPDATE actions SET status='failed', result=?, updated_at=? WHERE id=? AND status IN ('queued','running')",
                 (f"crashed: {e}", time.time(), i))
            r = {"ok": False}
        ok, failed = ok + bool(r.get("ok")), failed + (not r.get("ok"))
    db.log(f"Group approval: {ok} of {len(ids)} action(s) done" + (f", {failed} failed or withdrawn" if failed else ""),
           "info" if not failed else "warning")


def execute(action_id: int) -> dict:
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not a or a["status"] not in ("proposed", "failed", "queued"):
        return {"ok": False, "error": "action not runnable"}
    if a["kind"] in TRACK_KINDS:
        from . import trackedit

        return trackedit.start(action_id)
    codes = db.unj(a["codes"], []) or []
    if codes and not _still_open(a["file_id"], codes):
        db.x("UPDATE actions SET status='withdrawn', result=?, updated_at=? WHERE id=?",
             ("Withdrawn: the issues behind it are no longer open", time.time(), action_id))
        return {"ok": False, "error": "withdrawn - the issues behind this action have since been resolved"}
    db.x("UPDATE actions SET status='running', updated_at=? WHERE id=?", (time.time(), action_id))
    f = db.q1("SELECT * FROM files WHERE id=?", (a["file_id"],))
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],)) if f else None
    try:
        if not f or not item:
            raise RuntimeError("file no longer in library")
        if a["kind"] == "whisper_subtitle":
            return _start_whisper(action_id, f, item)
        if a["kind"] == "resub":
            msg = _resub(f, item)
        elif a["kind"] == "redownload":
            msg = _redownload(f, item)
        else:
            raise RuntimeError(f"unknown action {a['kind']}")
        status = "done"
    except Exception as e:  # noqa: BLE001
        msg, status = str(e), "failed"
    db.x("UPDATE actions SET status=?, result=?, updated_at=? WHERE id=?", (status, msg, time.time(), action_id))
    label = f"{item['title']} {f.get('label', '')}" if item and f else f"file {a['file_id']}"
    db.log(f"Action {a['kind']} on {label}: {status} - {msg}", "info" if status == "done" else "error")
    if status == "done" and a["kind"] == "resub":
        # re-check subtitles after Bazarr had a moment to write them
        db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?,?,?,?)",
             (f["id"], "standard", "action", time.time() + 120))
    return {"ok": status == "done", "result": msg}


def _start_whisper(action_id: int, f: dict, item: dict) -> dict:
    """Bazarr transcribes while it 'downloads' the Whisper result, which can take a long time: run it in a thread."""
    import threading

    why = whisper_blocker(f["id"])
    if why:
        raise RuntimeError(why)
    db.x("UPDATE actions SET result=?, updated_at=? WHERE id=?", ("asking Bazarr's Whisper provider", time.time(), action_id))
    threading.Thread(target=_whisper_run, args=(action_id, f, item), name=f"whisper-{action_id}", daemon=True).start()
    return {"ok": True, "result": "started - Bazarr is transcribing (this can take several minutes)"}


def _whisper_run(action_id: int, f: dict, item: dict) -> None:
    label = f"{item['title']} {f.get('label', '')}"
    try:
        b = arr.Bazarr()
        sub2 = langs.iso1(settings.get()["languages"]["subtitle_language"]) or "en"
        if item["source"] == "sonarr":
            ep = (db.unj(f["episodes"], []) or [{}])[0]
            res = b.provider_results(episode_id=ep["id"])
        else:
            res = b.provider_results(radarr_id=item["arr_id"])
        wh = [r for r in res if "whisper" in str(r.get("provider", "")).lower()]
        if not wh:
            raise RuntimeError("Bazarr returned no Whisper result - is the Whisper provider enabled in Bazarr "
                               "(Settings > Providers > Whisper) and the subtitle language in the series' profile?")
        pick = next((r for r in wh if (langs.iso1(r.get("language") or "") or str(r.get("language", ""))[:2]) == sub2), wh[0])
        if item["source"] == "sonarr":
            b.provider_download(pick, series_id=item["arr_id"], episode_id=ep["id"])
        else:
            b.provider_download(pick, radarr_id=item["arr_id"])
        msg, status = f"Bazarr Whisper subtitle ({pick.get('language') or sub2}) downloaded", "done"
    except Exception as e:  # noqa: BLE001
        msg, status = str(e), "failed"
    db.x("UPDATE actions SET status=?, result=?, updated_at=? WHERE id=? AND status='running'",
         (status, msg, time.time(), action_id))
    db.log(f"Action whisper_subtitle on {label}: {status} - {msg}", "info" if status == "done" else "error")
    if status == "done":
        db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?,?,?,?)",
             (f["id"], "standard", "action", time.time() + 120))


def _resub(f: dict, item: dict) -> str:
    b = arr.Bazarr()
    lang2 = langs.iso1(settings.get()["languages"]["subtitle_language"]) or "en"
    if item["source"] == "sonarr":
        eps = db.unj(f["episodes"], [])
        for e in eps:
            b.search_episode(item["arr_id"], e["id"], lang2)
        return f"Bazarr subtitle search ({lang2}) triggered for {len(eps)} episode(s)"
    b.search_movie(item["arr_id"], lang2)
    return f"Bazarr subtitle search ({lang2}) triggered"


def _redownload(f: dict, item: dict) -> str:
    delete = settings.get()["automation"].get("redownload_delete_file", True)
    notes = []
    if item["source"] == "sonarr":
        s = arr.Sonarr()
        eps = db.unj(f["episodes"], [])
        failed = set()
        packs = 0
        for e in eps:
            hist = s.grab_history(item["arr_id"], e["id"])
            if not hist or hist[0]["id"] in failed:
                continue
            if str((hist[0].get("data") or {}).get("fullSeason", "")).lower() == "true":
                packs += 1  # never blocklist a whole season pack because of one episode
                continue
            s.mark_failed(hist[0]["id"])
            failed.add(hist[0]["id"])
        notes.append(f"blocklisted {len(failed)} release(s)" if failed else "no grab history (not blocklisted)")
        if packs:
            notes.append("season pack not blocklisted")
        if delete:
            try:
                s.delete_file(f["arr_file_id"])
                notes.append("deleted file")
            except arr.ServiceError as ex:
                notes.append(f"delete failed: {ex}")
        s.search([e["id"] for e in eps])
        notes.append("search triggered")
    else:
        r = arr.Radarr()
        hist = r.grab_history(item["arr_id"])
        if hist:
            r.mark_failed(hist[0]["id"])
            notes.append("blocklisted release")
        else:
            notes.append("no grab history (not blocklisted)")
        if delete:
            try:
                r.delete_file(f["arr_file_id"])
                notes.append("deleted file")
            except arr.ServiceError as ex:
                notes.append(f"delete failed: {ex}")
        r.search([item["arr_id"]])
        notes.append("search triggered")
    return "; ".join(notes)
