"""retag_audio - cases from the Nov 5 retag spec."""
import json
import os
import shutil
import subprocess
import time

import pytest

from anotherr import db, media, retag, settings

HAVE_TOOLS = shutil.which("ffmpeg") and shutil.which("mkvpropedit")


def W(lang, n, prob=0.97):
    return [{"lang": lang, "prob": prob}] * n


def _item(title, orig, kind="series"):
    return db.x("INSERT INTO items(source, arr_id, title, kind, original_language) VALUES (?, ?, ?, ?, ?)",
                ("sonarr" if kind == "series" else "radarr", abs(hash(title)) % 10**6, title, kind, orig))


def _file(iid, label, season, tracks, issues=(), group="GRP", path=None):
    eps = json.dumps([{"id": hash(label) % 10**6, "season": season, "episode": 1}]) if season is not None else "[]"
    ev = {"scan_started": time.time() + 3600, "tracks": [
        dict({"a_index": i, "tag": "", "title": "", "commentary": False, "forced": False, "hi": False, "speech_s": 60}, **t)
        for i, t in enumerate(tracks)]}
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, label, episodes, release_group, audio_evidence, "
               "first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (iid, abs(hash(label)) % 10**6, path or f"/tv/{label}.mkv", path or f"/tv/{label}.mkv", label, eps, group,
                json.dumps(ev), time.time()))
    for code, st in issues:
        db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, ?, 'info', ?, ?)",
             (fid, code, st, time.time()))
    return fid


def _batches():
    return [dict(a, payload=json.loads(a["payload"])) for a in
            db.q("SELECT * FROM actions WHERE kind='retag_audio' AND status='proposed' ORDER BY id")]


def test_run_bts_season_batch_with_sibling_rule():
    iid = _item("Run BTS!", "kor")
    for e in range(1, 5):
        _file(iid, f"S01E0{e}", 1, [{"windows": W("kor", 3)}])
    weak1 = _file(iid, "S01E05", 1, [{"windows": W("kor", 1)}])                       # 1/1 - needs siblings
    weak2 = _file(iid, "S01E06", 1, [{"windows": W("kor", 2) + W("jpn", 1, 0.5)}])   # 2/3 - needs siblings
    retag.refresh_item(iid)
    (b,) = _batches()
    files = {f["file_id"]: f for f in b["payload"]["files"]}
    assert len(files) == 6 and {f["new"] for f in files.values()} == {"kor"}
    assert "confirmed by" in files[weak1]["why"] and "confirmed by" in files[weak2]["why"]
    assert b["reason"].startswith("Run BTS! Season 1: set audio track 1 to Korean on 6 file(s). Was untagged.")
    assert b["payload"]["files"][0]["method"] == "mkvpropedit" and "track:a1" in b["payload"]["files"][0]["command"]


def test_single_window_without_siblings_is_not_proposed():
    iid = _item("The Karate Kid", "eng", kind="movie")
    _file(iid, "", None, [{"windows": W("eng", 1)}])
    retag.refresh_item(iid)
    assert _batches() == []


def test_wrong_tag_needs_series_language_and_two_agreeing_siblings():
    iid = _item("Run BTS! S9", "kor")
    for e in range(1, 4):
        _file(iid, f"S09E0{e}", 9, [{"tag": "eng", "windows": W("kor", 3, 0.99)}], issues=[("audio_mislabeled", "open")])
    retag.refresh_item(iid)
    (b,) = _batches()
    assert b["payload"]["case"] == "wrong_tag" and "Manual approval only" in b["reason"]
    assert {f["old"] for f in b["payload"]["files"]} == {"eng"}


def test_haikyu_rule_no_retag_away_from_series_language():
    iid = _item("Haikyu!!", "jpn")
    for e in range(1, 5):
        _file(iid, f"S04E1{e}", 4, [{"tag": "jpn", "windows": W("kor", 3, 0.99)}])
    retag.refresh_item(iid)
    assert _batches() == []


def test_mismatched_language_goes_to_review_batch_per_file():
    iid = _item("Dragon Ball Z", "jpn")
    for e in range(1, 4):  # siblings: Japanese track 2 confirmed elsewhere in the season
        _file(iid, f"S02E0{e}", 2, [{"tag": "eng", "windows": W("eng", 3)}, {"windows": W("jpn", 3)}])
    dbz = _file(iid, "S02E09", 2, [{"windows": W("eng", 3)}, {"windows": W("jpn", 2) + W("kor", 1, 0.4)}])
    retag.refresh_item(iid)
    b = _batches()
    review = [x for x in b if x["payload"]["review"]]
    assert len(review) == 1 and review[0]["reason"].startswith("Manual review")
    tracks = sorted((f["file_id"], f["a_index"]) for f in review[0]["payload"]["files"])
    assert (dbz, 0) in tracks and (dbz, 1) in tracks  # both of the file's tracks go to review together


