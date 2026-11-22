"""Issue-row fixes of 2026-11-22: exact "View proposed fix" links, Propose redownload, the Tried column, bulk
track-edit approvals no longer dying as "stalled" while they wait their turn, and MP4s with codec-less data streams."""
import threading
import time

from anotherr import actions, api, db, settings, trackedit


def _file(n=1):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', ?, 'Show', 'series')", (n,))
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, ?, ?, ?, ?)",
                (iid, n * 10, f"/tv/{n}.mkv", f'[{{"id": {n * 100}, "season": 1, "episode": {n}}}]', time.time()))


def _issue(fid, code, created):
    return db.x("INSERT INTO issues(file_id, category, code, severity, message, status, created_at) "
                "VALUES (?, 'audio', ?, 'error', 'msg', 'open', ?)", (fid, code, created))


def test_actions_list_by_ids_ignores_status_and_tab():
    fid = _file()
    now = time.time()
    a = db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'set_default_audio', 'proposed', ?, ?)",
             (fid, now, now))
    b = db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'redownload', 'done', ?, ?)",
             (fid, now, now))
    db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'resub', 'proposed', ?, ?)",
         (fid, now, now))
    rows = api.actions_list(ids=f"{a},{b}")
    assert sorted(r["id"] for r in rows) == [a, b]


def test_issue_rows_offer_redownload_and_list_attempts():
    fid = _file()
    now = time.time()
    iid = _issue(fid, "original_audio_missing", now - 1000)
    failed = db.x("INSERT INTO actions(file_id, kind, codes, status, result, created_at, updated_at) VALUES "
                  "(?, 'redownload', '[\"original_audio_missing\"]', 'failed', 'no untried release matches', ?, ?)",
                  (fid, now - 900, now - 900))
    # a done resub for a different issue type is not an attempt at this one
    db.x("INSERT INTO actions(file_id, kind, codes, status, created_at, updated_at) VALUES "
         "(?, 'resub', '[\"subs_missing\"]', 'done', ?, ?)", (fid, now - 500, now - 500))
    row = next(r for r in api.issues(page_no=1)["rows"] if r["id"] == iid)
    assert row["redownload"] is True
    assert [t["id"] for t in row["tried"]] == [failed]
    assert api.action_create({"kind": "redownload", "issue_id": iid})["action_id"]


def test_tried_counts_track_edit_entries_and_done_without_effect():
    fid = _file()
    now = time.time()
    iid = _issue(fid, "default_audio_not_preferred", now - 1000)
    payload = '{"files": [{"file_id": %d, "status": "failed", "outcome": "Could not find tag for codec none"}]}' % fid
    a = db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES "
             "(?, 'set_default_audio', 'failed', ?, ?, ?)", (fid, payload, now - 800, now - 800))
    # retag_audio doesn't fix a default-track issue
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES "
         "(?, 'retag_audio', 'failed', ?, ?, ?)", (fid, payload, now - 700, now - 700))
    # a redownload that finished after the issue was raised, and the issue is still open
    b = db.x("INSERT INTO actions(file_id, kind, codes, status, created_at, updated_at) VALUES "
             "(?, 'redownload', '[\"default_audio_not_preferred\"]', 'done', ?, ?)", (fid, now - 600, now - 600))
    t = api._attempts([iid])[iid]
    assert sorted(x["id"] for x in t) == [a, b]
    assert next(x for x in t if x["id"] == a)["result"].startswith("Could not find tag")


def test_bulk_propose_redownload_once_per_file():
    f1, f2 = _file(1), _file(2)
    now = time.time()
    ids = [_issue(f1, "duration_short", now), _issue(f1, "truncated", now), _issue(f2, "duration_short", now),
           _issue(f2, "default_audio_not_preferred", now)]
    r = api.issues_bulk({"op": "propose_redownload", "ids": ids})
    assert r["proposed"] == 2 and r["already_proposed"] == 0
    r = api.issues_bulk({"op": "propose_redownload", "ids": ids})
    assert r["proposed"] == 0 and r["already_proposed"] == 2
    codes = db.q1("SELECT codes FROM actions WHERE file_id=?", (f1,))["codes"]
    assert "duration_short" in codes and "truncated" in codes
    assert "default_audio_not_preferred" not in db.q1("SELECT codes FROM actions WHERE file_id=?", (f2,))["codes"]


def test_bulk_track_edits_wait_as_queued_not_running(monkeypatch):
    settings.save({"retag": {"enabled": True}})
    fid = _file()
    now = time.time()
    ids = [db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'retag_audio', 'proposed', ?, ?)",
                (fid, now, now)) for _ in range(3)]
    gate, seen = threading.Event(), []

    def run(i):
        seen.append({a["id"]: a["status"] for a in db.q("SELECT id, status FROM actions")})
        gate.wait(5)
        db.x("UPDATE actions SET status='done' WHERE id=?", (i,))

    monkeypatch.setattr(trackedit, "run", run)
    assert actions._approve_track_edits(ids, "test")["queued"] == 3
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    # while the first runs, the others wait as 'queued', which the stall reaper leaves alone
    assert seen[0] == {ids[0]: "running", ids[1]: "queued", ids[2]: "queued"}
    trackedit.reap()
    assert {a["status"] for a in db.q("SELECT status FROM actions WHERE id IN (?, ?)", (ids[1], ids[2]))} == {"queued"}
    gate.set()
    for _ in range(200):
        if all(a["status"] == "done" for a in db.q("SELECT status FROM actions")):
            break
        time.sleep(0.02)
    assert all(a["status"] == "done" for a in db.q("SELECT status FROM actions"))


def test_copy_args_can_drop_data_streams():
    ops = [{"type": "a", "index": 0, "default": True}]
    assert "-0:d?" not in trackedit.copy_args("/x.mp4", "/t.mp4", ops)
    args = trackedit.copy_args("/x.mp4", "/t.mp4", ops, drop_data=True)
    assert args[args.index("-0:d?") - 1] == "-map" and args.index("-0:d?") < args.index("-c")
