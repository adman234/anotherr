"""Regression cases from the Nov 5 review."""
import os
import shutil
import subprocess

import pytest

from anotherr import media, ocr, scanner

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
needs_tools = pytest.mark.skipif(not (shutil.which("ffmpeg") and ocr.available() and os.path.exists(FONT)),
                                 reason="ffmpeg, tesseract and a font are needed")


def test_series_logo_never_matches_a_special():
    eps = [{"id": 1, "season": 0, "episode": 1, "title": "The Clone Wars"},
           {"id": 2, "season": 5, "episode": 1, "title": "Revival"}]
    text = ["STAR WARS\nTHE CLONE WARS", "Revival"]
    found = ocr.match(text, eps, "Star Wars: The Clone Wars")
    assert [m["id"] for m in found] == [2]


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("nov5")
    hard = str(d / "hard.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=40", "-vf",
                    f"drawtext=fontfile={FONT}:text='We have to get to the castle before dark':fontsize=40:"
                    "fontcolor=white:borderw=3:x=(w-tw)/2:y=h-90", "-c:v", "libx264", "-preset", "ultrafast", hard],
                   check=True)
    clean = str(d / "clean.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=40",
                    "-c:v", "libx264", "-preset", "ultrafast", clean], check=True)
    cut = str(d / "cut.mkv")
    data = open(clean, "rb").read()
    open(cut, "wb").write(data[: len(data) // 2])
    return {"hard": hard, "clean": clean, "cut": cut}


def _ctx(path):
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series", "title": "X"}, cfg={}, profile="standard", pcfg={}, seed=1)
    c.path = path
    c.info = media.summarize(media.ffprobe(path))
    c.segments = [(2, 10), (14, 10), (26, 10)]
    return c


@needs_tools
def test_burned_in_subtitles_are_detected_and_clean_video_is_not(clips):
    assert scanner._hardsubs(_ctx(clips["hard"]), "eng")["found"]
    assert scanner._hardsubs(_ctx(clips["clean"]), "eng") is None


@needs_tools
def test_truncation_is_confirmed_in_software(clips):
    assert scanner.confirm_truncation(_ctx(clips["clean"]), 2, True)["truncated"] is False
    assert scanner.confirm_truncation(_ctx(clips["cut"]), 2, True)["truncated"] is True
