"""Scan cache table, pruning of old scan results, and the one-time migration."""
import json

from anotherr import db, scanstore


def _file():
    n = (db.scalar("SELECT MAX(id) FROM items") or 0) + 1
    iid = db.x("INSERT INTO items(source, arr_id, kind, title) VALUES ('sonarr', ?, 'series', 'X')", (n,))
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, ?, ?, '[]', 0)",
                (iid, n, f"/x{n}.mkv"))


def _scan(fid, result, status="done"):
    return db.x("INSERT INTO scans(file_id, status, result) VALUES (?,?,?)", (fid, status, json.dumps(result)))


def _res(sid):
    return json.loads(db.scalar("SELECT result FROM scans WHERE id=?", (sid,)))


def test_cache_round_trip_and_replace():
    fid = _file()
    with db.tx() as c:
        scanstore.save_cache(c, fid, 5, {"cache": {"made_at": 1, "decode:a": [1, 2]}, "fingerprint": {"size": 3},
                                         "sampled": [3, 30], "segments": [[0, 30]]})
    got = scanstore.load_cache(fid)
    assert got["scan_id"] == 5 and got["cache"]["decode:a"] == [1, 2] and got["segments"] == [[0, 30]]
    with db.tx() as c:
        scanstore.save_cache(c, fid, 9, {"cache": {"made_at": 2}})
    assert scanstore.load_cache(fid)["scan_id"] == 9
    assert db.scalar("SELECT COUNT(*) FROM scan_cache") == 1
    assert scanstore.load_cache(fid + 1) is None


def test_prune_keeps_what_is_read():
    fid = _file()
    old = _scan(fid, {"streams": {"a": 1}, "decode": 1})
    issue_scan = _scan(fid, {"streams": {"a": 1}, "decode": 2})
    sync_scan = _scan(fid, {"streams": {"a": 1}, "subtitle_sync": [1]})
    review_scan = _scan(fid, {"decode": 3})
    failed = _scan(fid, {"x": 1}, status="failed")
    last = _scan(fid, {"streams": {"a": 1}, "decode": 4})
    db.x("UPDATE files SET last_scan_id=? WHERE id=?", (last, fid))
    db.x("INSERT INTO issues(file_id, scan_id, category, code, severity, message, status) "
         "VALUES (?,?,'video','x','error','m','resolved')", (fid, issue_scan))
    db.x("INSERT INTO reviews(file_id, scan_id, kind, status) VALUES (?,?,'visual','done')", (fid, review_scan))
    other = _file()
    other_old = _scan(other, {"decode": 9})
    with db.tx() as c:
        assert scanstore.prune(c, fid) == 2
    assert _res(old) == {} and _res(failed) == {}
    assert _res(issue_scan) == {"decode": 2}  # kept, minus streams
    assert _res(sync_scan) == {"subtitle_sync": [1]}
    assert _res(review_scan) == {"decode": 3}
    assert _res(last) == {"streams": {"a": 1}, "decode": 4}
    assert _res(other_old) == {"decode": 9}  # another file is untouched


def test_migration_moves_caches_prunes_and_runs_once():
    fid = _file()
    old = _scan(fid, {"decode": 1, "cache": {"made_at": 1, "k": "old"}})
    new = _scan(fid, {"decode": 2, "fingerprint": {"size": 1}, "sampled": [3, 30], "segments": [[0, 30]],
                      "cache": {"made_at": 2, "k": "new"}})
    db.x("UPDATE files SET last_scan_id=? WHERE id=?", (new, fid))
    out = scanstore.migrate()
    assert out["caches_moved"] == 2 and out["results_emptied"] == 1
    got = scanstore.load_cache(fid)
    assert got["scan_id"] == new and got["cache"]["k"] == "new" and got["sampled"] == [3, 30]
    assert "cache" not in _res(new) and _res(old) == {}
    assert scanstore.migrate() is None
