"""Quick rescan: reuse the last scan's expensive results when the file has not changed."""
import shutil
import time

import pytest

from anotherr import db, media, scanner, scheduler, settings, stt
from tests.test_scan import _add, _small_profiles, fake_whisper, video  # noqa: F401  (fixtures)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


# ------------------------------------------------------------- no media needed

def _fp(**over):
    info = {"size": 1_000_000_000, "duration": 1440.04, "video": [{"index": 0, "codec": "h264", "duration": 1440}],
            "audio": [{"index": 1, "codec": "aac", "channels": 2, "duration": 1440, "lang": "jpn", "default": True}],
            "subs": [{"index": 2, "codec": "ass", "lang": "eng", "external": False},
                     {"index": None, "codec": "subrip", "external": True}]}
    info.update(over)
    c = scanner.Ctx(file={"id": 1, "arr_file_id": 7}, item={}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.info = info
    return scanner.fingerprint(c)


def test_header_edits_keep_the_fingerprint():
    a = _fp()
    retagged = _fp(audio=[{"index": 1, "codec": "aac", "channels": 2, "duration": 1440, "lang": "eng",
                           "default": False, "title": "English"}], size=1_000_000_512)
    assert scanner.same_file(a, retagged)
    # sidecar subtitles come and go without touching the file
    assert scanner.same_file(a, _fp(subs=[{"index": 2, "codec": "ass", "external": False}]))


def test_remux_or_redownload_changes_it():
    a = _fp()
    assert not scanner.same_file(a, _fp(size=1_200_000_000))
    assert not scanner.same_file(a, _fp(audio=[{"index": 1, "codec": "eac3", "channels": 6, "duration": 1440}]))
    assert not scanner.same_file(a, _fp(duration=1500))
    assert not scanner.same_file(None, a)


def _bare_file():
    iid = db.x("INSERT INTO items(source, arr_id, kind, title) VALUES ('sonarr', 1, 'series', 'X')")
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, 1, '/x.mkv', '[]', 0)",
                (iid,))


def _issue(fid, code, details="{}"):
    db.x("INSERT INTO issues(file_id, code, severity, status, details, created_at) VALUES (?,?,'warning','open',?,?)",
         (fid, code, details, time.time()))


def test_full_flag_sticks_in_the_queue():
    fid = _bare_file()
    scheduler.enqueue([fid], "standard", "manual", full=True)
    scheduler.enqueue([fid], "quick", "retag")  # a later quick request must not cancel the full one
    assert db.q1("SELECT full FROM queue WHERE file_id=?", (fid,))["full"] == 1


def test_forced_kinds_from_open_issues():
    fid = _bare_file()
    for code, details in (("truncated", "{}"), ("language_unconfirmed", "{}"), ("subs_out_of_sync", "{}"),
                          ("audio_mislabeled", '{"a_index": 2}'), ("subs_missing", "{}")):
        _issue(fid, code, details)
    assert scanner.forced_kinds(fid) == {"decode", "language", "transcribe", "language:2"}


# ------------------------------------------------------------- real scans

def _counted(monkeypatch):
    calls = {}

    def count(obj, name):
        real = getattr(obj, name)

        def wrapped(*a, **k):
            calls[name] = calls.get(name, 0) + 1
            return real(*a, **k)

        monkeypatch.setattr(obj, name, wrapped)

    for obj, name in ((media, "decode_segment"), (media, "extract_audio_multi"), (media, "sub_packets"),
                      (stt.WHISPER, "detect_language"), (stt.WHISPER, "transcribe")):
        count(obj, name)
    return calls


def _result(res):
    assert res["status"] == "done", res.get("error")
    return db.unj(db.q1("SELECT result FROM scans WHERE id=?", (res["scan_id"],))["result"])


def _open_codes(fid):
    return {r["code"] for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (fid,))}


