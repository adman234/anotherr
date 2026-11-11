import json
import time

from anotherr import actions, arr, db


def _file():
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 3, 'Show', 'series')")
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, 30, '/tv/a.mkv', ?, ?)",
                (iid, '[{"id": 300, "season": 1, "episode": 1}, {"id": 301, "season": 1, "episode": 2}]', time.time()))


def test_redownload_skips_season_packs(monkeypatch):
    calls = []
    hist = {300: [{"id": 1, "data": {"fullSeason": "True"}}], 301: [{"id": 2, "data": {}}]}
    monkeypatch.setattr(arr.Sonarr, "grab_history", lambda self, s, e: hist[e])
    monkeypatch.setattr(arr.Sonarr, "mark_failed", lambda self, h: calls.append(("failed", h)))
    monkeypatch.setattr(arr.Sonarr, "delete_file", lambda self, f: calls.append(("delete", f)))
    monkeypatch.setattr(arr.Sonarr, "search", lambda self, ids: calls.append(("search", tuple(ids))))
    fid = _file()
    aid = actions.propose(fid, "redownload", "test")
    res = actions.execute(aid)
    assert res["ok"], res
    assert ("failed", 1) not in calls and ("failed", 2) in calls
    assert ("delete", 30) in calls and ("search", (300, 301)) in calls
    assert "season pack" in res["result"]


def test_no_repropose_within_week():
    fid = _file()
    now = time.time()
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'subs_missing', 'error', 'open', ?)", (fid, now))
    db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'resub', 'done', ?, ?)", (fid, now, now))
    assert actions.propose_for_file(fid) == []
    db.x("UPDATE actions SET updated_at=?", (now - 8 * 86400,))
    assert len(actions.propose_for_file(fid)) == 1


def test_stale_proposals_are_withdrawn_and_rechecked_before_running(monkeypatch):
    fid = _file()
    now = time.time()
    xid = db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'duration_short', 'error', 'open', ?)",
               (fid, now))
    aid = actions._propose(fid, "redownload", "Issues: duration_short", ["duration_short"])
    assert actions.reconcile(fid) == 0          # still open -> stays proposed
    db.x("UPDATE issues SET status='resolved' WHERE id=?", (xid,))
    assert actions.reconcile(fid) == 1
    assert db.q1("SELECT status FROM actions WHERE id=?", (aid,))["status"] == "withdrawn"
    # approving a proposal whose issue closed in the meantime does nothing
    db.x("UPDATE issues SET status='open' WHERE id=?", (xid,))
    aid2 = actions._propose(fid, "redownload", "x", ["duration_short"])
    db.x("UPDATE issues SET status='resolved' WHERE id=?", (xid,))
    monkeypatch.setattr(actions, "_redownload", lambda f, i: (_ for _ in ()).throw(AssertionError("must not run")))
    assert actions.execute(aid2)["ok"] is False


def test_auto_resub_skips_series_where_bazarr_found_nothing(monkeypatch):
    from anotherr import settings

    settings.save({"automation": {"enabled": True, "windows": []}})
    fid = _file()
    now = time.time()
    db.x("INSERT INTO actions(file_id, kind, codes, status, created_at, updated_at) VALUES (?, 'resub', ?, 'done', ?, ?)",
         (fid, json.dumps(["subs_missing"]), now - 7200, now - 7200))
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'subs_missing', 'error', 'open', ?)",
         (fid, now - 3600))  # the rescan after the resub still found no subs
    calls = []
    monkeypatch.setattr(actions, "_resub", lambda f, i: calls.append(f["id"]) or "ok")
    aid = actions._propose(fid, "resub", "x", ["subs_missing"])
    actions.maybe_auto()
    assert calls == [] and db.q1("SELECT status FROM actions WHERE id=?", (aid,))["status"] == "proposed"


def test_one_automatic_action_per_series_per_day(monkeypatch):
    from anotherr import settings

    settings.save({"automation": {"enabled": True, "windows": []}})
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 77, 'Run BTS!', 'series')")
    calls = []
    monkeypatch.setattr(actions, "_resub", lambda f, i: calls.append(f["id"]) or "ok")
    for n in range(3):
        fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, ?, '/x', '[]', ?)",
                   (iid, n, time.time()))
        db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'subs_missing', 'error', 'open', ?)",
             (fid, time.time()))
        actions._propose(fid, "resub", "x", ["subs_missing"])
    actions.maybe_auto()
    assert len(calls) == 1


def test_bulk_english_retag_and_out_of_sync_selection():
    fid = _file()
    now = time.time()

    def retag(files):
        db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) "
             "VALUES (?, 'retag_audio', 'proposed', ?, ?, ?)", (fid, json.dumps({"files": files}), now, now))

    retag([{"file_id": fid, "new": "eng", "conf": 0.99}])                      # qualifies
    retag([{"file_id": fid, "new": "eng", "conf": 0.9}])                       # below 0.95
    retag([{"file_id": fid, "new": "jpn", "conf": 0.99}])                      # not English
    retag([{"file_id": fid, "new": "eng", "conf": 0.99, "review": True}])      # flagged
    assert len(actions.english_retag_ids()) == 1
    assert len(actions.english_retag_ids(0.85)) == 2
    db.x("INSERT INTO actions(file_id, kind, status, codes, reason, created_at, updated_at) "
         "VALUES (?, 'resub', 'proposed', '[\"subs_out_of_sync\"]', 'x', ?, ?)", (fid, now, now))
    db.x("INSERT INTO actions(file_id, kind, status, codes, reason, created_at, updated_at) "
         "VALUES (?, 'resub', 'proposed', '[\"subs_missing\"]', 'x', ?, ?)", (fid, now, now))
    assert len(actions.subs_out_of_sync_ids()) == 1
