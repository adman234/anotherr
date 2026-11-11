"""Fix-up actions: Bazarr re-subtitle, Sonarr/Radarr blocklist + redownload. Automation is opt-in."""
from __future__ import annotations

import logging
import os
import time

from . import arr, db, langs, settings
from .schedule_util import in_windows

log = logging.getLogger("anotherr.actions")
TRACK_KINDS = ("retag_audio", "set_default_audio", "set_default_subtitle")  # run by trackedit
BATCH_KINDS = TRACK_KINDS + ("subgen_transcribe", "accept_season_audio")  # payload batches, rebuilt by their own refresh
MANUAL_KINDS = ("resub", "redownload", "whisper_subtitle", "accept_season_audio")  # what a person can propose from a file/issue
REARM_AFTER = 7 * 86400


def propose_for_file(file_id: int) -> list[int]:
    """Create action proposals from a file's open issues (deduplicated)."""
    issues = db.q("SELECT code, severity, details FROM issues WHERE file_id=? AND status='open'", (file_id,))
    codes = {i["code"] for i in issues}
    created = []
    # don't re-propose a fix that already ran in the last week (avoids resub -> rescan -> resub loops)
    recent = {r["kind"] for r in db.q("SELECT kind FROM actions WHERE file_id=? AND status IN ('done','failed') "
                                      "AND updated_at > ?", (file_id, time.time() - REARM_AFTER))}
    if "resub" in recent and _rearmed(file_id):
        recent.discard("resub")
    # a blue dual-audio gap only asks for a redownload when that language was requested (older issues have no flag)
    unasked = {i["code"] for i in issues if i["severity"] == "dual" and db.unj(i["details"], {}).get("requested") is False}
    redl = sorted((codes - unasked) & set(settings.REDOWNLOAD_CODES))
    resub = sorted(codes & set(settings.RESUB_CODES))
    if redl and "redownload" not in recent:
        created += [_propose(file_id, "redownload", "Issues: " + ", ".join(redl), redl)]
    elif resub and not redl:  # a redownload would replace the subs anyway
        item_id = db.scalar("SELECT item_id FROM files WHERE id=?", (file_id,))
        from . import subgen

        kind, reason = ("subgen", "") if subgen.wanted(file_id) else _sub_fix(file_id, item_id, resub)
        if kind not in ("subgen", "skip") and kind not in recent:
            created += [_propose(file_id, kind, reason, resub)]
    created = [c for c in created if c]
    if created:
        maybe_auto(created)
    return created


def reconcile(file_id: int | None = None) -> int:
    """Withdraw proposals whose triggering issues are no longer open (they'd replace a file that's now fine)."""
    sql = "SELECT * FROM actions WHERE status='proposed'" + (" AND file_id=?" if file_id else "")
    n = 0
    for a in db.q(sql, (file_id,) if file_id else ()):
        if a["kind"] in BATCH_KINDS:
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


WHISPER_CODES = ("subs_missing", "subs_signs_only")  # no usable full subtitle: a generated one fills the gap


def _sub_fix(file_id: int, item_id: int | None, codes: list[str]) -> tuple[str, str]:
    """Bazarr resub - or, once Bazarr's normal search came up empty for this series, a Whisper subtitle.
    With subgen set up the Whisper step is subgen's own proposal (subgen.refresh_item), never a Bazarr one."""
    from . import subgen

    base = "Issues: " + ", ".join(codes)
    if not (item_id and resub_failed_before(item_id)):
        return "resub", base
    if subgen.configured():
        if not set(codes) & set(WHISPER_CODES):
            return "resub", base + " - Bazarr found nothing useful for this series before"
        return "skip", ""  # subgen's refresh proposes (or explains why not) for eligible files
    if not set(codes) & set(WHISPER_CODES):
        return "resub", base + " - Bazarr found nothing useful for this series before"
    if db.q1("SELECT id FROM actions WHERE file_id=? AND kind='whisper_subtitle' AND status IN ('done','failed')",
             (file_id,)):
        return "skip", ""  # a Whisper request already ran for this file: don't repeat it (subgen can, if set up)
    why = whisper_blocker(file_id)
    if why:
        return "resub", base + f" - Bazarr found nothing useful for this series before; Whisper can't be used yet: {why}"
    return "whisper_subtitle", (base + " - Bazarr's search found nothing for this series before, so this asks Bazarr's "
                                       "Whisper provider (subgen) to transcribe + translate")


