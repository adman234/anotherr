import json
import time

from anotherr import db, scanner


def _file():
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 3, 'Show', 'series')")
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, 30, '/tv/a.mkv', ?)",
                (iid, time.time()))


def _ignored(fid, code, category):
    return db.x("INSERT INTO issues(file_id, category, code, severity, message, details, status, created_at) "
                "VALUES (?, ?, ?, 'warning', 'm', '{}', 'ignored', ?)", (fid, category, code, time.time()))


def _scan(fid, issues, ran):
    sid = db.x("INSERT INTO scans(file_id, profile, started_at, status) VALUES (?, 'standard', ?, 'running')",
               (fid, time.time()))
    ctx = scanner.Ctx(file={"id": fid}, item={"id": None, "title": "Show", "kind": "series"}, cfg={}, profile="standard", pcfg={}, seed=0,
                      issues=issues, ran=set(ran))
    scanner.persist(ctx, sid, "done", "", 1.0)


def _st(xid):
    return db.scalar("SELECT status FROM issues WHERE id=?", (xid,))


def test_rescan_resolves_ignored_issues_no_longer_found():
    fid = _file()
    gone = _ignored(fid, "default_audio_not_preferred", "audio")
    still = _ignored(fid, "audio_untagged", "audio")
    unchecked = _ignored(fid, "subs_missing", "subtitles")
    _scan(fid, [{"category": "audio", "code": "audio_untagged", "severity": "warning", "message": "m2", "details": {}}],
          ["audio"])
    assert _st(gone) == "resolved"
    assert json.loads(db.scalar("SELECT details FROM issues WHERE id=?", (gone,)))["was_ignored"] == 1
    assert _st(still) == "ignored"  # still found: stays ignored, refreshed in place
    assert db.scalar("SELECT message FROM issues WHERE id=?", (still,)) == "m2"
    assert _st(unchecked) == "ignored"  # this scan never checked subtitles
    assert db.scalar("SELECT COUNT(*) FROM issues WHERE status='open'") == 0


def test_resolved_ignored_issue_comes_back_ignored():
    fid = _file()
    _ignored(fid, "default_audio_not_preferred", "audio")
    _scan(fid, [], ["audio"])
    _scan(fid, [{"category": "audio", "code": "default_audio_not_preferred", "severity": "warning", "message": "m",
                 "details": {}}], ["audio"])
    assert db.scalar("SELECT COUNT(*) FROM issues WHERE status='open'") == 0
    assert db.scalar("SELECT COUNT(*) FROM issues WHERE status='ignored'") == 1
