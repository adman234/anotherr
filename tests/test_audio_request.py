"""Requested audio language (from the quality profile) and per-season accept exceptions."""
import json
import time

import pytest

from anotherr import actions, api, arr, audio_request, db, scanner, settings, sync

# ---------------------------------------------------------------- helpers

EPS1 = json.dumps([{"id": 1, "season": 1, "episode": 1}])
EPS2 = json.dumps([{"id": 2, "season": 2, "episode": 1}])


def _tracks(spec):
    """spec: list of (language, is_default); every track is tagged and detected as that language."""
    out = []
    for i, (lang, dflt) in enumerate(spec):
        out.append({"tag": lang, "wins": [(lang, .95)] * 3, "default": dflt})
    return out


def _check(item, spec, file=None):
    c = scanner.Ctx(file=file or {"id": 1}, item=item,
                    cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}}, profile="standard", pcfg={}, seed=1)
    tracks = _tracks(spec)
    c.info = {"audio": [{"default": t["default"], "a_index": i} for i, t in enumerate(tracks)]}
    from anotherr import analysis

    for i, t in enumerate(tracks):
        agg = analysis.aggregate_language(t["wins"])
        c.tracks[i] = {"a_index": i, "tag": t["tag"], "default": t["default"], "speech_s": 60, "level_db": -20,
                       "windows": [{"lang": lang, "prob": p} for lang, p in t["wins"]], "detected": agg["detected"],
                       "prob": agg["prob"], "agree": agg["agree"], "all_agree": agg["all_agree"]}
    c.expected = item["original_language"]
    scanner.check_language(c)
    return {i["code"]: i for i in c.issues}


def _item(request="", override="", **kw):
    return {"id": None, "kind": "series", "original_language": "jpn", "language_override": "",
            "audio_request": request, "audio_request_override": override, **kw}


O, E, N = ("jpn", True), ("eng", False), ("ger", False)  # original, English, neither (German)

# ---------------------------------------------------------------- the rules table (original language = Japanese)

CASES = {  # (request, tracks) -> expected audio-language issue codes
    ("", "O+E"): set(), ("", "O"): set(), ("", "E"): set(), ("", "neither"): {"original_audio_missing"},
    ("original", "O+E"): set(), ("original", "O"): set(), ("original", "E"): {"original_audio_missing"},
    ("original", "neither"): {"original_audio_missing"},
    ("english", "O+E"): set(), ("english", "O"): {"english_audio_missing"}, ("english", "E"): set(),
    ("english", "neither"): {"original_audio_missing"},
}
TRACKS = {"O+E": [O, ("eng", False)], "O": [O], "E": [("eng", True)], "neither": [("ger", True)]}
SEVERITY = {("original", "E"): "warning", ("english", "O"): "warning", ("", "neither"): "error",
            ("original", "neither"): "error", ("english", "neither"): "error"}


@pytest.mark.parametrize("request_,tracks", sorted(CASES))
def test_rules_table_cell(request_, tracks):
    codes = _check(_item(request_), TRACKS[tracks])
    got = {c for c in codes if c in ("original_audio_missing", "english_audio_missing")}
    assert got == CASES[(request_, tracks)]
    for c in got:
        assert codes[c]["severity"] == SEVERITY[(request_, tracks)]


def test_english_original_show_is_never_missing_english():
    codes = _check(_item("english", original_language="eng"), [("eng", True)])
    assert "english_audio_missing" not in codes and "original_audio_missing" not in codes


def test_unsure_track_does_not_raise_english_missing():
    # a second track with no usable language could be the English one
    item = _item("english")
    c = scanner.Ctx(file={"id": 1}, item=item, cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}},
                    profile="standard", pcfg={}, seed=1)
    c.info = {"audio": [{"default": True, "a_index": 0}, {"default": False, "a_index": 1}]}
    c.tracks[0] = {"a_index": 0, "tag": "jpn", "default": True, "speech_s": 60, "level_db": -20,
                   "windows": [{"lang": "jpn", "prob": .95}] * 3, "detected": "jpn", "prob": .95, "agree": 3, "all_agree": True}
    c.tracks[1] = {"a_index": 1, "tag": "", "default": False, "speech_s": 0, "level_db": -20, "windows": [],
                   "detected": "", "prob": 0.0, "agree": 0, "all_agree": False}
    c.expected = "jpn"
    scanner.check_language(c)
    assert "english_audio_missing" not in {i["code"] for i in c.issues}


