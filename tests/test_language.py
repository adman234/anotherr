"""Language ID needs agreement across windows before it overrides a tag or raises an error."""
from anotherr import scanner


def _ctx(original, tracks):
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series", "original_language": original, "language_override": ""},
                    cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}}, profile="standard", pcfg={}, seed=1)
    c.info = {"audio": [{"default": t.get("default", False)} for t in tracks]}
    for i, (tag, wins) in enumerate((t["tag"], t["wins"]) for t in tracks):
        from anotherr import analysis
        agg = analysis.aggregate_language(wins)
        c.tracks[i] = {"a_index": i, "tag": tag, "default": tracks[i].get("default", False), "speech_s": 60,
                       "windows": [{"lang": l, "prob": p} for l, p in wins], "detected": agg["detected"],
                       "prob": agg["prob"], "agree": agg["agree"], "all_agree": agg["all_agree"]}
    c.expected = original
    scanner.check_language(c)
    return {i["code"]: i for i in c.issues}, c


def test_haikyu_two_windows_do_not_override_tag():
    codes, c = _ctx("jpn", [{"tag": "eng", "wins": [("eng", .95)] * 3, "default": True},
                            {"tag": "jpn", "wins": [("kor", .94), ("kor", .93)]}])
    assert "audio_mislabeled" not in codes and "original_audio_missing" not in codes
    assert c.tracks[1]["effective"] == "jpn" and c.tracks[1]["disputed"]


def test_haikyu_confirmed_override_warns_but_no_error():
    codes, c = _ctx("jpn", [{"tag": "eng", "wins": [("eng", .95)] * 3, "default": True},
                            {"tag": "jpn", "wins": [("kor", .94), ("kor", .95), ("kor", .93)]}])
    assert "audio_mislabeled" in codes
    assert "original_audio_missing" not in codes
    assert c.orig_track == 1


def test_disagreeing_windows_never_override():
    codes, _ = _ctx("jpn", [{"tag": "jpn", "wins": [("kor", .97), ("jpn", .9), ("kor", .96)]}])
    assert codes == {} or "audio_mislabeled" not in codes


def test_run_bts_mislabel_kept():
    codes, _ = _ctx("kor", [{"tag": "eng", "wins": [("kor", .99), ("kor", .94), ("kor", .97)], "default": True}])
    assert "audio_mislabeled" in codes and "original_audio_missing" not in codes


def test_english_only_release_still_errors():
    codes, _ = _ctx("jpn", [{"tag": "eng", "wins": [("eng", .98)] * 3, "default": True}])
    assert codes["original_audio_missing"]["severity"] == "error"
