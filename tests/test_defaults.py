"""Default-track proposals: the decision table from the spec."""
import json
import time

from anotherr import db, defaults, settings


def A(i, lang, default=False, ch=2, confident=True, agree=True, analysed=True, title="", commentary=False):
    return {"a_index": i, "tag": lang, "title": title, "channels": ch, "default": default, "commentary": commentary,
            "analysed": analysed, "effective": lang, "confident": confident and analysed, "all_agree": agree and analysed,
            "windows": [{"lang": lang, "prob": 0.97}] * 3 if analysed else []}


def S(i, lang="eng", kind="full", default=False, forced=False, text=True, sdh=False, external=False, title=""):
    return {"s_index": None if external else i, "lang": lang, "kind": kind, "default": default, "forced": forced,
            "text": text, "hearing_impaired": sdh, "external": external, "title": title, "codec": "subrip"}


BOTH = {"default_audio_not_preferred", "default_subs_signs"}


def test_frog_and_toad_german_default_english_track0():
    p = defaults.plan_file([A(0, "eng"), A(1, "ger", default=True)], [], {"default_audio_not_preferred"}, "eng", "eng")
    assert p["ops"] == [{"type": "a", "index": 0, "default": True, "expect_lang": "eng"}]
    assert p["audio"]["from"] == 1 and p["sub"] is None


def test_anime_needs_both_is_one_plan():
    tracks = [A(0, "eng", default=True, ch=6), A(1, "jpn", ch=2)]
    subs = [S(0, kind="signs", default=True, forced=True), S(1)]
    p = defaults.plan_file(tracks, subs, BOTH, "jpn", "eng")
    assert [o["type"] for o in p["ops"]] == ["a", "s"]
    assert p["ops"][0]["index"] == 1 and p["ops"][1]["index"] == 1


def test_audio_most_channels_then_lowest_index_and_skips_commentary():
    tracks = [A(0, "ger", default=True), A(1, "jpn", ch=2), A(2, "jpn", ch=6, title="Commentary"), A(3, "jpn", ch=6)]
    p = defaults.plan_file(tracks, [], {"default_audio_not_preferred"}, "jpn", "eng")
    assert p["ops"][0]["index"] == 3


def test_never_promote_on_tag_alone():
    tracks = [A(0, "eng", default=True), A(1, "jpn", analysed=False)]
    p = defaults.plan_file(tracks, [], {"default_audio_not_preferred"}, "jpn", "eng")
    assert not p["ops"] and "no confidently detected" in p["notes"][0]


def test_disputed_track_not_promoted():
    p = defaults.plan_file([A(0, "eng", default=True), A(1, "spa", agree=False)], [], {"default_audio_not_preferred"},
                           "spa", "eng")
    assert not p["ops"]


def test_subtitle_only_text_over_image_non_sdh_over_sdh():
    tracks = [A(0, "jpn", default=True)]
    subs = [S(0, kind="signs", default=True), S(1, text=False), S(2, sdh=True), S(3), S(4, lang="spa")]
    p = defaults.plan_file(tracks, subs, {"default_subs_signs"}, "jpn", "eng")
    assert p["ops"] == [{"type": "s", "index": 3, "default": True}] and p["audio"] is None
    assert p["sub"]["from"] == 0 and not p["sub"]["image"]


def test_image_only_target_is_flagged():
    p = defaults.plan_file([A(0, "jpn", default=True)], [S(0, kind="signs", default=True), S(1, text=False)],
                           {"default_subs_signs"}, "jpn", "eng")
    assert p["sub"]["image"] and p["ops"][0]["index"] == 1
    assert "image-based" in defaults._reason({"title": "MHA"}, 1, [{"plan": p}])


def test_english_audio_keeps_forced_signs():
    p = defaults.plan_file([A(0, "eng", default=True)], [S(0, kind="signs", default=True, forced=True), S(1)],
                           {"default_subs_signs"}, "eng", "eng")
    assert not p["ops"] and "subtitle language" in p["notes"][0]


def test_audio_fix_to_english_means_no_sub_change():
    p = defaults.plan_file([A(0, "ger", default=True), A(1, "eng")], [S(0, kind="signs", default=True), S(1)],
                           BOTH, "eng", "eng")
    assert [o["type"] for o in p["ops"]] == ["a"]