def test_no_dub_is_retired():
    item = _item("english", is_anime=1)
    c = scanner.Ctx(file={"id": 1}, item=item, cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": True}},
                    profile="standard", pcfg={}, seed=1)
    c.info = {"audio": [{"default": True, "a_index": 0}]}
    c.tracks[0] = {"a_index": 0, "tag": "jpn", "default": True, "speech_s": 60, "level_db": -20,
                   "windows": [{"lang": "jpn", "prob": .95}] * 3, "detected": "jpn", "prob": .95, "agree": 3, "all_agree": True}
    c.expected = "jpn"
    scanner.check_language(c)
    assert "no_dub" not in {i["code"] for i in c.issues}


# ---------------------------------------------------------------- default track

def test_default_track_must_be_original_when_it_exists():
    for req in ("", "original", "english"):
        codes = _check(_item(req), [("eng", True), ("jpn", False)])
        assert "default_audio_not_preferred" in codes, req
        assert codes["default_audio_not_preferred"]["details"]["target_track"] == 2


def test_english_is_default_when_there_is_no_original():
    codes = _check(_item(""), [("ger", True), ("eng", False)])
    d = codes["default_audio_not_preferred"]
    assert d["details"]["fix_available"] and d["details"]["target_track"] == 2
    assert "English" in d["message"]
    assert "default_audio_not_preferred" not in _check(_item(""), [("eng", True), ("ger", False)])


def test_default_track_plan_falls_back_to_english():
    from anotherr import defaults

    tracks = [{"a_index": 0, "effective": "ger", "default": True, "confident": True, "all_agree": True, "windows": [1]},
              {"a_index": 1, "effective": "eng", "default": False, "confident": True, "all_agree": True, "windows": [1]}]
    assert not defaults.plan_file(tracks, [], {"default_audio_not_preferred"}, "jpn", "eng")["ops"]
    plan = defaults.plan_file(tracks, [], {"default_audio_not_preferred"}, "jpn", "eng", fallback="eng")
    assert plan["audio"]["to"] == 1 and plan["audio"]["to_lang"] == "eng"
    # a file that does have the original keeps preferring it
    tracks[0].update(effective="jpn", default=False)
    tracks[1]["default"] = True
    plan = defaults.plan_file(tracks, [], {"default_audio_not_preferred"}, "jpn", "eng", fallback="eng")
    assert plan["audio"]["to"] == 0 and plan["audio"]["to_lang"] == "jpn"


# ---------------------------------------------------------------- mapping and override precedence

def test_profile_mapping_is_case_insensitive_and_defaults_to_either():
    m = audio_request.map_profile
    assert m("Default TV - Original Language") == "original"
    assert m("anime - original language only") == "original"
    assert m("Default Movie - English Only") == "english"
    assert m("Default Anime - Dual Audio") == "english"
    assert m("OLD: Any Quality") == "" and m("something else") == "" and m(None) == ""


def test_mapping_is_configurable():
    settings.save({"languages": {"request_profiles": {"english": ["My Dub Profile"]}}})
    assert audio_request.map_profile("my dub profile") == "english"
    assert audio_request.map_profile("Default TV - Original Language") == "original"  # the other list is kept


def test_override_beats_profile():
    assert audio_request.effective({"audio_request": "english", "audio_request_override": ""}) == ("english", "profile")
    assert audio_request.effective({"audio_request": "english", "audio_request_override": "original"}) == ("original", "override")
    assert audio_request.effective({"audio_request": "english", "audio_request_override": "either"}) == ("", "override")
    assert audio_request.effective({"audio_request": "", "audio_request_override": ""}) == ("", "none")
    assert audio_request.describe({"audio_request": "original"})["audio_request_effective"] == "original"


def test_override_changes_the_verdict():
    assert "english_audio_missing" not in _check(_item("original"), TRACKS["O"])
    assert "english_audio_missing" in _check(_item("original", "english"), TRACKS["O"])
    assert "english_audio_missing" not in _check(_item("english", "either"), TRACKS["O"])


# ---------------------------------------------------------------- sync stores the profile + request

