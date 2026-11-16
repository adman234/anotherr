"""Ask Claude (through the MCP bridge) to look up a title's real runtime online before trusting a runtime issue.

Lifecycle of a `mcp_review` action:
  proposed  -> the person approves it (or the bulk button approves all of them)
  waiting   -> the next Claude session using the MCP picks it up (runtime_check_requests), searches online and
               reports back (submit_runtime_check)
  then, by verdict:
    file_ok   -> proposed again, phase "verdict": approving it ignores the runtime issue(s) and withdraws the
                 redownload they raised
    file_bad  -> done; the redownload proposal stays (with Claude's finding added to its reason) for the person
    unsure    -> done, with Claude's notes
Claude never approves or rejects anything itself: the person still makes every change.
"""
from __future__ import annotations

import time

from . import db

KIND = "mcp_review"
CODES = ("duration_short", "duration_short_warn", "double_length", "duration_long")  # what a web lookup can settle
VERDICTS = ("file_ok", "file_bad", "unsure")


def _open_codes(file_id: int) -> list[str]:
    rows = db.q(f"SELECT DISTINCT code FROM issues WHERE file_id=? AND status='open' AND code IN "
                f"({','.join('?' * len(CODES))})", (file_id, *CODES))
    return sorted(r["code"] for r in rows)


def propose(file_id: int) -> int | None:
    """Propose a runtime check for this file's open runtime issues, unless one was already asked for since those
    issues appeared (a rejected or finished check isn't asked again for the same issues)."""
    codes = _open_codes(file_id)
    if not codes:
        return None
    newest = db.scalar(f"SELECT MAX(created_at) FROM issues WHERE file_id=? AND status='open' AND code IN "
                       f"({','.join('?' * len(codes))})", (file_id, *codes)) or 0
    if db.q1("SELECT id FROM actions WHERE file_id=? AND kind=? AND (status IN ('proposed','waiting') OR created_at >= ?)",
             (file_id, KIND, newest)):
        return None
    now = time.time()
    reason = ("Ask Claude to look up the real runtime online (next time the MCP is used) and say whether the file is "
              "fine. Issues: " + ", ".join(codes))
    return db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                "VALUES (?,?,?,?, 'proposed', 0, ?, ?, ?)", (file_id, KIND, reason, db.j(codes),
                                                               db.j({"phase": "request"}), now, now))


def propose_all() -> int:
    """Backfill: a proposal for every file with an open runtime issue that hasn't been checked."""
    n = 0
    for r in db.q(f"SELECT DISTINCT x.file_id FROM issues x JOIN files f ON f.id=x.file_id WHERE x.status='open' "
                  f"AND f.missing=0 AND x.code IN ({','.join('?' * len(CODES))})", CODES):
        n += bool(propose(r["file_id"]))
    return n


def request_ids() -> list[int]:
    return [a["id"] for a in db.q("SELECT id, payload FROM actions WHERE kind=? AND status='proposed'", (KIND,))
            if (db.unj(a["payload"], {}) or {}).get("phase", "request") == "request"]


def approve_all() -> dict:
    """The bulk button: propose any missing checks, then hand every request to Claude."""
    made = propose_all()
    ids = request_ids()
    for i in ids:
        approve(i)
    return {"ok": True, "proposed": made, "waiting": len(ids)}


def approve(action_id: int) -> dict:
    a = db.q1("SELECT * FROM actions WHERE id=? AND kind=?", (action_id, KIND))
    if not a or a["status"] != "proposed":
        return {"ok": False, "error": "action not runnable"}
    p = db.unj(a["payload"], {}) or {}
    if p.get("phase") == "verdict":
        return _apply_ok(a, p)
    db.x("UPDATE actions SET status='waiting', result=?, updated_at=? WHERE id=?",
         ("Waiting for Claude to check the runtime online the next time the MCP is used", time.time(), action_id))
    return {"ok": True, "result": "waiting for Claude"}