def test_no_flag_means_first_track_is_default():
    p = defaults.plan_file([A(0, "eng"), A(1, "jpn")], [], {"default_audio_not_preferred"}, "jpn", "eng")
    assert p["audio"]["from"] == 0 and p["ops"][0]["index"] == 1


def test_external_full_sub_note_only():
    p = defaults.plan_file([A(0, "jpn", default=True)], [S(0, kind="signs", default=True), S(1, external=True)],
                           {"default_subs_signs"}, "jpn", "eng")
    assert not p["ops"] and "external file" in p["notes"][0]


def test_already_default_full_sub_is_noop():
    p = defaults.plan_file([A(0, "jpn", default=True)], [S(0, kind="signs"), S(1, default=True)],
                           {"default_subs_signs"}, "jpn", "eng")
    assert not p["ops"]


# ---------------------------------------------------------------- grouping

def _file(iid, label, season, tracks, subs, codes):
    ev = {"scan_started": time.time(), "container": "matroska", "tracks": tracks, "subs": subs}
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, label, episodes, first_seen, audio_evidence) "
               "VALUES (?,?,?,?,?,?,?)", (iid, abs(hash(label)) % 10**6, f"/nope/{label}.mkv", label,
                                          json.dumps([{"id": 1, "season": season}]), time.time(), json.dumps(ev)))
    for c in codes:
        db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?,?, 'warning', 'open', ?)",
             (fid, c, time.time()))
    return fid


def test_grouping_per_season_and_change_then_withdraw():
    settings.save({"languages": {"subtitle_language": "eng"}})
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, original_language) VALUES ('sonarr', 5, 'Haikyu', 'series', 'jpn')")
    both = ([A(0, "eng", default=True), A(1, "jpn")], [S(0, kind="signs", default=True), S(1)])
    for ep in ("S01E01", "S01E02"):
        _file(iid, ep, 1, *both, BOTH)
    _file(iid, "S02E01", 2, *both, BOTH)
    subonly = _file(iid, "S01E03", 1, [A(0, "jpn", default=True)], [S(0, kind="signs", default=True), S(1)],
                    {"default_subs_signs"})
    _file(iid, "S01E04", 1, [A(0, "jpn", default=True)], [], {"wrong_content", "default_subs_signs"})  # blocked
    assert defaults.refresh_item(iid) == {"created": 3, "updated": 0, "withdrawn": 0}
    rows = db.q("SELECT * FROM actions ORDER BY id")
    kinds = sorted((r["kind"], len(json.loads(r["payload"])["files"])) for r in rows)
    assert kinds == [("set_default_audio", 1), ("set_default_audio", 2), ("set_default_subtitle", 1)]
    s1 = next(r for r in rows if r["kind"] == "set_default_audio" and len(json.loads(r["payload"])["files"]) == 2)
    assert "Haikyu Season 1" in s1["reason"] and "subtitle" in s1["reason"]
    db.x("UPDATE issues SET status='resolved' WHERE file_id=?", (subonly,))
    assert defaults.refresh_item(iid) == {"created": 0, "updated": 2, "withdrawn": 1}


# ---------------------------------------------------------------- manual proposals, whisper, resub re-arm

def _ep_file(tag="jpn", episodes=1):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 9 + (SELECT COUNT(*) FROM items), 'Show', 'series')")
    ev = {"tracks": [A(0, tag, default=True)], "subs": []}
    eps = [{"id": 100 + i, "season": 1, "episode": i + 1} for i in range(episodes)]
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, label, episodes, first_seen, audio_evidence) "
               "VALUES (?, 1, '/x.mkv', 'S01E01', ?, ?, ?)", (iid, json.dumps(eps), time.time(), json.dumps(ev)))
    return iid, fid


def test_whisper_blockers():
    from anotherr import actions

    settings.save({"languages": {"subtitle_language": "eng"}})
    assert actions.whisper_blocker(_ep_file("jpn")[1]) is None
    assert "already" in actions.whisper_blocker(_ep_file("eng")[1])
    assert "language tag" in actions.whisper_blocker(_ep_file("und")[1])
    assert "one episode" in actions.whisper_blocker(_ep_file("jpn", episodes=2)[1])