def test_exclusions():
    iid = _item("Baraka", "eng", kind="movie")
    _file(iid, "", None, [{"windows": W("eng", 3), "speech_s": 2}])                  # almost no speech
    iid2 = _item("Courage", "eng")
    _file(iid2, "S02E01", 2, [{"windows": W("eng", 3)}], issues=[("wrong_content", "open")])
    _file(iid2, "S02E02", 2, [{"windows": W("eng", 3), "commentary": True}])
    _file(iid2, "S02E03", 2, [{"windows": W("eng", 3)}], issues=[("audio_untagged", "ignored")])  # excluded by ignoring
    retag.refresh_item(iid)
    retag.refresh_item(iid2)
    assert _batches() == []


def test_batch_withdrawn_when_nothing_qualifies_anymore():
    iid = _item("NCIS", "eng")
    fids = [_file(iid, f"S24E0{e}", 24, [{"windows": W("eng", 3)}]) for e in range(1, 4)]
    retag.refresh_item(iid)
    assert len(_batches()) == 1
    for fid in fids:  # retagged by hand: evidence now shows the tag
        db.x("UPDATE files SET audio_evidence=? WHERE id=?", (json.dumps({"tracks": [
            {"a_index": 0, "tag": "eng", "speech_s": 60, "windows": W("eng", 3)}]}), fid))
    retag.refresh_item(iid)
    assert _batches() == []
    assert db.q1("SELECT status FROM actions WHERE kind='retag_audio'")["status"] == "withdrawn"


# ---------------------------------------------------------------- execution on real files

def _mkv(path, tags=("", "")):
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3",
            "-f", "lavfi", "-i", "sine=d=3", "-f", "lavfi", "-i", "sine=f=880:d=3", "-map", "0", "-map", "1", "-map", "2",
            "-c:v", "libx264", "-c:a", "aac"]
    for i, t in enumerate(tags):
        if t:
            args += [f"-metadata:s:a:{i}", f"language={t}"]
    subprocess.run(args + [str(path)], check=True)
    return str(path)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    from anotherr import trackedit
    monkeypatch.setattr(trackedit, "LOG_PATH", str(tmp_path / "retag_log.jsonl"))
    root = tmp_path / "media"
    root.mkdir()
    settings.save({"retag": {"enabled": True, "allowed_paths": [str(root)]}})
    return root


def _entry(fid, a_index=0, old="", new="kor"):
    return {"file_id": fid, "a_index": a_index, "old": old, "new": new, "scan_started": time.time() + 60}


@pytest.mark.skipif(not HAVE_TOOLS, reason="ffmpeg + mkvpropedit needed")
def test_edit_in_place_verify_and_undo(lib):
    p = _mkv(lib / "ep.mkv")
    iid = _item("Run BTS! X", "kor")
    fid = _file(iid, "S01E01", 1, [{"windows": W("kor", 3)}], path=p)
    aid = db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', "
               "'running', ?, ?, ?)", (fid, json.dumps({"item_id": iid, "files": [_entry(fid)]}), time.time(), time.time()))
    retag._run(aid)  # as started by an approval
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "done", a["result"]
    assert media.summarize(media.ffprobe(p))["audio"][0]["lang"] == "kor"
    assert db.q1("SELECT source FROM queue WHERE file_id=?", (fid,))["source"] == "retag"  # read-back rescan
    # read-back: a rescan that sees the tag confirms it
    retag.after_scan(fid, media.summarize(media.ffprobe(p)), 0)
    assert json.loads(db.q1("SELECT payload FROM actions WHERE id=?", (aid,))["payload"])["files"][0]["verified"] is True
    assert retag.undo(aid)["restored"] == 1
    assert media.summarize(media.ffprobe(p))["audio"][0]["lang"] == ""


