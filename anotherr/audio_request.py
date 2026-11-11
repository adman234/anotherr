"""Requested audio language: read from the Sonarr/Radarr quality profile, plus per-season accept exceptions.

A show is requested as 'original' (original-language audio), 'english', or '' (either). The scanner judges audio
against that request (see scanner.check_language). When the requested language simply isn't available, one
`accept_season_audio` action accepts the whole season (or movie) as it is: it writes an `audio_exceptions` row that
also covers episodes imported into that season later.

  allow 'original_only'  the season is accepted without English audio   (silences english_audio_missing)
  allow 'english_only'   the season is accepted without original audio  (silences the warning original_audio_missing)

A show-wide exception (scope 'show', season NULL, series only) does the same for every season, including seasons
imported later.

An exception never silences the "neither" error (no original and no English track) or default-track issues.
"""
from __future__ import annotations

import time

from . import db, settings

KIND = "accept_season_audio"
ALLOW_FOR_CODE = {"english_audio_missing": "original_only", "original_audio_missing": "english_only"}
CODES_FOR_ALLOW = {v: k for k, v in ALLOW_FOR_CODE.items()}
REQUESTS = ("", "original", "english")


def map_profile(name: str | None, cfg: dict | None = None) -> str:
    """Quality profile name -> '' | 'original' | 'english' (case-insensitive; unmapped = either)."""
    n = (name or "").strip().lower()
    if not n:
        return ""
    rp = ((cfg or settings.get()).get("languages") or {}).get("request_profiles") or {}
    for req in ("original", "english"):
        if n in {str(p).strip().lower() for p in rp.get(req) or []}:
            return req
    return ""


def effective(item: dict) -> tuple[str, str]:
    """(request, source): the manual override wins over the profile mapping; source is override | profile | none."""
    ovr = (item.get("audio_request_override") or "").strip().lower()
    if ovr:
        return (ovr if ovr in ("original", "english") else ""), "override"
    req = (item.get("audio_request") or "").strip().lower()
    return (req, "profile") if req in ("original", "english") else ("", "none")


def describe(item: dict) -> dict:
    req, src = effective(item)
    return {"audio_request_effective": req or "either", "audio_request_source": src}


def season_of(f: dict) -> int | None:
    eps = f.get("episodes")
    eps = eps if isinstance(eps, list) else db.unj(eps, [])  # a DB row (JSON text) or an already-decoded file report
    s = [e.get("season") for e in eps if e.get("season") is not None]
    return min(s) if s else None


def allows(item_id: int | None, season: int | None, allow: str) -> bool:
    """Is there an exception covering this item + season (NULL season = movie) for that kind of gap?"""
    if not item_id:
        return False
    return bool(db.q1("SELECT id FROM audio_exceptions WHERE item_id=? AND allow=? "
                      "AND (scope='show' OR (COALESCE(scope,'season')='season' AND season IS ?))",
                      (item_id, allow, season)))


def list_for_item(item_id: int) -> list[dict]:
    return db.q("SELECT * FROM audio_exceptions WHERE item_id=? ORDER BY season, id", (item_id,))


def _season_files(item_id: int, season: int | None, scope: str | None = "season") -> list[dict]:
    if scope == "show":
        return db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item_id,))
    return [f for f in db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item_id,)) if season_of(f) == season]


def refresh_file_status(fid: int) -> None:
    sev = [r["severity"] for r in db.q("SELECT severity FROM issues WHERE file_id=? AND status='open'", (fid,))]
    cur = db.q1("SELECT status FROM files WHERE id=?", (fid,))
    if not cur or cur["status"] in ("unscanned", "failed"):
        return
    db.x("UPDATE files SET status=?, open_issues=? WHERE id=?", (db.worst(sev) or "ok", len(sev), fid))


# ====================================================================== proposals

def _label(item: dict, season: int | None) -> str:
    return item["title"] + (f" Season {season}" if season is not None else "")


