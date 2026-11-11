"""The blue "dual" severity: single-language gaps, extra dual-audio flags, and the yellow default-track checks."""
import time

from anotherr import analysis, db, scanner, settings


def _ctx(spec, info_audio=None, duration=None, original="jpn", subs=None, request=""):
    """spec: list of (tag, wins, default). Runs check_language then check_dual_audio."""
    item = {"id": None, "kind": "movie", "original_language": original, "language_override": "",
            "audio_request": request, "audio_request_override": ""}
    cfg = settings.get()
    c = scanner.Ctx(file={"id": 1}, item=item, cfg=cfg, profile="standard", pcfg={}, seed=1)
    c.info = {"audio": info_audio or [{"default": d, "a_index": i} for i, (_, _, d) in enumerate(spec)],
              "subs": subs or []}
    if duration:
        c.info["duration"] = duration
    for i, (tag, wins, dflt) in enumerate(spec):
        agg = analysis.aggregate_language(wins)
        c.tracks[i] = {"a_index": i, "tag": tag, "default": dflt, "speech_s": 60, "level_db": -20,
                       "windows": [{"lang": lang, "prob": p} for lang, p in wins], "detected": agg["detected"],
                       "prob": agg["prob"], "agree": agg["agree"], "all_agree": agg["all_agree"]}
    c.expected = original
    scanner.check_language(c)
    scanner.check_dual_audio(c)
    return {i["code"]: i for i in c.issues}


JPN = [("jpn", .95)] * 3
ENG = [("eng", .95)] * 3


def test_proper_dual_audio_is_clean():
    codes = _ctx([("jpn", JPN, True), ("eng", ENG, False)])
    assert not codes


def test_dub_length_mismatch_is_blue():
    audio = [{"default": True, "a_index": 0, "end": 6000}, {"default": False, "a_index": 1, "end": 5400}]
    codes = _ctx([("jpn", JPN, True), ("eng", ENG, False)], info_audio=audio, duration=6000)
    assert codes["dual_track_length_mismatch"]["severity"] == "dual"
    audio[1]["end"] = 5990  # 10 s apart is normal
    assert "dual_track_length_mismatch" not in _ctx([("jpn", JPN, True), ("eng", ENG, False)],
                                                    info_audio=audio, duration=6000)


def test_unidentified_second_track_is_blue_not_missing():
    mush = [("ger", .3), ("fra", .3), ("ita", .3)]
    codes = _ctx([("jpn", JPN, True), ("", mush, False)])  # the probe normalises "und" to ""
    assert codes["dual_track_unlabeled"]["severity"] == "dual"
    assert codes["dual_track_unlabeled"]["details"]["tracks"] == [2]


def test_commentary_default_is_yellow():
    audio = [{"default": True, "a_index": 0, "commentary": True}, {"default": False, "a_index": 1},
             {"default": False, "a_index": 2}]
    codes = _ctx([("jpn", JPN, False), ("eng", ENG, False)], info_audio=audio)
    d = codes["default_audio_not_preferred"]
    assert d["severity"] == "warning" and d["details"]["commentary"] is True


def test_foreign_default_subtitle_is_yellow():
    subs = [{"s_index": 0, "lang": "ger", "default": True, "title": ""},
            {"s_index": 1, "lang": "eng", "default": False, "title": ""}]
    codes = _ctx([("jpn", JPN, True), ("eng", ENG, False)], subs=subs)
    assert codes["default_subs_foreign"]["severity"] == "warning"
    subs[0]["lang"] = "jpn"  # an original-language default is fine
    assert "default_subs_foreign" not in _ctx([("jpn", JPN, True), ("eng", ENG, False)], subs=subs)


def test_english_dub_default_needs_forced_subs():
    full = [{"s_index": 0, "lang": "eng", "default": False, "title": "Full"}]
    codes = _ctx([("jpn", JPN, False), ("eng", ENG, True)], subs=full)
    assert codes["dual_no_forced_subs"]["severity"] == "dual"
    forced = full + [{"s_index": 1, "lang": "eng", "default": False, "forced": True, "title": ""}]
    assert "dual_no_forced_subs" not in _ctx([("jpn", JPN, False), ("eng", ENG, True)], subs=forced)
    signs = full + [{"s_index": 1, "lang": "eng", "default": False, "title": "Signs & Songs"}]
    assert "dual_no_forced_subs" not in _ctx([("jpn", JPN, False), ("eng", ENG, True)], subs=signs)
    # original audio playing by default: no forced track needed
    assert "dual_no_forced_subs" not in _ctx([("jpn", JPN, True), ("eng", ENG, False)], subs=full)


def test_migration_moves_old_warnings_to_dual():
    now = time.time()
    iid = db.x("INSERT INTO items(source, arr_id, kind, title) VALUES ('radarr', 1, 'movie', 'X')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen, status) "
               "VALUES (?, 1, '/m/x.mkv', '[]', ?, 'warning')", (iid, now))
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) "
         "VALUES (?, 'english_audio_missing', 'warning', 'open', ?)", (fid, now))
    db.init()
    assert db.q1("SELECT severity FROM issues WHERE file_id=?", (fid,))["severity"] == "dual"
    assert db.q1("SELECT status FROM files WHERE id=?", (fid,))["status"] == "dual"


def test_yellow_beats_blue():
    assert db.worst(["dual", "warning"]) == "warning"
    assert db.worst(["info", "dual"]) == "dual"


def test_dual_actions_split():
    from anotherr import actions, audio_request

    assert actions.is_dual({"kind": audio_request.KIND, "file_id": 1})
    assert not actions.is_dual({"kind": "redownload", "file_id": 1, "codes": ["truncated"]})
    assert not actions.is_dual({"kind": "set_default_audio", "file_id": 1})
    assert actions.is_dual({"kind": "set_default_audio", "file_id": 1, "payload": {"dual": True}})
    assert not actions.is_dual({"kind": "retag_audio", "file_id": 1})


def test_english_default_audio_bulk_picks_only_plain_english_targets():
    from anotherr import actions

    iid = db.x("INSERT INTO items(source, arr_id, kind, title) VALUES ('radarr', 1, 'movie', 'X')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen, status) "
               "VALUES (?, 1, '/m/x.mkv', '[]', 0, 'ok')", (iid,))

    def add(payload):
        return db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                    "VALUES (?, 'set_default_audio', '', '[]', 'proposed', 0, ?, 0, 0)", (fid, db.j(payload)))

    eng = add({"dual": False, "files": [{"audio": {"to_lang": "eng"}}]})
    add({"dual": True, "files": [{"audio": {"to_lang": "eng"}}]})
    add({"dual": False, "files": [{"audio": {"to_lang": "jpn"}}]})
    add({"dual": False, "files": [{"audio": {"to_lang": "eng"}}, {"audio": {"to_lang": "jpn"}}]})
    assert actions.english_default_audio_ids() == [eng]