def upgrade_resubs_to_whisper(item_id: int | None = None) -> int:
    """Swap open resub proposals for Whisper ones where another plain search is known to be pointless."""
    n = 0
    sql = ("SELECT a.id, a.file_id, a.codes, f.item_id FROM actions a JOIN files f ON f.id=a.file_id "
           "WHERE a.kind='resub' AND a.status='proposed'" + (" AND f.item_id=?" if item_id else ""))
    from . import subgen

    for a in db.q(sql, (item_id,) if item_id else ()):
        codes = db.unj(a["codes"], []) or []
        if subgen.wanted(a["file_id"]):
            continue  # subgen's own refresh replaces it
        kind, reason = _sub_fix(a["file_id"], a["item_id"], codes)
        if kind != "whisper_subtitle":
            continue
        if _propose(a["file_id"], kind, reason, codes) or db.q1(
                "SELECT id FROM actions WHERE file_id=? AND kind='whisper_subtitle' AND status IN ('proposed','queued','running')",
                (a["file_id"],)):
            db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: replaced by a Whisper subtitle proposal "
                 "(Bazarr found nothing for this series before)', updated_at=? WHERE id=? AND status='proposed'",
                 (time.time(), a["id"]))
            n += 1
    return n


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
    if db.q1("SELECT id FROM actions WHERE file_id=? AND kind=? AND status IN ('proposed','queued','running')", (file_id, kind)):
        return None
    now = time.time()
    return db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (file_id, kind, reason, db.j(codes), "proposed", int(auto), now, now))


def propose(file_id: int, kind: str, reason: str, codes: list[str] | None = None) -> int | None:
    return _propose(file_id, kind, reason, codes or [])


def whisper_blocker(file_id: int) -> str | None:
    """Why a Whisper subtitle can't be requested for this file (None = it can)."""
    from .retag import _evidence

    f = db.q1("SELECT f.episodes, f.audio_evidence, f.last_scan_id, i.source FROM files f JOIN items i ON i.id=f.item_id "
              "WHERE f.id=?", (file_id,))
    if not f:
        return "file not found"
    if f["source"] == "sonarr" and len(db.unj(f["episodes"], []) or []) != 1:
        return "Whisper subtitles are requested for one episode at a time; this file holds several (or none)"
    sub = langs.norm(settings.get()["languages"]["subtitle_language"]) or "eng"
    ev = _evidence(f) or {}  # also reads scans from before the evidence column
    streams = _last_streams(f)
    subs = ev.get("subs") or []
    if any(langs.norm(s.get("lang")) == sub and s.get("kind") == "full" for s in subs) or \
            (not subs and any(langs.norm(s.get("lang")) == sub and not s.get("forced") for s in streams.get("subs") or [])):
        return f"it already has {langs.name(sub)} subtitles - no Whisper subtitle needed"
    # the tag as ffprobe reported it in the last scan (older evidence records may lack it)
    audio = streams.get("audio") or ev.get("tracks") or []
    cur = next((t for t in audio if t.get("default")), audio[0] if audio else None)
    tag = langs.norm((cur or {}).get("lang") or (cur or {}).get("tag"))
    if not cur:
        return "no audio track information yet - rescan the file"
    if not tag or tag == "und":
        return "the default audio track has no language tag - Bazarr's Whisper provider needs it (retag it first)"
    if tag == sub:
        return f"the audio is already {langs.name(sub)} - Whisper would only transcribe, not translate"
    return None


def _last_streams(f: dict) -> dict:
    if not f.get("last_scan_id"):
        return {}
    res = db.unj(db.scalar("SELECT result FROM scans WHERE id=?", (f["last_scan_id"],)), {}) or {}
    return res.get("streams") or {}