def _fake_arr(monkeypatch, profiles):
    settings.save({"sonarr": {"url": "http://s", "api_key": "k"}, "radarr": {"url": "http://r", "api_key": "k"}})
    series = [{"id": 1, "title": "Show", "runtime": 11, "statistics": {"episodeFileCount": 0}, "path": "/tv/S",
               "qualityProfileId": 2},
              {"id": 2, "title": "Other", "runtime": 11, "statistics": {"episodeFileCount": 0}, "path": "/tv/O",
               "qualityProfileId": 9}]
    movies = [{"id": 5, "title": "Film", "year": 2020, "runtime": 100, "path": "/movies/F", "qualityProfileId": 3,
               "originalLanguage": {"name": "Japanese"}}]
    monkeypatch.setattr(arr.Sonarr, "series", lambda self, i=None: series[0] if i else series)
    monkeypatch.setattr(arr.Sonarr, "quality_profiles", lambda self: profiles["sonarr"])
    monkeypatch.setattr(arr.Radarr, "movies", lambda self, i=None: movies[0] if i else movies)
    monkeypatch.setattr(arr.Radarr, "quality_profiles", lambda self: profiles["radarr"])


def test_sync_stores_profile_name_and_request(monkeypatch):
    profiles = {"sonarr": {2: "Default TV - English Only", 4: "OLD: Any Quality"},
                "radarr": {3: "Default Movie - Original Language"}}
    _fake_arr(monkeypatch, profiles)
    res = sync.full_sync()
    assert res["ok"], res
    show = db.q1("SELECT * FROM items WHERE source='sonarr' AND arr_id=1")
    assert (show["quality_profile"], show["audio_request"]) == ("Default TV - English Only", "english")
    film = db.q1("SELECT * FROM items WHERE source='radarr' AND arr_id=5")
    assert (film["quality_profile"], film["audio_request"]) == ("Default Movie - Original Language", "original")
    unknown = db.q1("SELECT * FROM items WHERE source='sonarr' AND arr_id=2")  # profile id 9 is not in the list
    assert (unknown["quality_profile"], unknown["audio_request"]) == ("", "")
    # the override survives a resync, and a profile change is picked up
    db.x("UPDATE items SET audio_request_override='original' WHERE id=?", (show["id"],))
    profiles["sonarr"][2] = "OLD: Any Quality"
    sync.full_sync()
    show = db.q1("SELECT * FROM items WHERE source='sonarr' AND arr_id=1")
    assert show["audio_request"] == "" and show["audio_request_override"] == "original"
    assert audio_request.effective(show) == ("original", "override")


def test_sync_keeps_stored_request_when_profiles_cannot_be_read(monkeypatch):
    profiles = {"sonarr": {2: "Default TV - English Only"}, "radarr": {3: "Default Movie - English Only"}}
    _fake_arr(monkeypatch, profiles)
    sync.full_sync()

    def boom(self):
        raise arr.ServiceError("down")

    monkeypatch.setattr(arr.Sonarr, "quality_profiles", boom)
    sync.full_sync()
    show = db.q1("SELECT * FROM items WHERE source='sonarr' AND arr_id=1")
    assert show["audio_request"] == "english"


def test_item_patch_validates_override():
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, quality_profile, audio_request) "
               "VALUES ('sonarr', 1, 'S', 'series', 'Default TV - English Only', 'english')")
    it = api.item_update(iid, {"audio_request_override": "Original"})
    assert it["audio_request_override"] == "original"
    assert (it["audio_request_effective"], it["audio_request_source"]) == ("original", "override")
    it = api.item_update(iid, {"audio_request_override": ""})
    assert (it["audio_request_effective"], it["audio_request_source"]) == ("english", "profile")
    with pytest.raises(api.HTTPException):
        api.item_update(iid, {"audio_request_override": "klingon"})


# ---------------------------------------------------------------- exceptions: scanner side

def _show(request="english"):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, original_language, audio_request) "
               "VALUES ('sonarr', 1, 'Show', 'series', 'jpn', ?)", (request,))
    files = {}
    for season, eps in ((1, EPS1), (2, EPS2)):
        for n in (1, 2):
            files[(season, n)] = db.x(
                "INSERT INTO files(item_id, arr_file_id, arr_path, local_path, episodes, label, first_seen, status) "
                "VALUES (?,?,?,?,?,?,?, 'warning')", (iid, season * 10 + n, f"/tv/S/{season}{n}.mkv", f"/tv/S/{season}{n}.mkv",
                                                       eps, f"S0{season}E0{n}", time.time()))
    return iid, files


