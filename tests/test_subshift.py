"""Shifting offset subtitles back into sync."""
import os

import pytest

from anotherr import subshift

SRT = "1\r\n00:00:01,000 --> 00:00:02,500\r\nhello\r\n\r\n2\r\n01:00:00,100 --> 01:00:01,000\r\nbye\r\n"
ASS = ("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hi, there\n")


def test_srt_shift_earlier_and_clamps():
    out = subshift.shift_srt_like(SRT.replace("\r\n", "\n"), -0.7)
    assert "00:00:00,300 --> 00:00:01,800" in out
    assert "00:59:59,400 --> 01:00:00,300" in out
    assert subshift.shift_srt_like("00:00:00,200 --> 00:00:01,000\n", -0.7).startswith("00:00:00,000 --> 00:00:00,300")


def test_vtt_keeps_cue_settings():
    out = subshift.shift_srt_like("00:00:01.000 --> 00:00:02.000 align:start\n", 1.5)
    assert out.startswith("00:00:02.500 --> 00:00:03.500 align:start")


def test_ass_shift_keeps_text_commas():
    out = subshift.shift_ass(ASS, -0.5)
    assert "Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,Hi, there" in out


def test_sidecar_roundtrip_with_backup(tmp_path):
    p = tmp_path / "ep.en.srt"
    p.write_bytes(SRT.encode())
    msg = subshift._shift_sidecar(str(p), 0.7)  # subtitles late by 0.7s -> cues move earlier
    assert "-0.70s" in msg
    text = p.read_bytes().decode()
    assert "00:00:00,300 --> 00:00:01,800" in text and "\r\n" in text
    assert (tmp_path / ("ep.en.srt" + subshift.BACKUP_SUFFIX)).read_bytes() == SRT.encode()
    subshift._shift_sidecar(str(p), 0.1)  # second shift must not clobber the first backup
    assert (tmp_path / ("ep.en.srt" + subshift.BACKUP_SUFFIX)).read_bytes() == SRT.encode()


def test_sidecar_rejects_non_text(tmp_path):
    p = tmp_path / "ep.sup"
    p.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        subshift._shift_sidecar(str(p), 1.0)


def test_remux_args_delay_sign():
    args = subshift.remux_args("/a.mkv", "/t.mkv", {3: -700})
    assert args[args.index("--sync") + 1] == "3:-700" and args[-1] == "/a.mkv"
    assert os.path.basename(args[0]).startswith("mkvmerge")
