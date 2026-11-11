"""Subtitle verdicts: one issue per track, offset-aware text check, wrong-content grouping."""
import os
import shutil
import subprocess
import time

import pytest

from anotherr import actions, analysis, db, media, scanner, stt
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


def test_turned_off_checks_skip_their_steps_and_timings_add_up(d, fake_whisper):
    from fastapi.testclient import TestClient

    from anotherr import api, settings

    v = make_video(d, "checksoff", [(440, "eng")], [([(a + 4, b + 4) for a, b in CUES], TEXTS, "eng", "English")])
    settings.save({**settings.get(), "scan": {**settings.get()["scan"], "checks": {"subtitle_sync": False, "interlace": False}}})
    fid, codes = scan(v)
    assert "subs_out_of_sync" not in codes  # the sync check didn't run
    res = db.unj(db.scalar("SELECT result FROM scans WHERE file_id=? ORDER BY id DESC", (fid,)))
    assert "interlace check" not in res["timings"] and not any(k.startswith("subtitle sync") for k in res["timings"])
    assert res["sync_skipped"].startswith("subtitle sync check turned off")
    t = TestClient(api.app).get("/api/stats/timings").json()
    assert t["scans"] >= 1 and t["steps"] and all(s["control"] for s in t["steps"])
    assert abs(sum(s["share"] for s in t["steps"]) - 100) < 1


# ---- embedded subtitle density: mkvmerge statistics tags beat the (unreliable) window read

def _classify(frames=None, title="", forced=False, file_s=600.0, wcues=0, wpk=5):
    return analysis.classify_embedded(frames, file_s, title, forced, 3.0, wcues, 60.0, wpk)


def test_classify_embedded_rules():
    assert _classify(frames=544, file_s=2641)[:2] == ("full", "tags")            # SG-1 "EN": 12/min
    assert _classify(frames=7, file_s=2641, forced=True)[0] == "signs"           # forced flag always signs
    assert _classify(frames=544, title="Forced")[0] == "signs"
    k, src, n, pm = _classify(frames=7, file_s=2641)                              # short forced-only track stays sparse
    assert (k, src, n) == ("signs", "tags", 7) and pm == 0.2
    assert _classify(frames=236, file_s=637)[0] == "full"                         # SpongeBob: 22/min
    assert _classify(frames=2, title="SDH")[0] == "signs"                         # tags clearly under threshold win
    assert _classify(title="SDH", wcues=0)[:2] == ("full", "window")              # title hint without tags
    assert _classify(title="English (CC)")[0] == "full"
    assert _classify(title="accessible")[0] != "full"                             # whole words only
    assert _classify(wcues=0, wpk=0)[0] == "unknown"                              # window read failed everywhere
    assert _classify(wcues=1, wpk=0)[0] == "unknown"
    assert _classify(wcues=0, wpk=9)[0] == "empty"                                # other tracks read fine, this one is empty
    assert _classify(wcues=1, wpk=9)[0] == "signs"
    assert _classify(wcues=50, wpk=50)[0] == "full"


def test_summarize_keeps_statistics_tags():
    probe = {"format": {"duration": "2641.0"}, "streams": [
        {"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {},
         "tags": {"language": "eng", "NUMBER_OF_FRAMES": "544", "DURATION": "00:44:01.000000000"}},
        {"index": 1, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {"forced": 1},
         "tags": {"language": "eng", "NUMBER_OF_FRAMES-eng": "7", "DURATION-eng": "00:30:00.0"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {}, "tags": {"language": "eng"}}]}
    a, b, c = media.summarize(probe)["subs"]
    assert (a["frames"], a["tag_duration"]) == (544, 2641.0)
    assert (b["frames"], b["tag_duration"]) == (7, 1800.0)
    assert c["frames"] is None and c["tag_duration"] == 0


@pytest.fixture
def tagged(monkeypatch):
    """Inject mkvmerge-style NUMBER_OF_FRAMES tags (ffmpeg's muxer never writes them): {s_index: frames}."""
    tags = {}
    real = media.ffprobe

    def fake(path, *a, **kw):
        probe = real(path, *a, **kw)
        n = -1
        for s in probe.get("streams", []):
            if s.get("codec_type") == "subtitle":
                n += 1
                if n in tags:
                    s.setdefault("tags", {})["NUMBER_OF_FRAMES"] = str(tags[n])
        return probe

    monkeypatch.setattr(media, "ffprobe", fake)
    return tags


def _tracks(fid):
    r = db.q1("SELECT result FROM scans WHERE file_id=? ORDER BY id DESC", (fid,))
    return db.unj(r["result"])["subtitle_tracks"]


def test_tags_full_means_no_signs_only(d, fake_whisper, tagged, monkeypatch):
    path = make_video(d, "tagfull", [(440, "eng")], [(CUES[:3], ["x"] * 3, "eng", "EN")])
    tagged[0] = 130  # 90 s file: 87/min; the 3 cues in the file would have read as signs by the window route
    fid, codes = scan(path, anime=1)
    assert "subs_signs_only" not in codes and "subs_density_unknown" not in codes
    t = _tracks(fid)[0]
    assert (t["kind"], t["density_source"], t["cues"]) == ("full", "tags", 130)


def test_forced_flag_is_signs_even_with_many_frames(d, fake_whisper, tagged):
    path = make_video(d, "tagforced", [(440, "eng")], [(CUES, TEXTS, "eng", "English")])
    # flag the only track forced via a re-mux
    forced = os.path.join(d, "tagforced2.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0", "-c", "copy", "-disposition:s:0", "forced", forced],
                   check=True)
    tagged[0] = 500
    fid, codes = scan(forced, anime=1)
    assert "subs_signs_only" in codes
    assert _tracks(fid)[0]["kind"] == "signs"