def _scan(fid, spec, store=True):
    """Run the language check for a stored file and persist its issues the way a scan does."""
    f = db.q1("SELECT * FROM files WHERE id=?", (fid,))
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    codes = _check(item, spec, file=f)
    if store:
        db.x("UPDATE issues SET status='resolved' WHERE file_id=? AND status='open'", (fid,))
        for c in codes.values():
            db.x("INSERT INTO issues(file_id, category, code, severity, message, details, status, created_at, updated_at) "
                 "VALUES (?, 'language', ?, ?, ?, '{}', 'open', ?, ?)", (fid, c["code"], c["severity"], c["message"],
                                                                         time.time(), time.time()))
        actions.reconcile(fid)
        actions.propose_for_file(fid)
        audio_request.refresh_item(f["item_id"])
    return codes


def _open(fid):
    return {r["code"] for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (fid,))}


def _proposed(kind=None):
    return db.q("SELECT * FROM actions WHERE status='proposed'" + (" AND kind=?" if kind else ""), (kind,) if kind else ())


def _add_exception(iid, season, allow):
    db.x("INSERT INTO audio_exceptions(item_id, season, allow, created_at) VALUES (?,?,?,?)", (iid, season, allow, time.time()))


def test_exception_suppresses_only_the_matching_warning_in_its_season():
    iid, files = _show("english")
    _add_exception(iid, 1, "original_only")
    assert "english_audio_missing" not in _scan(files[(1, 1)], TRACKS["O"], store=False)
    assert "english_audio_missing" in _scan(files[(2, 1)], TRACKS["O"], store=False)  # another season
    # the other direction needs the other kind of exception
    db.x("UPDATE items SET audio_request='original'")
    assert "original_audio_missing" in _scan(files[(1, 1)], TRACKS["E"], store=False)
    _add_exception(iid, 1, "english_only")
    assert "original_audio_missing" not in _scan(files[(1, 1)], TRACKS["E"], store=False)


def test_exception_never_suppresses_the_neither_error_or_default_track_issues():
    iid, files = _show("english")
    for allow in ("original_only", "english_only"):
        _add_exception(iid, 1, allow)
    codes = _scan(files[(1, 1)], [("ger", True)], store=False)
    assert codes["original_audio_missing"]["severity"] == "error"
    codes = _scan(files[(1, 1)], [("eng", True), ("jpn", False)], store=False)  # original exists but is not the default
    assert "default_audio_not_preferred" in codes
    codes = _scan(files[(1, 1)], [("ger", True), ("eng", False)], store=False)  # no original: English should be default
    assert "default_audio_not_preferred" in codes


def test_movie_exception_has_no_season():
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, original_language, audio_request) "
               "VALUES ('radarr', 3, 'Film', 'movie', 'jpn', 'english')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen, status) VALUES (?, 1, '/m/f.mkv', '[]', ?, "
               "'warning')", (iid, time.time()))
    assert "english_audio_missing" in _scan(fid, TRACKS["O"], store=False)
    _add_exception(iid, None, "original_only")
    assert "english_audio_missing" not in _scan(fid, TRACKS["O"], store=False)


# ---------------------------------------------------------------- exceptions: lifecycle

def test_accept_resolves_the_season_and_withdraws_redownloads_then_later_imports_stay_quiet():
    iid, files = _show("english")
    for key in ((1, 1), (1, 2), (2, 1)):
        assert "english_audio_missing" in _scan(files[key], TRACKS["O"])
    redl = _proposed("redownload")
    assert len(redl) == 3  # english_audio_missing leads to a redownload proposal
    acc = _proposed("accept_season_audio")
    assert sorted(db.unj(a["payload"])["season"] for a in acc) == [1, 2]  # one per season
    season1 = next(a for a in acc if db.unj(a["payload"])["season"] == 1)
    assert {e["file_id"] for e in db.unj(season1["payload"])["files"]} == {files[(1, 1)], files[(1, 2)]}

    res = actions.execute(season1["id"])
    assert res["ok"], res
    assert db.q1("SELECT status FROM actions WHERE id=?", (season1["id"],))["status"] == "done"
    ex = db.q("SELECT * FROM audio_exceptions")
    assert [(e["item_id"], e["season"], e["allow"]) for e in ex] == [(iid, 1, "original_only")]
    assert _open(files[(1, 1)]) == set() and _open(files[(1, 2)]) == set()
    assert db.q1("SELECT status, open_issues FROM files WHERE id=?", (files[(1, 1)],)) == {"status": "ok", "open_issues": 0}
    assert _open(files[(2, 1)]) == {"english_audio_missing"}  # season 2 is untouched
    withdrawn = {r["file_id"] for r in db.q("SELECT file_id FROM actions WHERE kind='redownload' AND status='withdrawn'")}
    assert withdrawn == {files[(1, 1)], files[(1, 2)]}
    assert {r["file_id"] for r in _proposed("redownload")} == {files[(2, 1)]}

    # an episode imported into season 1 later gets no warning, season 2 still does
    late = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, label, first_seen, status) "
                "VALUES (?, 19, '/tv/S/late.mkv', ?, 'S01E09', ?, 'unscanned')", (iid, EPS1, time.time()))
    assert _scan(late, TRACKS["O"]) == {}
    assert _open(late) == set() and not [a for a in _proposed() if a["file_id"] == late]


