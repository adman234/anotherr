"""Repairs for subtitles that exist but are wrong: shift an offset, move a bad sidecar aside, then subgen."""
import json
import time

from anotherr import actions, analysis, db, settings, subgen, subretire, subshift


def _w(offset, score=0.7, zero=0.2):
    return {"offset": offset, "score": score, "score_at_zero": zero}


def test_one_window_is_not_enough_for_an_offset():
    assert analysis.aggregate_sync([_w(12.0)])["verdict"] == "unknown"
    assert analysis.aggregate_sync([_w(12.0), None, None])["verdict"] == "unknown"
    assert analysis.aggregate_sync([_w(2.5), _w(2.4)])["verdict"] == "offset"


def _file(tmp_path, codes, sync, orig="eng", subs=()):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, original_language) VALUES ('sonarr', ?, 'Show', 'series', ?)",
               (int(time.time() * 1e6) % 10**9, orig))
    video = tmp_path / "Show S01E01.mkv"
    video.write_bytes(b"x")
    ev = {"scan_started": time.time() - 60, "subs": list(subs),
          "tracks": [{"a_index": 0, "tag": orig, "effective": orig, "default": True, "confident": True,
                      "all_agree": True, "windows": [{"lang": orig, "prob": 0.97}] * 3, "commentary": False}]}
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, label, episodes, first_seen, duration, "
               "audio_evidence) VALUES (?,?,?,?,?,?,?,?,?)",
               (iid, 7, str(video), str(video), "S01E01", json.dumps([{"id": 1, "season": 1}]), time.time(), 1300.0,
                json.dumps(ev)))
    for c in codes:
        db.x("INSERT INTO issues(file_id, code, severity, message, status, created_at) VALUES (?,?,'warning','m','open',?)",
             (fid, c, time.time()))
    db.x("INSERT INTO scans(file_id, status, started_at, finished_at, result) VALUES (?, 'done', ?, ?, ?)",
         (fid, time.time(), time.time(), json.dumps({"subtitle_sync": sync})))
    return iid, fid, video


def _sidecar(video, suffix=".en.srt"):
    p = video.with_name(video.stem + suffix)
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    return p


def _kinds(fid):
    return {r["kind"] for r in db.q("SELECT kind FROM actions WHERE file_id=? AND status='proposed'", (fid,))}


def test_known_offset_proposes_a_shift_not_a_resub(tmp_path):
    _, fid, video = _file(tmp_path, ["subs_out_of_sync"], [])
    sc = _sidecar(video)
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "ext en", "external": True, "path": str(sc), "offset": 2.4, "verdict_final": "offset"}]}),))
    actions.propose_for_file(fid)
    assert _kinds(fid) == {"shift_subtitle"}


def test_after_one_shift_the_offset_goes_to_a_resub(tmp_path):
    _, fid, video = _file(tmp_path, ["subs_out_of_sync"], [])
    sc = _sidecar(video)
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "ext en", "external": True, "path": str(sc), "offset": 2.4, "verdict_final": "offset"}]}),))
    old = time.time() - 8 * 86400
    db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'shift_subtitle', 'done', ?, ?)",
         (fid, old, old))
    actions.propose_for_file(fid)
    assert _kinds(fid) == {"resub"}


def test_partial_mismatch_retires_the_bad_sidecar(tmp_path):
    _, fid, video = _file(tmp_path, ["subs_mismatch_partial"], [])
    bad = _sidecar(video, ".en.srt")
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "s1 English", "external": False, "s_index": 0, "verdict_final": "ok"},
        {"track": "ext en", "external": True, "path": str(bad), "verdict_final": "mismatch"}]}),))
    actions.propose_for_file(fid)
    assert _kinds(fid) == {"retire_subtitle"}
    aid = db.scalar("SELECT id FROM actions WHERE file_id=? AND kind='retire_subtitle'", (fid,))
    res = actions.execute(aid)
    assert res["ok"], res
    assert not bad.exists() and bad.with_name(bad.name + subshift.BACKUP_SUFFIX).exists()
    assert db.q1("SELECT id FROM queue WHERE file_id=?", (fid,))


def test_retire_keeps_an_existing_shift_backup(tmp_path):
    _, fid, video = _file(tmp_path, ["subs_mismatch_partial"], [])
    bad = _sidecar(video)
    bak = bad.with_name(bad.name + subshift.BACKUP_SUFFIX)
    bak.write_text("original")
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "s1", "external": False, "verdict_final": "ok"},
        {"track": "ext en", "external": True, "path": str(bad), "verdict_final": "mismatch"}]}),))
    subretire.run({"id": fid})
    assert bak.read_text() == "original"
    assert bad.with_name(bad.name + subretire.RETIRED_SUFFIX).exists()


def test_retire_needs_a_good_track_and_skips_embedded(tmp_path):
    _, fid, _ = _file(tmp_path, ["subs_mismatch_partial"], [
        {"track": "s1", "external": False, "verdict_final": "ok"},
        {"track": "s2", "external": False, "verdict_final": "mismatch"}])
    assert subretire.plan(fid)["retire"] == [] and subretire.plan(fid)["embedded"] == ["s2"]
    actions.propose_for_file(fid)
    assert "retire_subtitle" not in _kinds(fid)


def _subgen_on():
    settings.save({"languages": {"subtitle_language": "eng"}, "subgen": {"url": "http://subgen.test"}})


def _failed_search(fid, code):
    old = time.time() - 3600
    db.x("INSERT INTO actions(file_id, kind, status, codes, created_at, updated_at) VALUES (?, 'resub', 'done', ?, ?, ?)",
         (fid, json.dumps([code]), old, old))
    db.x("UPDATE issues SET created_at=? WHERE file_id=?", (time.time(), fid))


def test_subgen_replaces_a_mismatched_sidecar_once_bazarr_failed(tmp_path):
    _subgen_on()
    _, fid, video = _file(tmp_path, ["subs_mismatch"], [], subs=[{"lang": "eng", "kind": "full", "external": True}])
    bad = _sidecar(video)
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "ext en", "external": True, "path": str(bad), "verdict_final": "mismatch"}]}),))
    plan, why = subgen.explain(fid, manual=False)
    assert plan is None and "resub comes first" in why
    _failed_search(fid, "subs_mismatch")
    plan, why = subgen.explain(fid, manual=False)
    assert plan, why
    assert plan["replace"] == str(bad) and plan["replace_kind"] == "mismatched" and plan["task"] == "transcribe"


def test_subgen_leaves_a_good_sidecar_alone(tmp_path):
    _subgen_on()
    _, fid, video = _file(tmp_path, ["subs_mismatch"], [])
    good = _sidecar(video)
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "ext en", "external": True, "path": str(good), "verdict_final": "ok"}]}),))
    plan, why = subgen.explain(fid, manual=True)
    assert plan is None and "matches the dialogue" in why


def test_subgen_waits_for_the_shift_on_a_plain_offset(tmp_path):
    _subgen_on()
    _, fid, video = _file(tmp_path, ["subs_out_of_sync"], [])
    sc = _sidecar(video)
    db.x("UPDATE scans SET result=?", (json.dumps({"subtitle_sync": [
        {"track": "ext en", "external": True, "path": str(sc), "offset": 3.0, "verdict_final": "offset"}]}),))
    _failed_search(fid, "subs_out_of_sync")
    plan, why = subgen.explain(fid, manual=False)
    assert plan is None and "shifting" in why
    old = time.time() - 3600
    db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'shift_subtitle', 'done', ?, ?)",
         (fid, old, old))
    plan, why = subgen.explain(fid, manual=False)
    assert plan and plan["replace"] == str(sc), why
