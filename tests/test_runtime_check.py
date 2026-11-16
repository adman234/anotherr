import json
import time

from anotherr import actions, db, runtime_check


def _short_file(title="Spot's Magical Christmas"):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, year) VALUES ('radarr', 9, ?, 'movie', 1995)", (title,))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen, last_scan_at, status, open_issues) "
               "VALUES (?, 1, '/movies/spot.mkv', ?, ?, 'error', 1)", (iid, time.time(), time.time()))
    db.x("INSERT INTO issues(file_id, category, code, severity, message, details, status, created_at) "
         "VALUES (?, 'integrity', 'duration_short', 'error', 'File is 30.0 min for 1 episode(s) - too short', ?, "
         "'open', ?)", (fid, json.dumps({"file_min": 30.0}), time.time()))
    return fid


def _acts(fid, kind):
    return db.q("SELECT * FROM actions WHERE file_id=? AND kind=?", (fid, kind))


def test_runtime_issue_proposes_a_check_next_to_the_redownload():
    fid = _short_file()
    actions.propose_for_file(fid)
    assert len(_acts(fid, "redownload")) == 1
    (rc,) = _acts(fid, "mcp_review")
    assert rc["status"] == "proposed" and json.loads(rc["codes"]) == ["duration_short"]
    actions.propose_for_file(fid)  # no duplicate
    assert len(_acts(fid, "mcp_review")) == 1


def test_file_ok_verdict_then_approval_ignores_issue_and_withdraws_redownload():
    fid = _short_file()
    actions.propose_for_file(fid)
    rc = _acts(fid, "mcp_review")[0]["id"]
    assert actions.execute(rc)["ok"]
    (w,) = runtime_check.waiting()
    assert w["action_id"] == rc and w["file_minutes"] == 30.0 and w["redownload_action"]
    assert runtime_check.submit(rc, "file_ok", 30, "TMDB lists 30 min", ["https://example.org/spot"])["ok"]
    a = db.q1("SELECT * FROM actions WHERE id=?", (rc,))
    assert a["status"] == "proposed" and "30 min" in a["reason"]
    assert db.q1("SELECT status FROM actions WHERE kind='redownload'")["status"] == "proposed"  # nothing changes yet
    assert actions.execute(rc)["ok"]
    assert db.q1("SELECT status FROM issues WHERE file_id=?", (fid,))["status"] == "ignored"
    assert db.q1("SELECT status FROM actions WHERE kind='redownload'")["status"] == "withdrawn"
    assert db.q1("SELECT status FROM files WHERE id=?", (fid,))["status"] == "ok"
    runtime_check.propose_all()  # an answered check isn't asked again
    assert len(_acts(fid, "mcp_review")) == 1


def test_file_bad_keeps_redownload_and_notes_it():
    fid = _short_file()
    actions.propose_for_file(fid)
    rc = _acts(fid, "mcp_review")[0]["id"]
    runtime_check.approve(rc)
    assert runtime_check.submit(rc, "file_bad", 95)["ok"]
    assert db.q1("SELECT status FROM actions WHERE id=?", (rc,))["status"] == "done"
    redl = db.q1("SELECT status, reason FROM actions WHERE kind='redownload'")
    assert redl["status"] == "proposed" and "95 min" in redl["reason"]
    assert not runtime_check.submit(rc, "file_ok")["ok"]  # only waiting checks take a verdict


def test_bulk_backfills_and_hands_everything_to_claude():
    fid = _short_file()  # issue exists, but no proposal was made yet (raised before this feature)
    out = runtime_check.approve_all()
    assert out == {"ok": True, "proposed": 1, "waiting": 1}
    assert _acts(fid, "mcp_review")[0]["status"] == "waiting"
    assert runtime_check.request_ids() == []
