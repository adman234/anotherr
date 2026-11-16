"""default_subs_foreign: every foreign default subtitle is cleared in one proposal, not one per scan."""
from anotherr import defaults


def test_all_foreign_default_subtitles_cleared_at_once():
    tracks = [{"a_index": 0, "effective": "eng", "confident": True, "all_agree": True, "windows": [1],
               "default": True, "title": "", "channels": 6, "tag": "eng"}]
    subs_all = [{"s_index": i, "lang": lang, "default": True, "forced": False}
                for i, lang in enumerate(["eng", "por", "spa", "fre"])]
    plan = defaults.plan_file(tracks, [], {"default_subs_foreign"}, "eng", "eng", subs_all=subs_all)
    assert sorted(o["index"] for o in plan["ops"] if o["default"] is False) == [1, 2, 3]
    assert plan["sub"]["clears"] == [1, 2, 3]
    assert defaults._signature(plan) == "subtitle clear Portuguese, Spanish, French"
