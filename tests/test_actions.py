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
    monkeypatch.setattr(arr.Sonarr, "import_history", lambda self, s, e: [])
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


def test_unfixable_default_audio_proposes_redownload():
    fid = _file()
    now = time.time()
    db.x("INSERT INTO issues(file_id, code, severity, details, status, created_at) "
         "VALUES (?, 'default_audio_not_preferred', 'warning', ?, 'open', ?)",
         (fid, json.dumps({"fix_available": False, "target_track": None}), now))
    [aid] = actions.propose_for_file(fid)
    a = db.q1("SELECT kind, status, codes FROM actions WHERE id=?", (aid,))
    assert a["kind"] == "redownload" and a["status"] == "proposed"
    assert json.loads(a["codes"]) == ["default_audio_not_preferred"]


def test_flag_fixable_default_audio_does_not_propose_redownload():
    fid = _file()
    db.x("INSERT INTO issues(file_id, code, severity, details, status, created_at) "
         "VALUES (?, 'default_audio_not_preferred', 'warning', ?, 'open', ?)",
         (fid, json.dumps({"fix_available": True, "target_track": 1}), time.time()))
    assert not any(db.scalar("SELECT kind FROM actions WHERE id=?", (a,)) == "redownload"
                   for a in actions.propose_for_file(fid))


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
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', "
         "'proposed', ?, ?, ?)", (fid, json.dumps({"series_language": "jpn", "files": [
             {"file_id": fid, "new": "jpn", "old": "", "conf": 0.98}]}), now, now))   # untagged -> series language
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', "
         "'proposed', ?, ?, ?)", (fid, json.dumps({"series_language": "jpn", "files": [
             {"file_id": fid, "new": "jpn", "old": "eng", "conf": 0.98}]}), now, now))  # overwrite, not unanimous
    assert len(actions.english_retag_ids()) == 2
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', "
         "'proposed', ?, ?, ?)", (fid, json.dumps({"series_language": "jpn", "files": [
             {"file_id": fid, "new": "jpn", "old": "eng", "conf": 0.98, "all_agree": True},     # unanimous overwrite
             {"file_id": fid, "new": "eng", "old": "", "conf": 0.99, "mismatch": True}]}), now, now))  # English dub
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', "
         "'proposed', ?, ?, ?)", (fid, json.dumps({"series_language": "jpn", "files": [
             {"file_id": fid, "new": "kor", "old": "", "conf": 0.99, "mismatch": True}]}), now, now))  # third language
    assert len(actions.english_retag_ids()) == 3
    db.x("INSERT INTO actions(file_id, kind, status, codes, reason, created_at, updated_at) "
         "VALUES (?, 'resub', 'proposed', '[\"subs_out_of_sync\"]', 'x', ?, ?)", (fid, now, now))
    db.x("INSERT INTO actions(file_id, kind, status, codes, reason, created_at, updated_at) "
         "VALUES (?, 'resub', 'proposed', '[\"subs_missing\"]', 'x', ?, ?)", (fid, now, now))
    assert len(actions.subs_out_of_sync_ids()) == 1


def test_bulk_preview_counts_files_per_button():
    from anotherr import api

    fid = _file()
    now = time.time()
    two = [{"file_id": fid, "new": "eng", "conf": 0.99}, {"file_id": fid + 1, "new": "eng", "conf": 0.99}]
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) "
         "VALUES (?, 'retag_audio', 'proposed', ?, ?, ?)", (fid, json.dumps({"files": two}), now, now))
    for _ in range(3):
        db.x("INSERT INTO actions(file_id, kind, status, codes, reason, created_at, updated_at) "
             "VALUES (?, 'resub', 'proposed', '[\"subs_out_of_sync\"]', 'x', ?, ?)", (fid, now, now))
    p = api.actions_bulk_preview()
    assert p["english_retags"] == 1 and p["files"]["english_retags"] == 2   # one batch, two files
    assert p["files"]["out_of_sync_resubs"] == 3                            # no payload: one file each
    assert p["files"]["foreign_defaults"] == 0


