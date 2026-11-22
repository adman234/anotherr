import time

from anotherr import actions, arr, db, trackedit


def _h(provider="subsource", subs_id="652367", code="en", blacklisted=False, path="/tv/a.en.srt", forced=False):
    return {"provider": provider, "subs_id": subs_id, "subtitles_path": path, "blacklisted": blacklisted,
            "language": {"code2": code, "forced": forced, "hi": False}}


def _file(source="sonarr"):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES (?, 3, 'Show', 'series')", (source,))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, 30, '/tv/a.mkv', ?, ?)",
               (iid, '[{"id": 300, "season": 1, "episode": 1}]', time.time()))
    return db.q1("SELECT * FROM files WHERE id=?", (fid,)), db.q1("SELECT * FROM items WHERE id=?", (iid,))


def test_current_sub_picks_newest_unbanned_in_language():
    hist = [{"provider": None, "subs_id": None, "subtitles_path": None, "language": {"code2": "en"}},  # a sync row
            _h(code="es", subs_id="1"), _h(forced=True, subs_id="2"), _h(subs_id="3"), _h(subs_id="4")]
    assert actions._current_sub(hist, "en")["subs_id"] == "3"
    assert actions._current_sub([_h(blacklisted=True), _h(subs_id="9")], "en") is None  # newest already banned
    assert actions._current_sub([], "en") is None


def test_resub_blacklists_the_current_subtitle_instead_of_searching(monkeypatch):
    calls = []
    monkeypatch.setattr(arr.Bazarr, "history", lambda self, episode_id=None, radarr_id=None, length=25: [_h()])
    monkeypatch.setattr(arr.Bazarr, "blacklist", lambda self, h, lang, **ids: calls.append(("ban", h["subs_id"], ids)))
    monkeypatch.setattr(arr.Bazarr, "search_episode", lambda self, s, e, lang: calls.append(("search", e)))
    f, item = _file()
    out = actions._resub(f, item)
    assert calls == [("ban", "652367", {"series_id": 3, "episode_id": 300})]
    assert "blacklisted" in out and "subsource 652367" in out


def test_resub_falls_back_to_search_when_bazarr_refuses(monkeypatch):
    calls = []

    def boom(self, h, lang, **ids):
        raise arr.ServiceError("bazarr POST -> 500: Subtitles file not found")

    monkeypatch.setattr(arr.Bazarr, "history", lambda self, episode_id=None, radarr_id=None, length=25: [_h()])
    monkeypatch.setattr(arr.Bazarr, "blacklist", boom)
    monkeypatch.setattr(arr.Bazarr, "search_movie", lambda self, r, lang: calls.append(("search", r)))
    f, item = _file("radarr")
    out = actions._resub(f, item)
    assert calls == [("search", 3)] and "blacklisted" not in out


def test_copy_timeout_scales_with_size():
    assert trackedit.copy_timeout(100 * 1024 * 1024) == trackedit.COPY_MIN_SECS
    assert trackedit.copy_timeout(3 * 1024 ** 3) >= 380  # 3 GB failed at 76s before
    assert trackedit.copy_timeout(500 * 1024 ** 3) < trackedit.COPY_HARD_LIMIT


def test_copy_size_check_allows_small_growth_not_truncation():
    mb = 1024 * 1024
    assert trackedit.copy_size_problem(134 * mb, int(134 * mb * 1.011)) is None  # Cane Toads: +1.1%
    assert "truncated" in trackedit.copy_size_problem(1000 * mb, 950 * mb)
    assert trackedit.copy_size_problem(1000 * mb, 1100 * mb)


def test_whisper_subtitle_is_never_blacklisted():
    # Banning Bazarr's whisperai sub deleted it and left the file with no subtitle at all (DBZ S06E22)
    assert actions._current_sub([_h(provider="whisperai", subs_id="w1")], "en") is None
    assert actions._current_sub([_h(provider="whisperai", subs_id="w1"), _h(subs_id="5")], "en") is None


def test_resub_blocked_for_embedded_offset_subgen_subs_and_repeats():
    import json
    f, _ = _file()
    fid = f["id"]
    assert actions.resub_blocker(fid, "subs_missing") == ""
    # out of sync in an embedded track: a new sidecar leaves it as it is
    assert "embedded" in actions.resub_blocker(fid, "subs_out_of_sync", {"tracks": ["English SDH"]})
    assert actions.resub_blocker(fid, "subs_out_of_sync", {"tracks": ["a.en.srt"]}) == ""
    # three manual resubs in a week and the issue is back
    now = time.time()
    for _ in range(2):
        db.x("INSERT INTO actions(file_id, kind, status, created_at, updated_at) VALUES (?, 'resub', 'done', ?, ?)",
             (fid, now, now))
    assert actions.resub_blocker(fid, "subs_missing") == ""
    assert "2 times" in actions.resub_blocker(fid, "subs_missing", manual=True)
    # subgen made the current sidecar: Bazarr would overwrite or delete it
    pay = {"files": [{"file_id": fid, "status": "done", "srt": "/tv/a.en.srt"}]}
    db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, 'subgen_transcribe', 'done', ?, ?, ?)",
         (fid, json.dumps(pay), now, now))
    assert "subgen" in actions.resub_blocker(fid, "subs_missing")


def test_scanner_reads_sidecars_past_six_embedded_tracks():
    from anotherr import scanner
    tracks = [{"external": False, "i": i} for i in range(8)] + [{"external": True, "i": 99}]
    got = scanner._capped(tracks)
    assert [t["i"] for t in got] == [0, 1, 2, 3, 4, 5, 99]
