"""Runtime: per-season baselines and a metadata second opinion (regression cases from the Nov 3/5 reviews)."""
import json
import time

from anotherr import db, ocr, scanner


def _series(title, series_runtime, files, ep_runtime=None):
    """files: [(minutes or None, mapped, season, ep_runtime)]"""
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, runtime) VALUES ('sonarr', ?, ?, 'series', ?)",
               (abs(hash(title)) % 100000, title, series_runtime))
    ids = []
    for n, (minutes, mapped, season, ert) in enumerate(files):
        eps = [{"id": n * 10 + k, "season": season, "episode": n * mapped + k + 1, "title": f"E{k}",
                "runtime": ert if ert is not None else 0} for k in range(mapped)]
        ids.append(db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, label, duration, first_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (iid, n, f"/tv/{title}/{n}.mkv", json.dumps(eps),
                                                         f"S{season:02d}E{n + 1:02d}", minutes * 60 if minutes else None,
                                                         time.time())))
    return iid, ids


def _check(fid, minutes):
    f = db.q1("SELECT * FROM files WHERE id=?", (fid,))
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    ctx = scanner.Ctx(file=f, item=item, cfg={}, profile="standard", pcfg={}, seed=1)
    scanner.check_runtime(ctx, minutes * 60)
    return {i["code"]: i for i in ctx.issues}, ctx.result


def test_daniel_tiger_two_story_files_pass():
    _, ids = _series("Daniel Tiger", 30, [(26.5, 2, 1, 30)] * 12)
    codes, res = _check(ids[0], 26.5)
    assert codes == {}, codes
    assert res["runtime_estimate"]["files_source"] == "season 1 files"


def test_courage_four_segments_at_one_episode_length_fails_both_opinions():
    _, ids = _series("Courage", 11, [(22.0, 2, 2, 11)] * 10 + [(21.7, 4, 2, 11)])
    codes, _ = _check(ids[-1], 21.7)
    assert codes["duration_short"]["severity"] == "error"


def test_run_bts_short_season_one_passes():
    # later seasons ~22-35 min, season 1 episodes 3-15 min (TVDB: S01E01 = 3 min)
    files = [(30.0, 1, 4, 30)] * 8 + [(8.0, 1, 1, 8)] * 6 + [(3.7, 1, 1, 3)]
    _, ids = _series("Run BTS", 22, files)
    codes, res = _check(ids[-1], 3.7)
    assert codes == {}, codes
    assert res["runtime_estimate"]["files_source"] == "season 1 files"


def test_rocko_two_segments_pass_when_tvdb_agrees():
    # TVDB numbers season 2 by 11-min segment; most files in the season are single 23-min episodes
    files = [(23.3, 1, 2, 11)] * 8 + [(24.0, 2, 2, 11)]
    _, ids = _series("Rocko", 23, files)
    codes, _ = _check(ids[-1], 24.0)
    assert "duration_short" not in codes and "duration_short_warn" not in codes, codes


def test_double_length_needs_both_opinions_else_title_card_review():
    files = [(32.0, 1, 6, 0)] * 8 + [(44.3, 1, 6, 0)]
    _, ids = _series("Run BTS 6", 22, files)       # series runtime 22 says 2x, the season's files say 1.4x
    codes, res = _check(ids[-1], 44.3)
    assert codes == {}
    files = [(60.0, 1, 1, 30)] + [(30.0, 1, 1, 30)] * 8   # not the last episode of the season
    _, ids = _series("Doubled", 30, files)
    codes, _ = _check(ids[0], 60.0)
    assert codes["double_length"]["severity"] == "warning"
    files = [(60.0, 1, 1, 0)] + [(30.0, 1, 1, 0)] * 8
    _, ids = _series("Disputed", 60, files)        # files say 2x, metadata says normal -> no issue, review queued
    codes, res = _check(ids[0], 60.0)
    assert codes == {} and res["runtime_disputed"]


def test_season_finale_double_length_is_info(monkeypatch):
    files = [(30.0, 1, 4, 30)] * 9 + [(55.4, 1, 4, 30)]
    iid, ids = _series("Ted Lasso", 30, files)
    monkeypatch.setattr(ocr, "series_episodes", lambda item: [{"id": i, "season": 4, "episode": i} for i in range(1, 11)])
    codes, _ = _check(ids[-1], 55.4)
    assert codes["double_length"]["severity"] == "info" and codes["double_length"]["details"]["season_finale"]


def test_too_few_files_uses_metadata_only():
    _, ids = _series("New Show", 30, [(26.5, 2, 1, 0), (None, 2, 1, 0)])
    codes, res = _check(ids[0], 26.5)
    assert res["runtime_estimate"]["files_min"] is None
    assert "duration_short" not in codes and "duration_short_warn" in codes  # unverified metadata: warn only


def test_segment_breaks_skipped_when_duration_off():
    c = scanner.Ctx(file={"id": 1, "episodes": json.dumps([{"id": 1}, {"id": 2}])}, item={"kind": "series"},
                    cfg={}, profile="standard", pcfg={"subtitles": True}, seed=1)
    c.info = {"duration": 1300, "video": [{}], "chapters": []}
    c.issue("integrity", "duration_short", "error", "x")
    scanner.check_multi_episode(c)
    assert "skipped" in c.result["segment_breaks"] and c.result["segment_starts"] == [0.0]