def test_bulk_subgen_approves_every_proposed_batch(monkeypatch):
    from anotherr import api, subgen

    fid = _file()
    now = time.time()
    for files in ([{"file_id": fid}, {"file_id": fid + 1}], [{"file_id": fid}]):
        db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) "
             "VALUES (?, 'subgen_transcribe', 'proposed', ?, ?, ?)", (fid, json.dumps({"files": files}), now, now))
    db.x("INSERT INTO actions(file_id, kind, status, codes, reason, created_at, updated_at) "
         "VALUES (?, 'resub', 'proposed', '[]', 'x', ?, ?)", (fid, now, now))
    seen = []
    monkeypatch.setattr(subgen, "approve", lambda i: seen.append(i) or {"ok": True})
    r = api.actions_bulk_subgen({})
    assert r == {"ok": True, "queued": 2, "files": 3} and len(seen) == 2   # the resub is left alone
    monkeypatch.setattr(subgen, "approve", lambda i: {"ok": False, "error": "subgen.url is not set"})
    assert api.actions_bulk_subgen({})["error"] == "subgen.url is not set"


def test_current_grab_follows_the_import_not_the_newest_grab():
    grabs = [{"id": 9, "downloadId": "NEW"}, {"id": 5, "downloadId": "OLD"}]
    assert actions._current_grab(grabs, [{"downloadId": "OLD"}])["id"] == 5
    assert actions._current_grab(grabs, [{"downloadId": "GONE"}]) is None
    assert actions._current_grab(grabs, []) is None  # ambiguous without import history
    assert actions._current_grab(grabs[:1], [])["id"] == 9


def test_claim_is_atomic():
    fid = _file()
    aid = actions.propose(fid, "redownload", "test")
    assert actions._claim(aid) is True
    assert actions._claim(aid) is False
    assert actions.execute(aid)["ok"] is False


def _rel(title, lang="English", rej=(), score=0, seeders=1, **kw):
    return {"guid": title, "indexerId": 1, "title": title, "languages": [{"name": lang}], "rejections": list(rej),
            "customFormatScore": score, "seeders": seeders, **kw}


def test_replace_on_import_keeps_the_file_and_grabs_a_matching_release(monkeypatch):
    calls = []
    rels = [_rel("jp", lang="Japanese"), _rel("pack", fullSeason=True), _rel("old"),
            _rel("blocked", rej=["Release is blocklisted"]),
            _rel("dub-low", rej=["Existing file meets cutoff: WEBDL-1080p"], score=1),
            _rel("dub-high", rej=["Existing file on disk is of equal or higher preference"], score=5)]
    monkeypatch.setattr(arr.Sonarr, "grab_history", lambda self, s, e: [{"sourceTitle": "old"}])
    monkeypatch.setattr(arr.Sonarr, "releases", lambda self, e: rels)
    monkeypatch.setattr(arr.Sonarr, "grab_release", lambda self, g, i: calls.append(("grab", g)))
    monkeypatch.setattr(arr.Sonarr, "mark_failed", lambda self, h: calls.append(("failed", h)))
    monkeypatch.setattr(arr.Sonarr, "delete_file", lambda self, f: calls.append(("delete", f)))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, 30, '/tv/a.mkv', ?, ?)",
               (db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 3, 'Show', 'series')"),
                '[{"id": 300, "season": 1, "episode": 1}]', time.time()))
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'english_audio_missing', 'error', 'open', ?)",
         (fid, time.time()))
    aid = actions._propose(fid, "redownload", "x", ["english_audio_missing"])
    res = actions.execute(aid)
    assert res["ok"], res
    assert calls == [("grab", "dub-high")]


def test_replace_on_import_changes_nothing_when_no_release_matches(monkeypatch):
    calls = []
    monkeypatch.setattr(arr.Sonarr, "grab_history", lambda self, s, e: [])
    monkeypatch.setattr(arr.Sonarr, "releases", lambda self, e: [_rel("jp", lang="Japanese")])
    monkeypatch.setattr(arr.Sonarr, "grab_release", lambda self, g, i: calls.append("grab"))
    monkeypatch.setattr(arr.Sonarr, "delete_file", lambda self, f: calls.append("delete"))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, 30, '/tv/a.mkv', ?, ?)",
               (db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 3, 'Show', 'series')"),
                '[{"id": 300, "season": 1, "episode": 1}]', time.time()))
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'english_audio_missing', 'error', 'open', ?)",
         (fid, time.time()))
    res = actions.execute(actions._propose(fid, "redownload", "x", ["english_audio_missing"]))
    assert not res["ok"] and "left alone" in res["result"] and calls == []