@needs_ffmpeg
def test_quick_rescan_reuses_measurements(video, fake_whisper, monkeypatch):  # noqa: F811
    _small_profiles()
    fid = _add(video)
    first = scanner.scan_file(fid, "standard")
    r1 = _result(first)
    assert r1["rescan"]["mode"] == "full"
    codes = _open_codes(fid)
    calls = _counted(monkeypatch)
    r = _result(scanner.scan_file(fid, "standard"))
    assert r["rescan"]["mode"] == "quick", r["rescan"]
    assert r["rescan"]["from_scan"] == first["scan_id"]
    assert r["segments"] == r1["segments"]
    assert "decode_segment" not in calls and "detect_language" not in calls
    # dubtitles is open, so the transcripts (and the subtitle text they are compared with) are made again
    assert r["rescan"]["rerun"] == ["transcribe"] and calls.get("transcribe")
    assert _open_codes(fid) == codes
    # only the newest scan keeps the cache
    assert "cache" not in _result(first) and "cache" in r


@needs_ffmpeg
def test_quick_rescan_without_open_issues_measures_nothing(video, fake_whisper, monkeypatch):  # noqa: F811
    _small_profiles()
    fid = _add(video)
    scanner.scan_file(fid, "standard")
    db.x("UPDATE issues SET status='resolved' WHERE file_id=?", (fid,))
    calls = _counted(monkeypatch)
    r = _result(scanner.scan_file(fid, "standard"))
    assert r["rescan"]["rerun"] == []
    assert calls == {}
    assert "dubtitles" in _open_codes(fid)  # the verdicts still come out of the reused results


@needs_ffmpeg
def test_open_decode_issue_reruns_decode_only(video, fake_whisper, monkeypatch):  # noqa: F811
    _small_profiles()
    fid = _add(video)
    scanner.scan_file(fid, "standard")
    db.x("UPDATE issues SET status='resolved' WHERE file_id=?", (fid,))
    _issue(fid, "decode_errors")
    calls = _counted(monkeypatch)
    r = _result(scanner.scan_file(fid, "standard"))
    assert r["rescan"]["rerun"] == ["decode"]
    assert calls.get("decode_segment") and "detect_language" not in calls and "transcribe" not in calls


@needs_ffmpeg
def test_mislabeled_track_reruns_language_on_that_track(video, fake_whisper, monkeypatch):  # noqa: F811
    _small_profiles()
    fid = _add(video)
    scanner.scan_file(fid, "standard")
    db.x("UPDATE issues SET status='resolved' WHERE file_id=?", (fid,))
    _issue(fid, "audio_mislabeled", '{"a_index": 1}')
    calls = _counted(monkeypatch)
    seen = []
    real = media.extract_audio_multi
    monkeypatch.setattr(media, "extract_audio_multi",
                        lambda p, idxs, s, d: seen.append(list(idxs)) or real(p, idxs, s, d))
    r = _result(scanner.scan_file(fid, "standard"))
    assert r["rescan"]["rerun"] == ["language:1"]
    assert seen and all(i == [1] for i in seen) and calls.get("detect_language")
    assert "decode_segment" not in calls


@needs_ffmpeg
def test_full_rescan_setting_and_changed_file(video, fake_whisper):  # noqa: F811
    _small_profiles()
    fid = _add(video)
    scanner.scan_file(fid, "standard")
    assert _result(scanner.scan_file(fid, "standard", full=True))["rescan"]["mode"] == "full"
    settings.save({"scan": {"quick_rescan": False}})
    assert _result(scanner.scan_file(fid, "standard"))["rescan"]["mode"] == "full"
    settings.save({"scan": {"quick_rescan": True}})
    r = _result(scanner.scan_file(fid, "standard"))
    assert r["rescan"]["mode"] == "quick", r["rescan"]
    db.x("UPDATE files SET arr_file_id=99 WHERE id=?", (fid,))  # the Arr imported a new file
    assert _result(scanner.scan_file(fid, "standard"))["rescan"]["mode"] == "full"
    # a profile that samples more than the cached scan did needs a full scan
    settings.save({"scan": {"profiles": {"deep": {"segments": 6, "seconds": 15, "subtitles": True,
                                                  "full_decode": False}}}})
    assert _result(scanner.scan_file(fid, "deep"))["rescan"]["mode"] == "full"
    # and a smaller one reuses the bigger scan's segments
    r = _result(scanner.scan_file(fid, "standard"))
    assert r["rescan"]["mode"] == "quick", r["rescan"]