def test_whisper_runs_in_background_and_picks_whisper_provider(monkeypatch):
    from anotherr import actions, arr

    settings.save({"languages": {"subtitle_language": "eng"}})
    _, fid = _ep_file("jpn")
    calls = {}

    class FakeBazarr:
        def provider_results(self, episode_id=None, radarr_id=None):
            calls["search"] = episode_id
            return [{"provider": "opensubtitles", "subtitle": "x", "language": "en"},
                    {"provider": "whisperai", "subtitle": "w", "language": "en"}]

        def provider_download(self, result, **kw):
            calls["download"] = (result["provider"], kw)

    monkeypatch.setattr(arr, "Bazarr", FakeBazarr)
    aid = actions.propose(fid, "whisper_subtitle", "manual")
    assert actions.execute(aid)["ok"]
    for _ in range(50):
        if db.scalar("SELECT status FROM actions WHERE id=?", (aid,)) != "running":
            break
        time.sleep(0.05)
    assert db.scalar("SELECT status FROM actions WHERE id=?", (aid,)) == "done"
    assert calls["search"] == 100 and calls["download"] == ("whisperai", {"series_id": 9, "episode_id": 100})


def test_resub_rearms_after_retag():
    from anotherr import actions

    iid, fid = _ep_file("jpn")
    old = time.time() - 3600
    db.x("INSERT INTO actions(file_id, kind, status, codes, created_at, updated_at) VALUES (?, 'resub', 'done', '[\"subs_missing\"]', ?, ?)",
         (fid, old, old))
    db.x("INSERT INTO issues(file_id, code, severity, status, created_at) VALUES (?, 'subs_missing', 'warning', 'open', ?)",
         (fid, time.time()))
    assert actions.resub_failed_before(iid) and not actions.propose_for_file(fid)
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'retag_audio', 'done', ?, ?, ?)",
         (fid, json.dumps({"files": [{"file_id": fid, "status": "edited"}]}), time.time(), time.time()))
    assert not actions.resub_failed_before(iid)
    assert actions.propose_for_file(fid)


def test_manual_resub_for_issue_via_api():
    from fastapi.testclient import TestClient

    from anotherr import api

    _, fid = _ep_file("jpn")
    iss = db.x("INSERT INTO issues(file_id, code, severity, message, status, created_at) VALUES (?, 'subs_out_of_sync', 'warning', 'off by 2s', 'open', ?)",
               (fid, time.time()))
    c = TestClient(api.app)
    r = c.post("/api/actions", json={"kind": "resub", "issue_id": iss}).json()
    a = db.q1("SELECT * FROM actions WHERE id=?", (r["action_id"],))
    assert a["file_id"] == fid and json.loads(a["codes"]) == ["subs_out_of_sync"] and "off by 2s" in a["reason"]
    assert c.post("/api/actions", json={"kind": "resub", "issue_id": iss}).json()["action_id"] is None
    detail = c.get(f"/api/files/{fid}").json()
    assert detail["audio_tracks"][0]["tag"] == "jpn" and "facts" in detail


def test_group_approve_runs_in_background_and_skips_other_kinds(monkeypatch):
    from fastapi.testclient import TestClient

    from anotherr import actions, api

    ran = []
    monkeypatch.setattr(actions, "_resub", lambda f, item: ran.append(f["id"]) or "searched")
    files = [_ep_file("kor")[1] for _ in range(3)]
    ids = [actions.propose(f, "resub", "Issues: subs_missing") for f in files]
    whisper = actions.propose(files[0], "whisper_subtitle", "manual")
    c = TestClient(api.app)
    listed = c.get("/api/actions").json()
    assert {a["season"] for a in listed} == {1}
    r = c.post("/api/actions/bulk", json={"op": "approve", "ids": ids + [whisper]}).json()
    assert r == {"ok": True, "queued": 3, "skipped": 1}
    for _ in range(50):
        if db.scalar("SELECT COUNT(*) FROM actions WHERE status='done'") == 3:
            break
        time.sleep(0.05)
    assert sorted(ran) == sorted(files)
    assert db.scalar("SELECT status FROM actions WHERE id=?", (whisper,)) == "proposed"
    assert c.post("/api/actions/bulk", json={"op": "approve", "ids": ids}).json()["queued"] == 0  # no double run
    assert c.post("/api/actions/bulk", json={"op": "reject", "ids": [whisper]}).json()["rejected"] == 1
