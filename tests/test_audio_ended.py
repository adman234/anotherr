"""Audio that ends before the video: windows past its end must not map it (ffmpeg 6 buffers every video frame
waiting for audio frames that never come), and a runaway child is killed instead of OOM-killing the container."""
import shutil
import subprocess
import sys

import pytest

from anotherr import media, scanner

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@pytest.fixture(scope="module")
def short_audio(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("aend") / "short_audio.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=60",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", out], check=True)
    return out


@needs_ffmpeg
def test_summarize_audio_end(short_audio):
    info = media.summarize(media.ffprobe(short_audio))
    assert info["audio"][0]["end"] == pytest.approx(20, abs=1.5)
    assert info["video"][0]["end"] == pytest.approx(60, abs=1.5)
    assert media.live_audio(info, 5) == [0]
    assert media.live_audio(info, 40) == []


def test_live_audio_unknown_end_counts():
    info = {"audio": [{"a_index": 0, "end": None}, {"a_index": 1, "end": 10.0}, {"a_index": 2, "end": 100.0}]}
    assert media.live_audio(info, 9.5) == [0, 2]
    assert media.live_audio(info, 50) == [0, 2]


@needs_ffmpeg
def test_decode_segment_maps_only_live_audio(short_audio, monkeypatch):
    real, cmds = media.run, []

    def spy(cmd, *a, **k):
        cmds.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(media, "run", spy)
    info = media.summarize(media.ffprobe(short_audio))
    late = media.decode_segment(short_audio, 40, 15, hw="off", audio_maps=media.live_audio(info, 40))
    assert "0:a?" not in cmds[-1] and not any(c.startswith("0:a:") for c in cmds[-1])
    assert late["decoded"] == pytest.approx(15, abs=1.5)
    early = media.decode_segment(short_audio, 5, 10, hw="off", audio_maps=media.live_audio(info, 5))
    assert "0:a:0" in cmds[-1]
    assert early["decoded"] == pytest.approx(10, abs=1.5)
    media.decode_segment(short_audio, 5, 10, hw="off")
    assert "0:a?" in cmds[-1]


@needs_ffmpeg
def test_audio_ends_early_issue(short_audio):
    info = media.summarize(media.ffprobe(short_audio))
    info["duration"] = 60.0
    ctx = scanner.Ctx(file={}, item={"kind": "movie"}, cfg={}, profile="standard", pcfg={}, seed=0, info=info)
    scanner.check_streams(ctx)
    assert any(i["code"] == "stream_duration_mismatch" and "Audio track" in i["message"] for i in ctx.issues)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_memory_watchdog_kills_child():
    cmd = [sys.executable, "-c", "x=bytearray(400*1024*1024); x[::4096]=b'1'*len(x[::4096]); import time; time.sleep(10)"]
    p = media.run(cmd, timeout=60, mem_limit_mb=100)
    assert p.mem_killed and "used more than 100 MB" in p.mem_killed
    assert "used more than 100 MB" in p.stderr
    assert p.returncode != 0


def test_no_limit_leaves_child_alone():
    p = media.run([sys.executable, "-c", "print('hi')"], timeout=30, mem_limit_mb=100)
    assert p.returncode == 0 and p.stdout.strip() == "hi" and p.mem_killed is None
    b = media.run([sys.executable, "-c", "print('hi')"], timeout=30, binary=True, mem_limit_mb=0)
    assert b.stdout.strip() == b"hi"