def _apply_ok(a: dict, p: dict) -> dict:
    """Claude found the file's runtime is right: ignore the runtime issues and withdraw what they raised."""
    from . import actions

    codes = db.unj(a["codes"], []) or []
    now = time.time()
    n = db.n(f"UPDATE issues SET status='ignored', updated_at=? WHERE file_id=? AND status='open' AND code IN "
             f"({','.join('?' * len(codes))})", (now, a["file_id"], *codes)) if codes else 0
    sev = [r["severity"] for r in db.q("SELECT severity FROM issues WHERE file_id=? AND status='open'", (a["file_id"],))]
    db.x("UPDATE files SET status=?, open_issues=? WHERE id=? AND last_scan_at IS NOT NULL",
         (db.worst(sev) or "ok", len(sev), a["file_id"]))
    actions.reconcile(a["file_id"])
    msg = f"ignored {n} runtime issue(s); proposals they raised were withdrawn"
    db.x("UPDATE actions SET status='done', result=?, updated_at=? WHERE id=?", (msg, now, a["id"]))
    db.log(f"Runtime check on file {a['file_id']}: {msg}")
    return {"ok": True, "result": msg}


def waiting() -> list[dict]:
    """What Claude needs to look each title up: the title, year, episode, the file's length and what was expected."""
    out = []
    for a in db.q("SELECT a.id, a.file_id, a.codes, a.created_at, f.label, f.arr_path, i.title, i.year, "
                  "i.kind, i.source FROM actions a JOIN files f ON f.id=a.file_id JOIN items i ON i.id=f.item_id "
                  "WHERE a.kind=? AND a.status='waiting' ORDER BY a.id", (KIND,)):
        issues = [{"code": x["code"], "message": x["message"], "details": db.unj(x["details"], {})}
                  for x in db.q("SELECT code, message, details FROM issues WHERE file_id=? AND status='open'",
                                (a["file_id"],)) if x["code"] in CODES]
        redl = db.q1("SELECT id FROM actions WHERE file_id=? AND kind='redownload' AND status='proposed'", (a["file_id"],))
        out.append({"action_id": a["id"], "title": a["title"], "year": a["year"], "kind": a["kind"],
                    "episode": a["label"] or "", "file": (a["arr_path"] or "").rsplit("/", 1)[-1],
                    "file_minutes": next((x["details"].get("file_min") for x in issues if x["details"].get("file_min")), None),
                    "issues": issues, "redownload_action": redl["id"] if redl else None})
    return out


def submit(action_id: int, verdict: str, runtime_min: float | None = None, notes: str = "",
           sources: list[str] | None = None) -> dict:
    if verdict not in VERDICTS:
        return {"ok": False, "error": f"verdict must be one of {', '.join(VERDICTS)}"}
    a = db.q1("SELECT * FROM actions WHERE id=? AND kind=?", (action_id, KIND))
    if not a or a["status"] != "waiting":
        return {"ok": False, "error": "no runtime check waiting with that id"}
    src = [s for s in (sources or []) if s][:5]
    found = (f"real runtime ~{runtime_min:g} min" if runtime_min else "no runtime found")
    said = f"Claude checked online: {found}" + (f". {notes.strip()}" if notes.strip() else "") \
        + (f" Sources: {', '.join(src)}" if src else "")
    now = time.time()
    p = dict(db.unj(a["payload"], {}) or {}, verdict=verdict, runtime_min=runtime_min, notes=notes, sources=src,
             checked_at=now)
    if verdict == "file_ok":
        p["phase"] = "verdict"
        reason = f"{said}. The file looks complete. Approve to ignore the runtime issue and drop the redownload."
        db.x("UPDATE actions SET status='proposed', reason=?, payload=?, result=NULL, updated_at=? WHERE id=?",
             (reason, db.j(p), now, action_id))
    else:
        what = "the file really is short or long; the redownload proposal stays for you" if verdict == "file_bad" \
            else "Claude couldn't settle it; decide on the redownload yourself"
        db.x("UPDATE actions SET status='done', payload=?, result=?, updated_at=? WHERE id=?",
             (db.j(p), f"{said}. Verdict: {what}.", now, action_id))
        if verdict == "file_bad":
            for r in db.q("SELECT id, reason FROM actions WHERE file_id=? AND kind='redownload' AND status='proposed'",
                          (a["file_id"],)):
                db.x("UPDATE actions SET reason=?, updated_at=? WHERE id=?",
                     (f"{r['reason']} ({said}.)", now, r["id"]))
    db.log(f"Runtime check on file {a['file_id']}: {verdict} - {said}")
    return {"ok": True, "verdict": verdict}
