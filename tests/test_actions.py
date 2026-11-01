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
