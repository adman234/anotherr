"""Subtitle verdicts: one issue per track, offset-aware text check, wrong-content grouping."""
import os
import shutil
import subprocess
import time

import pytest

from anotherr import actions, analysis, db, scanner, stt
from test_scan import CUES, TEXTS, _small_profiles, _srt, fake_whisper  # noqa: F401

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

OTHER = [f"weather report {k} rain valleys northern winds temperatures expected tomorrow morning" for k in range(29)]


def make_video(d, name, audios, subs):
    """audios: [(freq, lang)], subs: [(cues, texts, lang, title)]"""
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=90"]
    for f, _ in audios:
        args += ["-f", "lavfi", "-i", f"sine=f={f}:d=90"]
    for i, (cues, texts, _, _) in enumerate(subs):
        p = os.path.join(d, f"{name}.{i}.srt")
        open(p, "w").write(_srt(cues, texts))
        args += ["-i", p]
    for i in range(1 + len(audios) + len(subs)):
        args += ["-map", str(i)]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt"]
    for i, (_, lang) in enumerate(audios):
        args += [f"-metadata:s:a:{i}", f"language={lang}"]
    for i, (_, _, lang, title) in enumerate(subs):
        args += [f"-metadata:s:s:{i}", f"language={lang}", f"-metadata:s:s:{i}", f"title={title}"]
    out = os.path.join(d, f"{name}.mkv")
    subprocess.run(args + [out], check=True)
    return out


def scan(path, original="eng", anime=0):
    _small_profiles()
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, is_anime, original_language, runtime) "
               "VALUES ('sonarr', ?, 'Show', 'series', ?, ?, 2)", (int(time.time() * 1000) % 10**9, anime, original))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, size, episodes, label, first_seen) "
               "VALUES (?, 1, ?, ?, 1, '[{\"id\": 1, \"season\": 1, \"episode\": 1, \"title\": \"Pilot\"}]', 'S01E01', ?)",
               (iid, path, path, time.time()))
    res = scanner.scan_file(fid, "standard", seed=3)
    assert res["status"] == "done", res.get("error")
    return fid, [i["code"] for i in res["issues"]]


@pytest.fixture(scope="module")
def d(tmp_path_factory):
    return str(tmp_path_factory.mktemp("subs"))


def test_track_verdict_rules():
    assert analysis.track_verdict("offset", 0.8) == "offset"     # text matches once shifted -> one offset issue
    assert analysis.track_verdict("offset", 0.05) == "mismatch"  # still doesn't match -> one mismatch issue
    assert analysis.track_verdict("mismatch", 0.9) == "ok"       # text proves it's the right content
    assert analysis.track_verdict("ok", 0.1) == "mismatch"
    assert analysis.track_verdict("ok", None) == "ok"


def test_offset_with_matching_text_is_one_offset_issue(d, fake_whisper):
    v = make_video(d, "offset", [(440, "eng")], [([(a + 4, b + 4) for a, b in CUES], TEXTS, "eng", "English")])
    _, codes = scan(v)
    subs = [c for c in codes if c.startswith("subs_")]
    assert subs == ["subs_out_of_sync"], codes


def test_offset_with_wrong_text_is_one_mismatch_issue(d, fake_whisper):
    v = make_video(d, "wrongtext", [(440, "eng")], [([(a + 4, b + 4) for a, b in CUES], OTHER, "eng", "English")])
    _, codes = scan(v)
    subs = [c for c in codes if c.startswith("subs_")]
    assert subs == ["subs_mismatch"], codes


def test_labeled_dubtitles_are_info_when_a_translation_exists(d, fake_whisper):
    v = make_video(d, "dubs", [(880, "jpn"), (440, "eng")],
                   [(CUES, TEXTS, "eng", "English (Dubtitle) [CR]"), (CUES, OTHER, "eng", "English")])
    _, codes = scan(v, original="jpn", anime=1)
    assert "dubtitles_labeled" in codes and "dubtitles" not in codes, codes


def test_every_track_failing_is_wrong_content_with_redownload():
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series"}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.issue("subtitles", "subs_mismatch", "error", "Subtitles don't match this audio (a; b)",
            all_tracks_failed=True, track_count=2)
    c.issue("subtitles", "subs_out_of_sync", "warning", "offset")
    scanner.consolidate(c)
    assert [i["code"] for i in c.issues] == ["wrong_content"]
    # ...and the proposal is a redownload, never a resub
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 99, 'Courage', 'series')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, 1, '/tv/c.mkv', ?)",
               (iid, time.time()))
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'wrong_content', 'error', 'open', ?)",
         (fid, time.time()))
    actions.propose_for_file(fid)
    assert [a["kind"] for a in db.q("SELECT kind FROM actions WHERE file_id=?", (fid,))] == ["redownload"]


def test_single_failing_track_without_other_anomaly_stays_a_subtitle_issue():
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series"}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.issue("subtitles", "subs_mismatch", "warning", "Subtitles don't match (x)", all_tracks_failed=True, track_count=1)
    scanner.consolidate(c)
    assert [i["code"] for i in c.issues] == ["subs_mismatch"]


def test_no_dialogue_skips_subtitle_requirements(d, fake_whisper, monkeypatch):
    monkeypatch.setattr(stt, "speech_intervals", lambda audio: [])
    v = make_video(d, "silentfilm", [(440, "und")], [])
    _, codes = scan(v, original="kor")
    assert "no_dialogue" in codes and "subs_missing" not in codes, codes


def test_truncated_suppresses_downstream():
    c = scanner.Ctx(file={"id": 1}, item={"kind": "movie"}, cfg={}, profile="standard", pcfg={}, seed=1)
    for code in ("truncated", "silent_audio", "decode_errors_severe", "subs_mismatch", "original_audio_missing"):
        c.issue("x", code, "error", code)
    scanner.consolidate(c)
    assert [i["code"] for i in c.issues] == ["truncated", "original_audio_missing"]
    assert {s["code"] for s in c.result["suppressed"]} == {"silent_audio", "decode_errors_severe", "subs_mismatch"}


def test_duration_plus_sub_mismatch_becomes_wrong_content_but_duration_alone_stays():
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series"}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.issue("integrity", "duration_short", "error", "short")
    c.issue("subtitles", "subs_mismatch", "warning", "Subtitles don't match (x)", all_tracks_failed=True, track_count=1)
    c.issue("multi_episode", "segment_break_not_found", "info", "no break")
    scanner.consolidate(c)
    assert [i["code"] for i in c.issues] == ["wrong_content"]
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series"}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.issue("integrity", "duration_short", "error", "short")
    scanner.consolidate(c)
    assert [i["code"] for i in c.issues] == ["duration_short"]