def _eligible_auto(a: dict, cfg: dict) -> bool:
    acfg = cfg["automation"]
    if a["kind"] in BATCH_KINDS:
        return False  # edits media files / long GPU-CPU jobs: always approved by a person
    if a["kind"] == "resub":
        item_id = db.scalar("SELECT item_id FROM files WHERE id=?", (a["file_id"],))
        return bool(acfg["auto_resub"]) and not (item_id and resub_failed_before(item_id))
    if a["kind"] == "redownload":
        codes = set(db.unj(a["codes"], [])) & set(acfg["redownload_codes"])
        return bool(acfg["auto_redownload"]) and bool(codes - _acceptable_language_gaps(a["file_id"], codes))
    return False


def _acceptable_language_gaps(file_id: int, codes: set[str]) -> set[str]:
    """Language codes that are only warnings for this file (the other language is there and a season exception
    could accept it). A person decides those; automation only redownloads files with no usable audio at all."""
    lang = codes & {"original_audio_missing", "english_audio_missing"}
    if not lang:
        return set()
    errors = {r["code"] for r in db.q(
        f"SELECT code FROM issues WHERE file_id=? AND status='open' AND severity IN ('error','critical') "
        f"AND code IN ({','.join('?' * len(lang))})", [file_id, *lang])}
    return lang - errors


def is_dual(a: dict) -> bool:
    """Proposals that belong on the Dual actions tab: season-audio accepts, default-track changes the planner
    marked as a dual-audio choice (English vs original language, see defaults.is_dual_choice), and redownloads
    raised only for single-language gaps (the other language is there). A redownload for a file with no usable
    audio stays main."""
    from . import audio_request

    if a.get("kind") == audio_request.KIND:
        return True
    if a.get("kind") in ("set_default_audio", "set_default_subtitle"):
        p = a.get("payload")
        return bool((db.unj(p, {}) if isinstance(p, str) else p or {}).get("dual"))
    codes = set(a.get("codes") or [])
    if a.get("kind") != "redownload" or not codes or not codes <= set(audio_request.ALLOW_FOR_CODE):
        return False
    return _acceptable_language_gaps(a["file_id"], codes) == codes


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


GROUPABLE = ("resub", "redownload", "whisper_subtitle")  # one approval per series + season + identical reason (UI/MCP)


# ---- alternative proposals: several actions that fix the same files in different ways are one "choice"

def _lang_choice_key(a: dict) -> str | None:
    """accept_season_audio and the redownloads raised only for the codes it covers (english_audio_missing /
    original_audio_missing) share an item + season (NULL season = movie)."""
    from . import audio_request

    covered = set(audio_request.ALLOW_FOR_CODE)
    if a.get("kind") == audio_request.KIND:
        p = a.get("payload") or {}
        if not p.get("item_id"):
            return None
        season = p.get("season")
    elif a.get("kind") == "redownload":
        codes = set(a.get("codes") or [])
        if not codes or not codes <= covered or not a.get("item_id"):
            return None
        season = a.get("season")
        p = {"item_id": a["item_id"]}
    else:
        return None
    return f"lang|{p['item_id']}|{'movie' if season is None else season}"


CHOICE_RULES = (_lang_choice_key,)  # add further alternative pairings here
CHOICE_LABELS = {"accept_season_audio": "Accept season audio as is", "redownload": "Redownload"}


def choice_key(a: dict) -> str | None:
    """Stable key shared by proposals that are alternatives for the same files. `a` is an /api/actions row
    (codes and payload decoded, item_id and season present). None when the action is not part of a choice."""
    for rule in CHOICE_RULES:
        k = rule(a)
        if k:
            return k
    return None


def attach_choice_keys(rows: list[dict]) -> None:
    """Set row['choice_key'] on proposed rows, only where at least two different kinds compete."""
    keys: dict[str, set[str]] = {}
    for r in rows:
        k = choice_key(r) if r.get("status") == "proposed" else None
        r["choice_key"] = k
        if k:
            keys.setdefault(k, set()).add(r["kind"])
    for r in rows:
        if r["choice_key"] and len(keys[r["choice_key"]]) < 2:
            r["choice_key"] = None