def test_remove_exception_requeues_the_season_and_the_warning_returns():
    iid, files = _show("english")
    _scan(files[(1, 1)], TRACKS["O"])
    aid = _proposed("accept_season_audio")[0]["id"]
    actions.execute(aid)
    _scan(files[(1, 2)], TRACKS["O"])
    exc = db.q1("SELECT id FROM audio_exceptions")
    out = audio_request.remove(exc["id"])
    assert out["queued"] == 2
    assert {r["file_id"] for r in db.q("SELECT file_id FROM queue")} == {files[(1, 1)], files[(1, 2)]}
    assert {r["profile"] for r in db.q("SELECT profile FROM queue")} == {"standard"}
    assert db.q("SELECT * FROM audio_exceptions") == []
    assert "english_audio_missing" in _scan(files[(1, 1)], TRACKS["O"])  # the rescan brings it back
    assert audio_request.remove(exc["id"]) is None


def test_approving_the_redownload_withdraws_the_accept_proposal(monkeypatch):
    iid, files = _show("english")
    _scan(files[(1, 1)], TRACKS["O"])
    _scan(files[(2, 1)], TRACKS["O"])
    monkeypatch.setattr(actions, "_redownload", lambda f, item: "blocklisted; search triggered")
    redl = next(a for a in _proposed("redownload") if a["file_id"] == files[(1, 1)])
    assert actions.execute(redl["id"])["ok"]
    left = _proposed("accept_season_audio")
    assert [db.unj(a["payload"])["season"] for a in left] == [2]  # season 1's accept proposal is gone
    assert db.q1("SELECT COUNT(*) n FROM audio_exceptions")["n"] == 0


def test_accept_proposal_disappears_when_the_gap_does():
    iid, files = _show("english")
    _scan(files[(1, 1)], TRACKS["O"])
    assert len(_proposed("accept_season_audio")) == 1
    _scan(files[(1, 1)], TRACKS["O+E"])  # an English track arrived
    assert _proposed("accept_season_audio") == []
    assert db.q1("SELECT status FROM actions WHERE kind='accept_season_audio'")["status"] == "withdrawn"


def test_accept_proposal_not_made_for_the_neither_error():
    iid, files = _show("english")
    _scan(files[(1, 1)], [("ger", True)])
    assert _proposed("accept_season_audio") == []
    assert [a["kind"] for a in _proposed()] == ["redownload"]


def test_accept_is_never_automatic():
    settings.save({"automation": {"enabled": True, "auto_redownload": True}})
    iid, files = _show("english")
    _scan(files[(1, 1)], TRACKS["O"])
    a = _proposed("accept_season_audio")[0]
    assert not actions._eligible_auto(a, settings.get())


def test_manual_proposal_and_api_endpoints():
    iid, files = _show("english")
    _scan(files[(1, 1)], TRACKS["O"])
    aid = _proposed("accept_season_audio")[0]["id"]
    assert api.action_create({"kind": "accept_season_audio", "file_id": files[(1, 1)]})["action_id"] == aid
    with pytest.raises(api.HTTPException):  # a file without an open warning has nothing to accept
        api.action_create({"kind": "accept_season_audio", "file_id": files[(2, 1)]})
    assert api.action_approve(aid)["ok"]
    rows = api.item_audio_exceptions(iid)
    assert [(r["season"], r["allow"]) for r in rows] == [(1, "original_only")]
    assert api.item(iid)["audio_exceptions"] == rows
    detail = api.file_detail(files[(1, 1)])
    assert detail["item"]["audio_request_effective"] == "english" and len(detail["audio_exceptions"]) == 1
    assert api.file_detail(files[(2, 1)])["audio_exceptions"] == []
    assert api.audio_exception_delete(rows[0]["id"])["queued"] == 2
    with pytest.raises(api.HTTPException):
        api.audio_exception_delete(rows[0]["id"])
    with pytest.raises(api.HTTPException):
        api.item_audio_exceptions(99999)