@pytest.mark.skipif(not HAVE_TOOLS, reason="ffmpeg + mkvpropedit needed")
def test_guards(lib, tmp_path):
    cfg = settings.get()
    iid = _item("Guards", "kor")
    outside = _mkv(tmp_path / "outside.mkv")
    f1 = _file(iid, "S01E01", 1, [{}], path=outside)
    assert "allowed path" in retag.edit_one(_entry(f1), cfg, 1)["why"]
    p = _mkv(lib / "linked.mkv")
    os.link(p, str(lib / "seed-copy.mkv"))
    f2 = _file(iid, "S01E02", 1, [{}], path=p)
    assert "hardlinked" in retag.edit_one(_entry(f2), cfg, 1)["why"]
    settings.save({"retag": {"break_hardlinks": True}})
    r = retag.edit_one(_entry(f2), settings.get(), 1)
    assert r["status"] == "edited" and r["method"] == "copy"
    assert os.stat(p).st_nlink == 1 and media.summarize(media.ffprobe(str(lib / "seed-copy.mkv")))["audio"][0]["lang"] == ""
    p3 = _mkv(lib / "changed.mkv")
    f3 = _file(iid, "S01E03", 1, [{}], path=p3)
    old = dict(_entry(f3), scan_started=time.time() - 3600)
    assert "changed since" in retag.edit_one(old, settings.get(), 1)["why"]
    p4 = _mkv(lib / "tagged.mkv", tags=("eng", ""))
    f4 = _file(iid, "S01E04", 1, [{}], path=p4)
    assert "tag changed" in retag.edit_one(_entry(f4), settings.get(), 1)["why"]  # expected untagged, file says eng


@pytest.mark.skipif(not HAVE_TOOLS, reason="ffmpeg needed")
def test_mp4_copy_path_keeps_mode(lib):
    p = str(lib / "movie.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264", "-c:a", "aac", p], check=True)
    os.chmod(p, 0o640)
    iid = _item("Movie", "kor", kind="movie")
    fid = _file(iid, "", None, [{}], path=p)
    r = retag.edit_one(_entry(fid), settings.get(), 1)
    assert r["status"] == "edited" and r["method"] == "copy"
    assert media.summarize(media.ffprobe(p))["audio"][0]["lang"] == "kor" and oct(os.stat(p).st_mode & 0o777) == "0o640"
    assert not [n for n in os.listdir(lib) if n.startswith(".anotherr-retag")]


def test_disabled_changes_nothing():
    settings.save({"retag": {"enabled": False}})
    iid = _item("Off", "kor")
    fid = _file(iid, "S01E01", 1, [{}])
    aid = db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', "
               "'proposed', ?, ?, ?)", (fid, json.dumps({"item_id": iid, "files": [_entry(fid)]}), time.time(), time.time()))
    assert retag.start(aid)["ok"] is False
    assert "turned off" in db.q1("SELECT result FROM actions WHERE id=?", (aid,))["result"]


def test_retag_is_never_automatic():
    from anotherr import actions

    assert actions._eligible_auto({"kind": "retag_audio", "codes": "[]", "file_id": 1}, settings.get()) is False


def test_writes_through_share_path_when_disk_path_is_read_only(monkeypatch, tmp_path):
    from anotherr import sync

    share, disk = tmp_path / "user" / "tv", tmp_path / "disk3" / "tv"
    share.mkdir(parents=True)
    disk.mkdir(parents=True)
    (share / "ep.mkv").write_bytes(b"x")
    (disk / "ep.mkv").write_bytes(b"x")
    monkeypatch.setattr(sync, "map_path", lambda p: str(share / "ep.mkv"))
    writable = {str(share), str(share / "ep.mkv")}
    monkeypatch.setattr(os, "access", lambda p, mode: str(p) in writable)
    f = {"arr_path": "/tv/ep.mkv"}
    assert retag.write_path(f, str(disk / "ep.mkv")) == str(share / "ep.mkv")   # rw share mount wins
    writable.clear()
    assert retag.write_path(f, str(disk / "ep.mkv")) is None                     # nothing writable -> skip


def test_weak_english_file_gets_its_own_proposal():
    from anotherr import actions

    iid = _item("Superbook", "eng")
    for e in range(1, 4):
        _file(iid, f"S04E0{e}", 4, [{"windows": W("eng", 3, 0.99)}])
    weak = _file(iid, "S04E04", 4, [{"windows": W("eng", 3, 0.92)}])
    retag.refresh_item(iid)
    season, alone = sorted(_batches(), key=lambda b: len(b["payload"]["files"]), reverse=True)
    assert len(season["payload"]["files"]) == 3 and weak not in {f["file_id"] for f in season["payload"]["files"]}
    assert [f["file_id"] for f in alone["payload"]["files"]] == [weak] and "Listed on its own" in alone["reason"]
    assert actions.english_retag_ids() == [season["id"]]
    retag.refresh_item(iid)  # stable: the same two proposals are updated, not recreated
    assert sorted(b["id"] for b in _batches()) == sorted([season["id"], alone["id"]])
