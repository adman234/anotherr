import datetime as dt
import random

from anotherr import analysis, langs, media
from anotherr.schedule_util import active_window, in_windows


def test_lang_norm():
    assert langs.norm("Japanese") == "jpn"
    assert langs.norm("ja") == "jpn"
    assert langs.norm("ger") == langs.norm("deu") == "ger"
    assert langs.norm("und") == ""
    assert langs.norm("pt-BR") == "por"
    assert langs.iso1("eng") == "en"
    assert langs.same("fre", "fra")


def _speech(offset=0.0, n=40, seed=1):
    rng = random.Random(seed)
    t, out = 0.0, []
    for _ in range(n):
        t += rng.uniform(0.5, 2.5)
        d = rng.uniform(0.8, 3.0)
        out.append((t + offset, t + d + offset))
        t += d
    return out


def test_sync_in_sync():
    sp = _speech()
    r = analysis.sync_offset(sp, sp, 0, 120)
    assert r and abs(r["offset"]) < 0.2 and r["score"] > 0.8


def test_sync_detects_offset():
    sp = _speech()
    cues = [(a + 2.5, b + 2.5) for a, b in sp]
    rs = [analysis.sync_offset(sp, cues, 0, 120) for _ in range(3)]
    agg = analysis.aggregate_sync(rs)
    assert agg["verdict"] == "offset"
    assert abs(agg["offset"] - 2.5) < 0.2


def test_sync_detects_mismatch():
    sp = _speech(seed=1)
    other = _speech(seed=99)
    rs = [analysis.sync_offset(sp, other, 0, 120)]
    agg = analysis.aggregate_sync(rs)
    assert agg["verdict"] in ("mismatch", "ok")  # random cues can partially overlap
    assert agg["score"] < 0.6


def test_dialogue_cues_filters_signs():
    ass = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: Signs,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello there
Dialogue: 0,0:00:04.00,0:00:06.00,Signs,,0,0,0,,STORE
Dialogue: 0,0:00:07.00,0:00:09.00,Default,,0,0,0,,{\\pos(10,10)}Positioned sign
Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,General Kenobi
"""
    cues, total = analysis.dialogue_cues(ass)
    assert total == 4
    assert [c.text for c in cues] == ["Hello there", "General Kenobi"]


def test_classify():
    assert analysis.classify_track(300, 1440, "English", False, 3) == "full"
    assert analysis.classify_track(20, 1440, "English", False, 3) == "signs"
    assert analysis.classify_track(300, 1440, "Signs & Songs", False, 3) == "signs"
    assert analysis.classify_track(0, 1440, "", False, 3) == "empty"


def test_text_overlap_dubtitles():
    t = "We have to get to the castle before the soldiers find the princess tonight, otherwise everything is lost forever"
    o = analysis.text_overlap(t, t)
    assert o["recall"] == 1.0
    o2 = analysis.text_overlap(t, "The weather tomorrow brings rain across northern valleys with strong winds and cold temperatures expected")
    assert o2["recall"] < 0.2


def test_segments():
    rng = random.Random(1)
    segs = media.pick_segments(1400, 5, 120, rng)
    assert len(segs) == 5
    assert segs[0][0] < 20 and segs[-1][0] + segs[-1][1] >= 1390
    assert media.pick_segments(30, 5, 120, rng) == [(0.0, 30)]


def test_windows():
    w = [{"days": [0], "start": "23:00", "end": "05:00"}]  # Monday night
    mon_late = dt.datetime(2026, 11, 2, 23, 30)  # a Monday
    tue_early = dt.datetime(2026, 11, 3, 4, 0)
    tue_late = dt.datetime(2026, 11, 3, 23, 30)
    assert in_windows(w, mon_late)
    assert in_windows(w, tue_early)
    assert not in_windows(w, tue_late)
    assert active_window(w, mon_late)[1] == active_window(w, tue_early)[1]


def test_nearest_break():
    assert analysis.nearest_break(660, [(400, 401), (655, 656), (700, 701)], [(655, 657)]) == 655.5
    assert analysis.nearest_break(660, [], []) is None


def test_sync_periodic_prefers_zero():
    cues = [(1 + 3 * k, 3 + 3 * k) for k in range(40)]
    r = analysis.sync_offset(cues, cues, 0, 100)
    assert r["offset"] == 0