def refresh_item(item_id: int) -> dict:
    """One accept proposal per item + season + kind of gap, while a season has open warning-level gaps."""
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        return {}
    groups: dict[tuple, list[dict]] = {}
    for f in db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item_id,)):
        for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open' AND severity IN ('warning','dual') AND code IN (?, ?)",
                      (f["id"], *ALLOW_FOR_CODE)):
            groups.setdefault((season_of(f), ALLOW_FOR_CODE[r["code"]]), []).append(f)
    existing = {}
    for a in db.q("SELECT * FROM actions WHERE kind=? AND status='proposed'", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == item_id:
            existing[(p.get("season"), p.get("allow"))] = a
    now, made = time.time(), {"created": 0, "updated": 0, "withdrawn": 0}
    for (season, allow), files in groups.items():
        files.sort(key=lambda f: f.get("label") or "")
        miss = "English" if allow == "original_only" else "original-language"
        reason = (f"{_label(item, season)}: accept as is. {len(files)} file(s) have no {miss} audio; this records that "
                  f"it is not available and covers episodes imported into the season later.")
        payload = {"item_id": item_id, "season": season, "allow": allow, "code": CODES_FOR_ALLOW[allow],
                   "files": [{"file_id": f["id"], "label": f.get("label") or ""} for f in files]}
        a = existing.pop((season, allow), None)
        if a:
            db.x("UPDATE actions SET reason=?, payload=?, file_id=?, updated_at=? WHERE id=?",
                 (reason, db.j(payload), files[0]["id"], now, a["id"]))
            made["updated"] += 1
        else:
            db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                 "VALUES (?,?,?,?, 'proposed', 0, ?, ?, ?)",
                 (files[0]["id"], KIND, reason, "[]", db.j(payload), now, now))
            made["created"] += 1
    for a in existing.values():
        db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: no files in this season have the gap anymore', "
             "updated_at=? WHERE id=?", (now, a["id"]))
        made["withdrawn"] += 1
    return made


def propose_for_file(file_id: int, allow: str | None = None) -> int | None:
    """Manual proposal for a file's season (api/mcp). allow defaults to what the file's open warning calls for."""
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise ValueError("file not found")
    if allow is None:
        codes = {r["code"] for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open' AND severity IN ('warning','dual')",
                                         (file_id,))}
        hits = [ALLOW_FOR_CODE[c] for c in ALLOW_FOR_CODE if c in codes]
        if not hits:
            raise ValueError("this file has no open english_audio_missing / original_audio_missing warning")
        allow = hits[0]
    if allow not in CODES_FOR_ALLOW:
        raise ValueError("allow must be original_only | english_only")
    refresh_item(f["item_id"])
    season = season_of(f)
    for a in db.q("SELECT id, payload FROM actions WHERE kind=? AND status='proposed'", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == f["item_id"] and p.get("season") == season and p.get("allow") == allow:
            return a["id"]
    return None


# ====================================================================== approve

def accept(action_id: int) -> dict:
    """Approve an accept_season_audio proposal: record the exception, resolve the season's matching warnings,
    and withdraw redownload proposals that were raised only for those codes."""
    from . import actions

    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not a or a["status"] != "proposed":
        return {"ok": False, "error": "action not runnable"}
    p = db.unj(a["payload"], {}) or {}
    item_id, season, allow = p.get("item_id"), p.get("season"), p.get("allow")
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,)) if item_id else None
    if not item or allow not in CODES_FOR_ALLOW:
        db.x("UPDATE actions SET status='failed', result='item or allow missing', updated_at=? WHERE id=?",
             (time.time(), action_id))
        return {"ok": False, "error": "item no longer in the library"}
    now = time.time()
    if not allows(item_id, season, allow):
        db.x("INSERT INTO audio_exceptions(item_id, season, allow, created_at, note) VALUES (?,?,?,?,?)",
             (item_id, season, allow, now, f"accepted via action {action_id}"))
    code = CODES_FOR_ALLOW[allow]
    resolved, files = 0, _season_files(item_id, season)
    for f in files:
        resolved += db.n("UPDATE issues SET status='resolved', updated_at=? WHERE file_id=? AND status='open' "
                         "AND severity IN ('warning','dual') AND code=?", (now, f["id"], code))
        refresh_file_status(f["id"])
    for f in files:
        actions.reconcile(f["id"])  # redownloads raised only for this code lose their trigger
    msg = (f"Accepted {_label(item, season)} without {'English' if allow == 'original_only' else 'original-language'} "
           f"audio; resolved {resolved} issue(s)")
    db.x("UPDATE actions SET status='done', result=?, updated_at=? WHERE id=?", (msg, now, action_id))
    db.log(msg)
    return {"ok": True, "result": msg}


def on_redownload(file_id: int, codes: list[str]) -> int:
    """A redownload for these codes was approved: the accept proposal for that file's season is moot."""
    if not set(codes) & set(ALLOW_FOR_CODE):
        return 0
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        return 0
    season, n = season_of(f), 0
    for a in db.q("SELECT id, payload FROM actions WHERE kind=? AND status='proposed'", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == f["item_id"] and p.get("season") == season:
            db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: a redownload was approved instead', "
                 "updated_at=? WHERE id=?", (time.time(), a["id"]))
            n += 1
    return n


def open_gaps(item_id: int) -> list[str]:
    """The kinds of gap (original_only | english_only) that currently have an open warning on this item's files."""
    return sorted({ALLOW_FOR_CODE[r["code"]] for r in db.q(
        "SELECT DISTINCT i.code FROM issues i JOIN files f ON f.id=i.file_id WHERE f.item_id=? AND f.missing=0 "
        "AND i.status='open' AND i.severity IN ('warning','dual') AND i.code IN (?, ?)", (item_id, *ALLOW_FOR_CODE))})


