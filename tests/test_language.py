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


def test_neither_original_nor_english_still_errors():
    codes, _ = _ctx("jpn", [{"tag": "ger", "wins": [("ger", .98)] * 3, "default": True}])
    assert codes["original_audio_missing"]["severity"] == "error"


def test_english_only_release_is_a_blue_dual_gap():
    tracks = [{"tag": "eng", "wins": [("eng", .98)] * 3, "default": True}]
    codes, _ = _ctx("jpn", tracks)  # unmapped = either: still a dual-audio gap, but not a requested one
    assert set(codes) == {"original_audio_missing"}
    assert codes["original_audio_missing"]["severity"] == "dual"
    assert codes["original_audio_missing"]["details"]["requested"] is False


def test_sibling_mislabel_downgrades_tag_only_error():
    import json
    import time

    from anotherr import db

    iid = db.x("INSERT INTO items(source, arr_id, title, kind, original_language) VALUES ('sonarr', 5, 'Run BTS!', 'series', 'kor')")
    sib = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, label, first_seen) VALUES (?, 1, '/a', 'S09E01', ?)",
               (iid, time.time()))
    db.x("INSERT INTO issues(file_id, code, severity, status, message, details) VALUES (?, 'audio_mislabeled', 'warning', "
         "'open', 'tagged English but sounds like Korean', ?)", (sib, json.dumps({"tag": "eng", "detected": "kor"})))
    me = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, label, first_seen) VALUES (?, 2, '/b', 'S09E03', ?)",
              (iid, time.time()))
    c = scanner.Ctx(file={"id": me}, item=db.q1("SELECT * FROM items WHERE id=?", (iid,)),
                    cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}}, profile="standard", pcfg={}, seed=1)
    c.info = {"audio": [{"default": True}], "duration": 60}
    c.tracks[0] = {"a_index": 0, "tag": "eng", "default": True, "speech_s": 0, "level_db": -20, "windows": [],
                   "detected": "", "prob": 0.0, "agree": 0, "all_agree": False}
    c.expected = "kor"
    scanner.check_language(c)
    codes = {i["code"]: i for i in c.issues}
    assert "original_audio_missing" not in codes
    assert codes["language_unconfirmed"]["details"]["sibling"]["label"] == "S09E01"


def test_untagged_subtitle_language_from_title():
    from anotherr import langs, media

    assert langs.from_title("English-- ==== 300mbunited.me | rpnet.biz ====") == "eng"
    assert langs.from_title("English.Hearing.impaired--site") == "eng"
    assert langs.from_title("The Voyage - YIFY") == ""  # ordinary words are not language codes
    info = media.summarize({"streams": [{"codec_type": "subtitle", "codec_name": "subrip", "index": 2,
                                         "tags": {"title": "English.Foreign.parts.only"}}]})
    assert info["subs"][0]["lang"] == "eng"


def _untagged_ctx(path, speech_s, windows, duration=2400):
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series", "original_language": "eng", "language_override": ""},
                    cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}}, profile="standard", pcfg={}, seed=1)
    c.path = path
    c.info = {"audio": [{"default": True}], "duration": duration}
    c.tracks[0] = {"a_index": 0, "tag": "", "default": True, "speech_s": speech_s, "window_s": 45, "level_db": -20,
                   "windows": [dict(at=a, lang=l, prob=p) for a, l, p in windows], "detected": "", "prob": 0.0, "extra_tried": True,
                   "agree": 0, "all_agree": False}
    c.expected = "eng"
    return c


def test_untagged_track_keeps_sampling_until_ten_seconds_of_speech(monkeypatch):
    import numpy as np
    from anotherr import media, stt
    calls = []
    monkeypatch.setattr(media, "extract_audio_multi", lambda path, idx, start, secs: calls.append(start) or {idx[0]: np.ones(10)})
    monkeypatch.setattr(stt, "speech_intervals", lambda a: [(0.0, 5.0)])  # 5 s of speech per sample
    monkeypatch.setattr(stt, "speech_audio", lambda a, iv, n: a)
    monkeypatch.setattr(stt.WHISPER, "detect_language", lambda a: ("en", 0.97, {}), raising=False)
    c = _untagged_ctx("/x/a.mkv", 0.0, [])
    scanner.check_language(c)
    t = c.tracks[0]
    assert len(calls) == 2 and t["speech_s"] == 10.0 and len(t["windows"]) == 2   # stops once 10 s and 2 windows are in
    assert any(i["code"] == "audio_untagged" for i in c.issues)


