import os
import time

from anotherr import db, paths, settings, unraid


def _setup():
    root = paths.MNT
    show = os.path.join(root, "user", "Media", "TV Shows", "House", "Season 05")
    os.makedirs(show, exist_ok=True)
    os.makedirs(os.path.join(root, "disk1", "Media", "TV Shows", "House", "Season 05"), exist_ok=True)
    for base in (show, os.path.join(root, "disk1", "Media", "TV Shows", "House", "Season 05")):
        open(os.path.join(base, "House S05E10.mkv"), "wb").close()
    paths._bases_cache["t"] = 0
    unraid._dir_cache.clear()
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 1, 'House', 'series')")
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, 1, ?, ?)",
                (iid, "/data/tv/House/Season 05/House S05E10.mkv", time.time()))


def test_suggest_and_reresolve():
    fid = _setup()
    r = paths.check()
    assert r["found"] == 0
    assert r["suggestions"][0]["from"] == "/data/tv"
    assert r["suggestions"][0]["to"].endswith("/user/Media/TV Shows")
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'file_not_found', 'error', 'open', ?)",
         (fid, time.time()))
    db.x("UPDATE files SET status='error', last_scan_at=1 WHERE id=?", (fid,))
    settings.save({"path_mappings": [{"from": "/data/tv", "to": r["suggestions"][0]["to"]}]})
    res = paths.reresolve()
    assert res["found_again"] == 1
    f = db.q1("SELECT * FROM files WHERE id=?", (fid,))
    assert f["status"] == "unscanned" and f["last_scan_at"] is None and f["disk"] == "disk1"
    assert paths.check()["found"] == 1


def test_guard_pauses_after_repeated_missing():
    fid = _setup()
    now = time.time()
    for _ in range(5):
        sid = db.x("INSERT INTO scans(file_id, status, started_at) VALUES (?, 'done', ?)", (fid, now))
        db.x("INSERT INTO issues(file_id, scan_id, code, status) VALUES (?, ?, 'file_not_found', 'open')", (fid, sid))
    assert paths.mappings_look_wrong()
    settings.state_set("paths_changed_at", time.time() + 1)  # mappings changed -> guard resets
    assert not paths.mappings_look_wrong()
