"""frozen_video: the Nov 2026 false positives came from comparing keyframes padded with duplicates."""
import shutil
import subprocess

import pytest

from anotherr import media, scanner

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _enc(out, *args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args, "-c:v", "libx264", "-preset", "ultrafast", str(out)], check=True)
    return str(out)


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("freeze")
    # constant motion with a 10 s GOP - the pattern that used to report a freeze every keyframe interval
    moving = _enc(d / "moving.mkv", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=60", "-g", "240")
    # 25 s held frame in the middle
    frozen = _enc(d / "frozen.mkv", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=40", "-filter_complex",
                  "[0:v]split=3[x][y][z];[x]trim=0:20,setpts=PTS-STARTPTS[a];"
                  "[y]trim=20:21,setpts=PTS-STARTPTS,loop=loop=600:size=24,trim=0:25,setpts=PTS-STARTPTS[b];"
                  "[z]trim=21:40,setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-g", "48")
    # static picture where only a small region moves (a mouth in a medium shot)
    low = _enc(d / "low.mkv", "-f", "lavfi", "-i", "color=c=0x606870:s=640x360:r=24:d=20", "-f", "lavfi",
               "-i", "color=c=0xd0c0b0:s=24x12:r=24:d=20", "-filter_complex",
               "[0][1]overlay=x='300+6*sin(t*5)':y='190+2*sin(t*7)'", "-g", "240")
    return {"moving": moving, "frozen": frozen, "low": low}


def test_moving_video_has_no_candidates(clips):
    r = media.video_checks(clips["moving"], 5, 50)
    assert r["keyframes"] >= 4 and r["freeze_candidates"] == []


def test_real_freeze_is_screened_and_confirmed(clips):
    cands = media.video_checks(clips["frozen"], 0, 64)["freeze_candidates"]
    assert cands and cands[0][1] - cands[0][0] >= 10
    c = media.confirm_freeze(clips["frozen"], cands[0][0], 24)
    assert c["confirmed"] and c["longest_s"] >= 20


def test_small_motion_is_not_a_freeze(clips):
    assert not media.confirm_freeze(clips["low"], 0, 20)["confirmed"]
    assert not media.confirm_freeze(clips["moving"], 5, 30)["confirmed"]


def _ctx(dur=1800):
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series", "title": "T"}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.info = {"duration": dur}
    c.path = "x.mkv"
    return c


def test_one_window_is_info_two_windows_warning_and_edges_skipped(monkeypatch):
    monkeypatch.setattr(media, "confirm_freeze", lambda p, s, d: {"confirmed": True, "freeze": (s, s + 12),
                        "window": (s, d), "unchanged_fraction": 1.0, "longest_s": 12})
    ctx = _ctx()
    res = [{}, {"freeze_candidates": [(60, 75), (600, 615)]}, {"freeze_candidates": []}, {}]
    scanner.check_freeze(ctx, res, [1, 2])
    assert [i["severity"] for i in ctx.issues] == ["info"]  # the 60 s candidate is inside the 2-minute edge
    assert len(ctx.result["freeze_check"]) == 1 and ctx.issues[0]["details"]["evidence_ts"]
    ctx = _ctx()
    res = [{}, {"freeze_candidates": [(600, 615)]}, {"freeze_candidates": [(900, 915)]}, {}]
    scanner.check_freeze(ctx, res, [1, 2])
    assert [i["severity"] for i in ctx.issues] == ["warning"]
