"""Per-series runtime: segment-numbered shows (Daniel Tiger) vs wrong content (Courage tokar86a)."""
import json
import time

from anotherr import db, scanner


def _series(title, meta_runtime, files):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, runtime) VALUES ('sonarr', ?, ?, 'series', ?)",
               (abs(hash(title)) % 100000, title, meta_runtime))
    ids = []
    for n, (minutes, mapped) in enumerate(files):
        eps = [{"id": n * 10 + k, "season": 1, "episode": n * mapped + k + 1, "title": f"E{k}", "runtime": meta_runtime}
               for k in range(mapped)]
        ids.append(db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, duration, first_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (iid, n, f"/tv/{title}/{n}.mkv", json.dumps(eps),
                                                      minutes * 60 if minutes else None, time.time())))
    return iid, ids


def _check(fid, minutes):
    f = db.q1("SELECT * FROM files WHERE id=?", (fid,))
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    ctx = scanner.Ctx(file=f, item=item, cfg={}, profile="standard", pcfg={}, seed=1)
    scanner.check_runtime(ctx, minutes * 60)
    return {i["code"]: i for i in ctx.issues}, ctx.result


def test_daniel_tiger_two_story_files_pass():
    _, ids = _series("Daniel Tiger", 30, [(26.5, 2)] * 12 + [(26.5, 1)])
    codes, res = _check(ids[0], 26.5)
    assert codes == {}, codes
    assert res["runtime_estimate"]["source"] == "series files"
    assert abs(res["runtime_estimate"]["per_episode_min"] - 13.25) < 0.01
    # single-episode file at 2x the series' episode length -> double-length wording, not "multi-episode"
    codes, _ = _check(ids[-1], 26.5)
    assert list(codes) == ["double_length"] and "special" in codes["double_length"]["message"]


def test_courage_four_segment_file_at_one_episode_length_fails():
    _, ids = _series("Courage", 11, [(22.0, 2)] * 10 + [(21.7, 4)])
    codes, res = _check(ids[-1], 21.7)
    assert "duration_short" in codes and codes["duration_short"]["severity"] == "error"
    assert res["runtime_estimate"]["source"].startswith("metadata")  # 11 min agrees with the files


def test_too_few_files_falls_back_to_metadata():
    _, ids = _series("New Show", 30, [(26.5, 2), (None, 2)])
    codes, res = _check(ids[0], 26.5)
    assert res["runtime_estimate"]["source"] == "metadata"
    assert "duration_short" not in codes and "duration_short_warn" in codes  # unverified metadata: warn only


def test_segment_breaks_skipped_when_duration_off():
    c = scanner.Ctx(file={"id": 1, "episodes": json.dumps([{"id": 1}, {"id": 2}])}, item={"kind": "series"},
                    cfg={}, profile="standard", pcfg={"subtitles": True}, seed=1)
    c.info = {"duration": 1300, "video": [{}], "chapters": []}
    c.issue("integrity", "duration_short", "error", "x")
    scanner.check_multi_episode(c)
    assert "skipped" in c.result["segment_breaks"] and c.result["segment_starts"] == [0.0]
