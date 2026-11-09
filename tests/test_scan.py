import os
import shutil
import subprocess
import time

import numpy as np
import pytest

from anotherr import actions, db, media, scanner, settings, stt

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

CUES = [(1 + 3 * k, 3 + 3 * k) for k in range(29)]
TEXTS = [f"line number {k} about the dragon castle princess soldiers journey" for k in range(29)]


def _srt(cues, texts):
    def ts(t):
        return f"00:{int(t // 60):02d}:{int(t % 60):02d},{int(round((t % 1) * 1000)):03d}"
    return "\n".join(f"{i + 1}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, ((a, b), t) in enumerate(zip(cues, texts)))


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    (d / "full.srt").write_text(_srt(CUES, TEXTS))
    (d / "signs.srt").write_text(_srt(CUES[:3], ["SIGN"] * 3))
    out = d / "Show - S01E01.mkv"
    vcodec = ["-c:v", "libx264", "-preset", "ultrafast"] if "libx264" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout else ["-c:v", "mpeg4"]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=90",
                    "-f", "lavfi", "-i", "sine=f=880:d=90", "-f", "lavfi", "-i", "sine=f=440:d=90",
                    "-i", str(d / "full.srt"), "-i", str(d / "signs.srt"),
                    "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-map", "4", *vcodec, "-c:a", "aac", "-c:s", "srt",
                    "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
                    "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
                    "-metadata:s:s:1", "title=Signs & Songs", "-disposition:s:1", "forced", str(out)], check=True)
    return out


@pytest.fixture
def fake_whisper(monkeypatch):
    # Remember which window each extracted clip came from. The scanner reads every window first and
    # analyses afterwards, so "the last window read" would be the wrong start for most clips.
    starts = {}
    real_extract = media.extract_audio_multi

    def extract(path, idxs, s, d):
        out = real_extract(path, idxs, s, d)
        for audio in out.values():
            starts[id(audio)] = (s, audio)  # keep a reference so the id stays unique
        return out

    def vad(audio):
        s = starts[id(audio)][0]
        e = s + audio.size / stt.SR
        return [(max(a, s) - s, min(b, e) - s) for a, b in CUES if b > s and a < e]

    def detect(audio):
        spec = np.abs(np.fft.rfft(audio[: stt.SR * 4]))
        freq = np.argmax(spec) * stt.SR / (2 * (spec.size - 1))
        return ("ja", 0.97, []) if freq > 660 else ("en", 0.96, [])

    def transcribe(audio, language):
        s = starts[id(audio)][0]
        return [(a - s, b - s, t) for (a, b), t in zip(CUES, TEXTS)]

    monkeypatch.setattr(media, "extract_audio_multi", extract)
    monkeypatch.setattr(stt, "speech_intervals", vad)
    monkeypatch.setattr(stt.WHISPER, "detect_language", detect)
    monkeypatch.setattr(stt.WHISPER, "transcribe", transcribe)


def _add(path, original="jpn", anime=1, override=""):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, is_anime, original_language, runtime, language_override) "
               "VALUES ('sonarr', 1, 'Test Show', 'series', ?, ?, 2, ?)", (anime, original, override))
    eps = '[{"id": 11, "season": 1, "episode": 1, "title": "Pilot", "runtime": 0}]'
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, size, episodes, label, first_seen) "
               "VALUES (?, 5, ?, ?, ?, ?, 'S01E01', ?)", (iid, str(path), str(path), os.path.getsize(path), eps, time.time()))
    return fid


def _small_profiles():
    settings.save({"scan": {"profiles": {"standard": {"segments": 4, "seconds": 15, "subtitles": True, "full_decode": False},
                                         "deep": {"segments": 4, "seconds": 15, "subtitles": True, "full_decode": True}}}})


def test_anime_dual_audio_good_file(video, fake_whisper):
    _small_profiles()
    fid = _add(video)
    res = scanner.scan_file(fid, "standard")
    assert res["status"] == "done", res.get("error")
    codes = {i["code"] for i in res["issues"]}
    assert "original_audio_missing" not in codes
    assert "subs_missing" not in codes and "subs_signs_only" not in codes
    assert "decode_errors_severe" not in codes and "truncated" not in codes
    # English subs match the English dub word for word -> dubtitles
    assert "dubtitles" in codes
    scan = db.q1("SELECT result FROM scans WHERE id=?", (res["scan_id"],))
    r = db.unj(scan["result"])
    assert r["dual_audio"] is True
    kinds = {t["title"]: t["kind"] for t in r["subtitle_tracks"]}
    assert kinds["Signs & Songs"] == "signs"
    assert "full" in kinds.values()
    assert r["subtitle_sync"][0]["verdict"] == "ok"
    f = db.q1("SELECT status FROM files WHERE id=?", (fid,))
    assert f["status"] == "warning"
    acts = db.q("SELECT kind FROM actions WHERE file_id=?", (fid,))
    assert [a["kind"] for a in acts] == ["resub"]


