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


def test_gpu_gate_limits_slots_and_rests(monkeypatch):
    import threading
    import time
    from anotherr import gpu, settings
    monkeypatch.setattr(settings, "get", lambda: {"scan": {"gpu_slots": 1, "gpu_duty_percent": 50}})
    gpu._next_start = 0.0
    live, peak, starts = [0], [0], []

    def job():
        with gpu.use():
            starts.append(time.time())
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            time.sleep(0.1)
            live[0] -= 1

    ts = [threading.Thread(target=job) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    starts.sort()
    assert peak[0] == 1
    # each 0.1 s job is followed by ~0.1 s of rest at 50%
    assert all(b - a >= 0.18 for a, b in zip(starts, starts[1:])), starts
    gpu._next_start = 0.0
