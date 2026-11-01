"""Fix-up actions: Bazarr re-subtitle, Sonarr/Radarr blocklist + redownload. Automation is opt-in."""
from __future__ import annotations

import logging
import time

from . import arr, db, langs, settings
from .schedule_util import in_windows

log = logging.getLogger("anotherr.actions")


def propose_for_file(file_id: int) -> list[int]:
    """Create action proposals from a file's open issues (deduplicated)."""
    issues = db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (file_id,))
    codes = {i["code"] for i in issues}
    created = []
    # don't re-propose a fix that already ran in the last week (avoids resub -> rescan -> resub loops)
    recent = {r["kind"] for r in db.q("SELECT kind FROM actions WHERE file_id=? AND status IN ('done','failed') "
                                      "AND updated_at > ?", (file_id, time.time() - 7 * 86400))}
    redl = sorted(codes & set(settings.REDOWNLOAD_CODES))
    resub = sorted(codes & set(settings.RESUB_CODES))
    if redl and "redownload" not in recent:
        created += [_propose(file_id, "redownload", "Issues: " + ", ".join(redl), redl)]
    elif resub and not redl and "resub" not in recent:  # a redownload would replace the subs anyway
        created += [_propose(file_id, "resub", "Issues: " + ", ".join(resub), resub)]
    created = [c for c in created if c]
    if created:
        maybe_auto(created)
    return created


def _propose(file_id: int, kind: str, reason: str, codes: list[str], auto: bool = False) -> int | None:
    if db.q1("SELECT id FROM actions WHERE file_id=? AND kind=? AND status IN ('proposed','running')", (file_id, kind)):
        return None
    now = time.time()
    return db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (file_id, kind, reason, db.j(codes), "proposed", int(auto), now, now))


def propose(file_id: int, kind: str, reason: str) -> int | None:
    return _propose(file_id, kind, reason, [])


def _eligible_auto(a: dict, cfg: dict) -> bool:
    acfg = cfg["automation"]
    if a["kind"] == "resub":
        return bool(acfg["auto_resub"])
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
    rows = db.q("SELECT * FROM actions WHERE status='proposed' ORDER BY created_at")
    if ids:
        rows = [r for r in rows if r["id"] in ids]
    done = 0
    for a in rows:
        if done >= budget:
            break
        if _eligible_auto(a, cfg):
            db.x("UPDATE actions SET auto=1 WHERE id=?", (a["id"],))
            execute(a["id"])
            done += 1
    return done


def reject(action_id: int):
    db.x("UPDATE actions SET status='rejected', updated_at=? WHERE id=? AND status='proposed'", (time.time(), action_id))


def execute(action_id: int) -> dict:
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not a or a["status"] not in ("proposed", "failed"):
        return {"ok": False, "error": "action not runnable"}
    db.x("UPDATE actions SET status='running', updated_at=? WHERE id=?", (time.time(), action_id))
    f = db.q1("SELECT * FROM files WHERE id=?", (a["file_id"],))
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],)) if f else None
    try:
        if not f or not item:
            raise RuntimeError("file no longer in library")
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