def test_override_and_missing_language(video, fake_whisper):
    _small_profiles()
    fid = _add(video, original="kor", anime=0)
    res = scanner.scan_file(fid, "standard")
    codes = {i["code"] for i in res["issues"]}
    assert "original_audio_missing" not in codes  # English + Japanese present, nothing requested: either
    # original-language audio requested: the file only has Japanese + English, so Korean is missing (a warning)
    db.x("UPDATE items SET audio_request_override='original'")
    res = scanner.scan_file(fid, "standard")
    codes = {i["code"] for i in res["issues"]}
    assert "original_audio_missing" in codes
    assert {i["severity"] for i in res["issues"] if i["code"] == "original_audio_missing"} == {"warning"}
    acts = db.q("SELECT kind, status FROM actions WHERE file_id=? ORDER BY kind", (fid,))
    # no Korean track, so English should be the default: the jpn track is the default here, hence a flag change too
    assert acts == [{"kind": "accept_season_audio", "status": "proposed"},
                    {"kind": "redownload", "status": "proposed"},
                    {"kind": "set_default_audio", "status": "proposed"}]  # automation is off by default
    # override to English -> no language problem, subs no longer required
    db.x("UPDATE items SET language_override='eng'")
    res = scanner.scan_file(fid, "standard")
    codes = {i["code"] for i in res["issues"]}
    assert "original_audio_missing" not in codes
    assert "default_audio_not_preferred" in codes  # default track is the jpn one


def test_truncated_file(video, fake_whisper, tmp_path):
    _small_profiles()
    bad = tmp_path / "bad.mkv"
    data = video.read_bytes()
    bad.write_bytes(data[: int(len(data) * 0.6)])
    fid = _add(bad)
    res = scanner.scan_file(fid, "standard")
    codes = {i["code"] for i in res["issues"]}
    assert codes & {"truncated", "decode_errors_severe", "stream_duration_mismatch", "unreadable"}, codes


def test_review_queued_for_multi_episode(video, fake_whisper):
    _small_profiles()
    fid = _add(video)
    db.x("UPDATE files SET episodes=? WHERE id=?",
         ('[{"id": 11, "season": 1, "episode": 1, "title": "A", "runtime": 1},'
          ' {"id": 12, "season": 1, "episode": 2, "title": "B", "runtime": 1}]', fid))
    res = scanner.scan_file(fid, "standard")
    assert res["status"] == "done", res.get("error")
    rv = db.q("SELECT kind, timestamps FROM reviews WHERE file_id=?", (fid,))
    assert rv and rv[0]["kind"] == "title_cards"
    assert len(db.unj(rv[0]["timestamps"])) >= 4


def test_automation_runs_when_enabled(video, fake_whisper, monkeypatch):
    _small_profiles()
    calls = []
    monkeypatch.setattr(actions, "_resub", lambda f, i: calls.append(f["id"]) or "ok")
    settings.save({"automation": {"enabled": True, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:00"}]}})
    fid = _add(video)
    scanner.scan_file(fid, "standard")
    assert calls == [fid]
    assert db.q1("SELECT status, auto FROM actions WHERE file_id=?", (fid,)) == {"status": "done", "auto": 1}


def test_sub_packets_keep_cues_at_window_end(video):
    """ffprobe 'start%+dur' measures from the seeked keyframe and dropped the last cues of each window."""
    pk = media.sub_packets(str(video), [(27.1, 15.0), (48.1, 15.0)])
    s_full = min(pk)  # first subtitle stream = the full English track
    starts = {round(p["start"]) for p in pk[s_full]}
    assert {28, 31, 34, 37, 40} <= starts and {49, 52, 55, 58, 61} <= starts
    texts = [media.text_from_packet("subrip", p["data"])[1] for p in pk[s_full] if round(p["start"]) == 40]
    assert texts == [TEXTS[13]]