def choose(rows: list[dict], key: str, pick: str) -> dict | None:
    """Approve every proposed action of kind `pick` in choice `key` and reject the alternatives. `rows` are the
    proposed /api/actions rows with choice keys attached. None if the choice no longer exists."""
    group = [r for r in rows if r.get("choice_key") == key]
    if not group:
        return None
    if pick == SHOW_PICK:
        return _accept_show(group)
    if pick not in {r["kind"] for r in group}:
        raise ValueError("pick must be one of: " + ", ".join(sorted({r["kind"] for r in group})))
    label = CHOICE_LABELS.get(pick, pick)
    rejected = []
    for r in group:
        if r["kind"] != pick and db.n("UPDATE actions SET status='rejected', result=?, updated_at=? "
                                      "WHERE id=? AND status='proposed'",
                                      (f"Rejected: chose {label} instead", time.time(), r["id"])):
            rejected.append(r["id"])
    ids = [r["id"] for r in group if r["kind"] == pick]
    if pick in GROUPABLE:
        out = approve_many(ids)
        approved = ids if out.get("queued") else []
        res = {"ok": out["ok"], "queued": out["queued"]}
    else:
        res = {"ok": True, "results": {i: execute(i) for i in ids}}
        approved = [i for i in ids if res["results"][i].get("ok")]
    return {**res, "pick": pick, "approved": approved, "rejected": rejected}


SHOW_PICK = "accept_show_audio"  # extra pick on a language choice: accept every season of the series as is


def _accept_show(group: list[dict]) -> dict:
    from . import audio_request

    accept = [r for r in group if r["kind"] == audio_request.KIND]
    item_id = group[0].get("item_id")
    item = db.q1("SELECT kind FROM items WHERE id=?", (item_id,)) if item_id else None
    if not accept or not item or item["kind"] != "series":
        raise ValueError("accept_show_audio only applies to a series with an accept-as-is proposal")
    out = {"ok": True, "pick": SHOW_PICK, "approved": [], "rejected": [], "exception_ids": []}
    for allow in sorted({(r.get("payload") or {}).get("allow") for r in accept}):
        r = audio_request.accept_show(item_id, allow)
        out["rejected"] += r["rejected"]
        out["exception_ids"].append(r["exception_id"])
        out["result"] = r["result"]
    return out


def approve_many(ids: list[int]) -> dict:
    """Approve a group of proposals: they're marked 'queued' at once and run one after another in the background
    (a Bazarr search or Whisper transcription can take minutes per episode). Track-edit batches keep their own."""
    import threading

    claimed = []
    for i in dict.fromkeys(int(x) for x in ids):
        if db.n(f"UPDATE actions SET status='queued', result='queued', updated_at=? WHERE id=? AND status='proposed' "
                f"AND kind IN ({','.join('?' * len(GROUPABLE))})", (time.time(), i, *GROUPABLE)):
            claimed.append(i)
    if claimed:
        threading.Thread(target=_run_queued, args=(claimed,), name="anotherr-approve-many", daemon=True).start()
    return {"ok": bool(claimed), "queued": len(claimed), "skipped": len(ids) - len(claimed)}


def english_retag_ids(min_conf: float | None = None) -> list[int]:
    """Proposed retag_audio batches where every file is being tagged English (untagged or wrong tag) with confidence
    above `min_conf` (default settings retag.bulk_min_confidence) and nothing flagged for review or mismatch."""
    from . import settings

    if min_conf is None:
        min_conf = float(settings.get()["retag"].get("bulk_min_confidence", 0.95))
    out = []
    for a in db.q("SELECT id, payload FROM actions WHERE kind='retag_audio' AND status='proposed'"):
        files = (db.unj(a["payload"], {}) or {}).get("files") or []
        if files and all(f.get("new") == "eng" and (f.get("conf") or 0) > min_conf and not f.get("review")
                         and not f.get("mismatch") for f in files):
            out.append(a["id"])
    return out