def test_sparse_tags_raise_signs_only(d, fake_whisper, tagged):
    path = make_video(d, "tagsparse", [(440, "eng")], [(CUES, TEXTS, "eng", "English")])
    tagged[0] = 2  # 1.3/min over the 90 s file, although the window read sees plenty of cues
    fid, codes = scan(path, anime=1)
    assert "subs_signs_only" in codes
    t = _tracks(fid)[0]
    assert (t["kind"], t["density_source"], t["cues"]) == ("signs", "tags", 2)


def test_window_read_failure_is_unknown_not_signs(d, fake_whisper, monkeypatch):
    path = make_video(d, "winfail", [(440, "eng")], [(CUES, TEXTS, "eng", "English"), (CUES[:2], ["s"] * 2, "eng", "Forced")])
    monkeypatch.setattr(media, "sub_packets", lambda *a, **kw: {})
    fid, codes = scan(path, anime=1)
    assert "subs_signs_only" not in codes and "subs_missing" not in codes
    assert "subs_density_unknown" in codes
    assert [t["kind"] for t in _tracks(fid)] == ["unknown", "signs"]


def test_sdh_title_without_tags_is_full(d, fake_whisper, monkeypatch):
    path = make_video(d, "sdh", [(440, "eng")], [(CUES[:2], ["x"] * 2, "eng", "SDH")])
    fid, codes = scan(path, anime=1)
    assert "subs_signs_only" not in codes
    assert _tracks(fid)[0]["kind"] == "full"


def test_sidecar_label_ignores_episode_title_words():
    video = "/tv/Pokemon/Pokemon - S05E34 - Same Old Song and Dance SDTV.mkv"
    name = "Pokemon - S05E34 - Same Old Song and Dance SDTV.en.srt"
    label = analysis.sidecar_label(name, video)
    assert label == ".en.srt"
    assert analysis.classify_track(140, 1300, label, False, 3.0) == "full"
    assert analysis.classify_track(140, 1300, name, False, 3.0) == "signs"  # the old behaviour
    assert analysis.sidecar_label("x.forced.en.srt", "/v/y.mkv") == "x.forced.en.srt"


def test_full_title_beats_forced_flag_but_not_sign_words():
    assert not analysis.signs_by_label("English full", True)
    assert not analysis.signs_by_label("Full", True)
    assert analysis.signs_by_label("", True)
    assert analysis.signs_by_label("Signs & Songs full", False)
    assert analysis.signs_by_label("English forced", False)
    assert analysis.classify_embedded(500, 1400, "English full", True, 3.0, 0, 60.0, 5)[0] == "full"


def test_positioned_only_dialogue_is_not_dropped():
    t = r"{\an7}{\pos(38,28)}My very first clothing exchange."
    assert analysis.clean_cue(1.0, 3.0, "Default", t) is None
    assert analysis.clean_cue(1.0, 3.0, "Default", t, allow_pos=True) is not None
    strict = []
    loose = [analysis.clean_cue(i * 4.0, i * 4.0 + 2, "Default", t, allow_pos=True) for i in range(10)]
    assert analysis.pick_dialogue(strict, loose) == loose
    short = [analysis.clean_cue(i * 4.0, i * 4.0 + 2, "Default", r"{\pos(1,2)}Exit", allow_pos=True) for i in range(10)]
    assert analysis.pick_dialogue([], short) == []
    karaoke = [analysis.clean_cue(1.0, 3.0, "Default", r"{\k20}la la la la la la la la", allow_pos=True)]
    assert karaoke == [None]