def test_untagged_sampling_gives_up_after_five_samples(monkeypatch):
    import numpy as np
    from anotherr import media, stt
    calls = []
    monkeypatch.setattr(media, "extract_audio_multi", lambda path, idx, start, secs: calls.append(start) or {idx[0]: np.zeros(10)})
    monkeypatch.setattr(stt, "speech_intervals", lambda a: [])
    c = _untagged_ctx("/x/a.mkv", 0.0, [])
    scanner.check_language(c)
    assert len(calls) == 5 and not any(i["code"] == "audio_untagged" for i in c.issues)


def test_avi_untagged_audio_is_not_flagged_since_it_cannot_be_retagged():
    c = _untagged_ctx("/x/a.avi", 60, [(100, "eng", .98), (900, "eng", .97), (1500, "eng", .99)])
    c.tracks[0].update(detected="eng", prob=.98, agree=3, all_agree=True)
    scanner.check_language(c)
    assert not any(i["code"] == "audio_untagged" for i in c.issues) and c.tracks[0]["effective"] == "eng"


def test_tagged_english_track_missed_by_scan_windows_gets_middle_samples(monkeypatch):
    # The Walking Dead case: the scan windows hit the intro and end credits, so the English track (tagged, not default)
    # had no detection and the Polish default looked unfixable. Middle-of-file samples confirm it, so the fix is offered.
    import numpy as np
    from anotherr import media, stt
    calls = []
    monkeypatch.setattr(media, "extract_audio_multi", lambda path, idx, start, secs: calls.append(start) or {idx[0]: np.ones(10)})
    monkeypatch.setattr(stt, "speech_intervals", lambda a: [(0.0, 20.0)])
    monkeypatch.setattr(stt, "speech_audio", lambda a, iv, n: a)
    monkeypatch.setattr(stt.WHISPER, "detect_language", lambda a: ("en", 0.97, {}), raising=False)
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series", "original_language": "eng", "language_override": ""},
                    cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}}, profile="standard", pcfg={}, seed=1)
    c.path = "/x/a.mkv"
    c.info = {"audio": [{"a_index": 0, "lang": "eng", "default": False}, {"a_index": 1, "lang": "pol", "default": True}],
              "duration": 2600}
    c.tracks[0] = {"a_index": 0, "tag": "eng", "default": False, "speech_s": 0, "window_s": 135, "level_db": -32,
                   "windows": [], "detected": "", "prob": 0.0, "agree": 0, "all_agree": False}
    c.tracks[1] = {"a_index": 1, "tag": "pol", "default": True, "speech_s": 90, "window_s": 135, "level_db": -28,
                   "windows": [{"at": a, "lang": "pol", "prob": .99} for a in (5, 170, 2536)], "detected": "pol",
                   "prob": .99, "agree": 3, "all_agree": True}
    c.expected = "eng"
    scanner.check_language(c)
    assert len(calls) == 4 and all(0.1 * 2600 < s < 0.8 * 2600 for s in calls)
    assert c.tracks[0]["detected"] == "eng" and c.tracks[0]["confident"]
    iss = next(i for i in c.issues if i["code"] == "default_audio_not_preferred")
    assert iss["details"]["fix_available"] is True and iss["details"]["target_track"] == 1


def test_default_track_is_not_resampled(monkeypatch):
    from anotherr import media
    calls = []
    monkeypatch.setattr(media, "extract_audio_multi", lambda *a: calls.append(a) or {})
    c = _untagged_ctx("/x/a.mkv", 0.0, [])
    c.tracks[0]["tag"] = "eng"
    scanner.check_language(c)
    assert calls == []