def subs_out_of_sync_ids() -> list[int]:
    """Proposed resub actions raised for out-of-sync subtitles (not the 'nothing useful' ones)."""
    out = []
    for a in db.q("SELECT id, codes, reason FROM actions WHERE kind='resub' AND status='proposed'"):
        if "subs_out_of_sync" in (db.unj(a["codes"], []) or []) and "nothing useful" not in (a["reason"] or ""):
            out.append(a["id"])
    return out


def english_default_audio_ids() -> list[int]:
    """Proposed set_default_audio batches on the main tab (not dual-audio choices) where every file's new default
    audio track is English."""
    from . import defaults

    out = []
    for a in db.q("SELECT id, kind, payload FROM actions WHERE kind='set_default_audio' AND status='proposed'"):
        p = db.unj(a["payload"], {}) or {}
        files = p.get("files") or []
        if not p.get("dual") and files and all(
                defaults._same((f.get("audio") or {}).get("to_lang"), "eng") for f in files):
            out.append(a["id"])
    return out


def foreign_default_ids() -> list[int]:
    """Proposed default-track batches on the main tab (not dual-audio choices) where every file's change takes the
    default away from a track that is neither English nor the original language: a foreign default audio track,
    or a foreign default subtitle track."""
    from . import defaults

    def foreign(lang, orig) -> bool:
        return bool(lang) and not defaults._same(lang, "eng") and not (orig and defaults._same(lang, orig))

    out = []
    for a in db.q("SELECT id, kind, payload FROM actions WHERE kind IN ('set_default_audio','set_default_subtitle') "
                  "AND status='proposed'"):
        p = db.unj(a["payload"], {}) or {}
        files, orig = p.get("files") or [], p.get("series_language") or ""
        if p.get("dual") or not files:
            continue
        key = "audio" if a["kind"] == "set_default_audio" else "sub"
        if all(foreign((f.get(key) or {}).get("from_lang"), orig) for f in files):
            out.append(a["id"])
    return out


def _approve_track_edits(ids: list[int], label: str) -> dict:
    """Queue track-edit proposals; they run one after another in the background."""
    import threading

    from . import settings, trackedit

    if not settings.get()["retag"].get("enabled"):
        return {"ok": False, "error": "track editing is turned off in Settings -> Retag audio", "queued": 0}
    claimed = [i for i in ids if db.n(
        "UPDATE actions SET status='running', result='queued', updated_at=? WHERE id=? AND status='proposed'",
        (time.time(), i))]

    def go():
        for i in claimed:
            try:
                trackedit.run(i)
            except Exception as e:  # noqa: BLE001
                db.x("UPDATE actions SET status='failed', result=?, updated_at=? WHERE id=? AND status='running'",
                     (f"crashed: {e}", time.time(), i))
        db.log(f"{label}: {len(claimed)} action(s) processed")

    if claimed:
        threading.Thread(target=go, name="anotherr-bulk-trackedit", daemon=True).start()
    return {"ok": True, "queued": len(claimed)}


def approve_english_retags(min_conf: float | None = None) -> dict:
    """Queue every qualifying English retag; they run one after another in the background."""
    return _approve_track_edits(english_retag_ids(min_conf), "Bulk English retag")


def approve_english_default_audio() -> dict:
    """Queue every non-dual proposal that makes the English track the default audio."""
    return _approve_track_edits(english_default_audio_ids(), "Bulk English default audio")


def approve_foreign_defaults() -> dict:
    """Queue every non-dual proposal that takes the default off a foreign (not English, not original) track."""
    return _approve_track_edits(foreign_default_ids(), "Bulk foreign default clear")


def _run_queued(ids: list[int]) -> None:
    ok = failed = 0
    for i in ids:
        try:
            r = execute(i, wait=True)
        except Exception as e:  # noqa: BLE001
            db.x("UPDATE actions SET status='failed', result=?, updated_at=? WHERE id=? AND status IN ('queued','running')",
                 (f"crashed: {e}", time.time(), i))
            r = {"ok": False}
        ok, failed = ok + bool(r.get("ok")), failed + (not r.get("ok"))
    db.log(f"Group approval: {ok} of {len(ids)} action(s) done" + (f", {failed} failed or withdrawn" if failed else ""),
           "info" if not failed else "warning")