def accept_show(item_id: int, allow: str | None = None, note: str = "accepted the whole show as is") -> dict:
    """Accept every season of a series as is: record one show-wide exception, resolve the matching open warnings on
    all files, and reject the accept_season_audio proposals and the redownloads raised only for that code."""
    from . import actions

    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        raise LookupError("item not found")
    if item.get("kind") != "series":
        raise ValueError("only a series can be accepted show-wide; accept a movie as it is instead")
    if allow is None:
        gaps = open_gaps(item_id)
        if not gaps:
            raise ValueError("this show has no open english_audio_missing / original_audio_missing warning")
        allow = gaps[0]
    if allow not in CODES_FOR_ALLOW:
        raise ValueError("allow must be original_only | english_only")
    code, now = CODES_FOR_ALLOW[allow], time.time()
    ex = db.q1("SELECT id FROM audio_exceptions WHERE item_id=? AND allow=? AND scope='show'", (item_id, allow))
    if ex:
        eid = ex["id"]
    else:
        eid = db.x("INSERT INTO audio_exceptions(item_id, season, scope, allow, created_at, note) VALUES (?,NULL,'show',?,?,?)",
                   (item_id, allow, now, note))
    files = _season_files(item_id, None, "show")
    resolved = 0
    for f in files:
        resolved += db.n("UPDATE issues SET status='resolved', updated_at=? WHERE file_id=? AND status='open' "
                         "AND severity IN ('warning','dual') AND code=?", (now, f["id"], code))
        refresh_file_status(f["id"])
    why = "Rejected: accepted the whole show as is"
    rejected = []
    for a in db.q("SELECT a.id, a.kind, a.codes, a.payload FROM actions a JOIN files f ON f.id=a.file_id "
                  "WHERE f.item_id=? AND a.status='proposed' AND a.kind IN (?, 'redownload')", (item_id, KIND)):
        if a["kind"] == KIND:
            hit = (db.unj(a["payload"], {}) or {}).get("allow") == allow
        else:
            codes = set(db.unj(a["codes"], []) or [])
            hit = bool(codes) and codes <= {code}  # a redownload that also has other codes (truncated...) stays
        if hit and db.n("UPDATE actions SET status='rejected', result=?, updated_at=? WHERE id=? AND status='proposed'",
                        (why, now, a["id"])):
            rejected.append(a["id"])
    for f in files:
        actions.reconcile(f["id"])
    miss = "English" if allow == "original_only" else "original-language"
    msg = f"Accepted all of {item['title']} without {miss} audio; resolved {resolved} issue(s)"
    db.log(msg)
    return {"ok": True, "result": msg, "exception_id": eid, "allow": allow, "resolved": resolved, "rejected": rejected}


def _evidence_langs(f: dict) -> set[str]:
    ev = db.unj(f.get("audio_evidence"), None) or {}
    return {t.get("effective") for t in ev.get("tracks") or [] if t.get("effective") and not t.get("commentary")}


def list_all() -> list[dict]:
    """Every exception with its item and how many files it covers. `fixed` = covered files whose last scan found
    both the original-language and an English audio track (a later download may have solved the gap)."""
    out = []
    for e in db.q("SELECT e.*, i.title, i.kind, i.quality_profile, i.audio_request, i.audio_request_override, "
                  "i.original_language FROM audio_exceptions e JOIN items i ON i.id=e.item_id "
                  "ORDER BY i.title COLLATE NOCASE, e.season, e.id"):
        e["scope"] = e.get("scope") or "season"
        e["audio_request_effective"] = effective(e)[0] or "either"
        files = _season_files(e["item_id"], e["season"], e["scope"])
        orig = (e.pop("original_language") or "").lower()
        e.pop("audio_request_override", None)
        e["files"] = len(files)
        e["fixed"] = sum(1 for f in files if (lambda L: "eng" in L and (not orig or orig in L))(_evidence_langs(f)))
        out.append(e)
    return out


def remove(exception_id: int) -> dict | None:
    """Delete an exception and queue a standard rescan of that season's files (the warnings come back if still true)."""
    from . import scheduler

    e = db.q1("SELECT * FROM audio_exceptions WHERE id=?", (exception_id,))
    if not e:
        return None
    db.x("DELETE FROM audio_exceptions WHERE id=?", (exception_id,))
    ids = [f["id"] for f in _season_files(e["item_id"], e["season"], e.get("scope"))]
    queued = scheduler.enqueue(ids, "standard", "recheck") if ids else 0
    return {"ok": True, "removed": exception_id, "queued": queued}