def execute(action_id: int, wait: bool = False) -> dict:
    """Run one action. wait=True runs a Whisper request to the end in this thread (group approvals go one by one)."""
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not a or a["status"] not in ("proposed", "failed", "queued"):
        return {"ok": False, "error": "action not runnable"}
    if a["kind"] == "subgen_transcribe":
        from . import subgen

        return subgen.approve(action_id)
    if a["kind"] in TRACK_KINDS:
        from . import trackedit

        return trackedit.start(action_id)
    if a["kind"] == "accept_season_audio":
        from . import audio_request

        return audio_request.accept(action_id)
    codes = db.unj(a["codes"], []) or []
    if codes and not _still_open(a["file_id"], codes):
        db.x("UPDATE actions SET status='withdrawn', result=?, updated_at=? WHERE id=?",
             ("Withdrawn: the issues behind it are no longer open", time.time(), action_id))
        return {"ok": False, "error": "withdrawn - the issues behind this action have since been resolved"}
    db.x("UPDATE actions SET status='running', updated_at=? WHERE id=?", (time.time(), action_id))
    if a["kind"] == "redownload":
        from . import audio_request

        audio_request.on_redownload(a["file_id"], codes)
    f = db.q1("SELECT * FROM files WHERE id=?", (a["file_id"],))
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],)) if f else None
    try:
        if not f or not item:
            raise RuntimeError("file no longer in library")
        if a["kind"] == "whisper_subtitle":
            return _start_whisper(action_id, f, item, wait)
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


def _start_whisper(action_id: int, f: dict, item: dict, wait: bool = False) -> dict:
    """Bazarr transcribes while it 'downloads' the Whisper result, which can take a long time: run it in a thread."""
    import threading

    why = whisper_blocker(f["id"])
    if why:
        raise RuntimeError(why)
    db.x("UPDATE actions SET result=?, updated_at=? WHERE id=?", ("asking Bazarr's Whisper provider", time.time(), action_id))
    if wait:
        _whisper_run(action_id, f, item)
        return {"ok": db.scalar("SELECT status FROM actions WHERE id=?", (action_id,)) == "done"}
    threading.Thread(target=_whisper_run, args=(action_id, f, item), name=f"whisper-{action_id}", daemon=True).start()
    return {"ok": True, "result": "started - Bazarr is transcribing (this can take several minutes)"}


def _whisper_run(action_id: int, f: dict, item: dict) -> None:
    label = f"{item['title']} {f.get('label', '')}"
    started = time.time()
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
        # "done" means a subtitle is on disk, not that Bazarr accepted the request
        out = _wait_for_srt(action_id, f, started)
        if not out:
            raise RuntimeError(f"Bazarr accepted the Whisper request but no subtitle appeared within "
                               f"{WHISPER_SRT_WAIT // 60} minutes (check Bazarr's and subgen's logs)")
        msg, status = f"Bazarr Whisper subtitle written: {os.path.basename(out)}", "done"
    except Exception as e:  # noqa: BLE001
        msg, status = str(e), "failed"
    db.x("UPDATE actions SET status=?, result=?, updated_at=? WHERE id=? AND status='running'",
         (status, msg, time.time(), action_id))
    db.log(f"Action whisper_subtitle on {label}: {status} - {msg}", "info" if status == "done" else "error")
    if status == "done":
        db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?,?,?,?)",
             (f["id"], "standard", "action", time.time() + 120))


WHISPER_SRT_WAIT = 600


def _wait_for_srt(action_id: int, f: dict, since: float) -> str | None:
    from .scanner import resolve_path
    from .subgen import find_output

    local = resolve_path(f)
    deadline = time.time() + WHISPER_SRT_WAIT
    while local:
        out = find_output(local, since)
        if out or time.time() > deadline:
            return out
        db.x("UPDATE actions SET result=?, updated_at=? WHERE id=?",
             ("Bazarr accepted it - waiting for the subtitle file to appear", time.time(), action_id))
        time.sleep(15)
    return None


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
