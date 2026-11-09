"""size_too_large / size_too_small: only clear outliers, from ffprobe data (no extra decode)."""
import json
import math

import pytest

from anotherr import scanner, settings

GB, MB = scanner.GB, scanner.MB
HOUR = 3600.0


def _run(size, dur, height=1080, kind="series", episodes=1, cfg=None, path=""):
    checks = {"file_size": cfg} if cfg is not None else {}
    eps = json.dumps([{"id": i} for i in range(episodes)]) if kind == "series" else "[]"
    c = scanner.Ctx(file={"id": 1, "episodes": eps}, item={"kind": kind}, cfg={"scan": {"checks": checks}}, profile="quick",
                    pcfg={}, seed=1)
    c.info = {"size": int(size), "duration": dur, "video": [{"height": height}] if height is not None else []}
    c.path = path
    scanner.check_file_size(c, dur)
    return {i["code"]: i for i in c.issues}


def _codes(*a, **kw):
    return set(_run(*a, **kw))


@pytest.mark.parametrize("height,tier", [(480, "sd"), (699, "sd"), (700, "hd"), (1080, "hd"), (1199, "hd"),
                                         (1200, "uhd"), (2160, "uhd")])
def test_tier_boundaries(height, tier):
    assert scanner._res_tier(height) == tier
    assert scanner._res_tier(None) == ""


@pytest.mark.parametrize("height,small,large,hours", [(480, 0.15, 6, 4), (1080, 0.30, 20, 2), (2160, 1.0, 45, 1.5)])
def test_rate_boundaries_per_tier_for_movies(height, small, large, hours):
    # movie lengths chosen so the absolute floor (500 MB) and ceiling (80 GB) stay out of the way
    dur = hours * HOUR
    size = lambda rate: rate * hours * GB  # noqa: E731
    assert _codes(math.ceil(size(small)) + 1, dur, height, kind="movie") == set()      # just above the floor
    assert _codes(math.floor(size(small)) - 1, dur, height, kind="movie") == {"size_too_small"}
    assert _codes(math.floor(size(large)) - 1, dur, height, kind="movie") == set()     # just under the ceiling
    assert _codes(math.ceil(size(large)) + 1, dur, height, kind="movie") == {"size_too_large"}


def test_message_and_evidence():
    r = _run(54.2 * GB, 1 * HOUR + 58 * 60, 1080, kind="movie")["size_too_large"]
    assert r["severity"] == "warning"
    assert r["message"] == "54.2 GB for 1h58m of 1080p video (27.6 GB/hour; flagged above 20 GB/hour for HD)."
    d = r["details"]
    assert d["tier"] == "hd" and d["gb_per_hour"] == 27.56 and d["threshold"] == "20 GB/hour" and d["size_bytes"] == int(54.2 * GB)
    assert d["duration_s"] == 7080.0 and d["kind"] == "movie"


def test_absolute_floor_episode_and_movie():
    # a short SD file where only the absolute floor applies: 49 MB in 6 minutes is 0.48 GB/h, above the 0.15 GB/h floor
    r = _run(49 * MB, 6 * 60, 480)
    assert set(r) == {"size_too_small"} and r["size_too_small"]["details"]["threshold"] == "50 MB"
    assert _codes(50 * MB, 6 * 60, 480) == set()
    # movies have a 500 MB floor
    r = _run(499 * MB, 15 * 60, 480, kind="movie")
    assert r["size_too_small"]["details"]["threshold"] == "500 MB"
    assert _codes(500 * MB, 15 * 60, 480, kind="movie") == set()


def test_absolute_ceiling_episode_and_movie():
    # SD rate ceiling is 6 GB/h, so a 20 GB file only trips the absolute ceiling when the rate is below it
    assert _codes(20 * GB, 4 * HOUR, 480) == set()          # 5 GB/h, at the 20 GB ceiling
    r = _run(20 * GB + 1, 4 * HOUR, 480)
    assert set(r) == {"size_too_large"} and r["size_too_large"]["details"]["threshold"] == "20 GB"
    assert _codes(80 * GB, 14 * HOUR, 480, kind="movie") == set()
    assert set(_run(80 * GB + 1, 14 * HOUR, 480, kind="movie")) == {"size_too_large"}


def test_multi_episode_file_uses_real_duration_and_scales_the_ceiling():
    # 3 episodes in 3 hours of HD (20 GB/h rate ceiling, absolute ceiling 3 x 20 GB)
    assert _codes(55 * GB, 3 * HOUR, 1080, episodes=3) == set()                 # 18.3 GB/h
    assert _codes(61 * GB, 3 * HOUR, 1080, episodes=3) == {"size_too_large"}    # 20.3 GB/h
    # a 2-episode SD file of 41 GB over 8 hours = 5.1 GB/h: under the rate, over a single episode's 20 GB, under 2 x 20
    assert _codes(39 * GB, 8 * HOUR, 480, episodes=2) == set()
    assert _codes(41 * GB, 8 * HOUR, 480, episodes=2) == {"size_too_large"}
    # per-hour is computed from real duration, so a double-length file is not judged as one episode
    assert _codes(0.7 * GB, 2 * 22 * 60, 480, episodes=2) == set()


def test_unknown_duration_and_short_files_skipped():
    assert _codes(1, 0, 1080) == set()
    assert _codes(1, None, 1080) == set()
    assert _codes(1 * MB, 59, 1080) == set()
    assert _codes(1 * MB, 60, 1080) == {"size_too_small"}


def test_unknown_resolution_uses_absolute_limits_only():
    assert _codes(21 * GB, HOUR, None) == {"size_too_large"}   # over the 20 GB episode ceiling
    assert _codes(10 * GB, HOUR, None) == set()                # no tier, so no rate rule
    assert _codes(49 * MB, 6 * 60, None) == {"size_too_small"}
    assert _codes(79 * GB, 20 * HOUR, None, kind="movie") == set()


def test_disabled_and_custom_settings():
    off = {**settings.DEFAULTS["scan"]["checks"]["file_size"], "enabled": False}
    assert _codes(1 * MB, HOUR, 1080, cfg=off) == set()
    custom = {**settings.DEFAULTS["scan"]["checks"]["file_size"], "too_large_gb_per_hour": {"sd": 6, "hd": 5, "uhd": 45}}
    assert _codes(6 * GB, HOUR, 1080, cfg=custom) == {"size_too_large"}
    assert _codes(6 * GB, HOUR, 1080) == set()


def test_size_falls_back_to_the_file_on_disk(tmp_path):
    f = tmp_path / "x.mkv"
    with open(f, "wb") as fh:
        fh.truncate(10 * MB)
    c = scanner.Ctx(file={"id": 1}, item={"kind": "series"}, cfg={}, profile="quick", pcfg={}, seed=1)
    c.info = {"duration": HOUR, "video": [{"height": 1080}]}
    c.path = str(f)
    scanner.check_file_size(c, HOUR)
    assert [i["code"] for i in c.issues] == ["size_too_small"]


def test_defaults_are_in_settings_and_survive_a_partial_save():
    d = settings.get()["scan"]["checks"]["file_size"]
    assert d["enabled"] and d["too_small_gb_per_hour"] == {"sd": 0.15, "hd": 0.30, "uhd": 1.0}
    assert d["too_large_gb_per_hour"] == {"sd": 6, "hd": 20, "uhd": 45}
    assert (d["min_episode_mb"], d["min_movie_mb"], d["max_episode_gb"], d["max_movie_gb"]) == (50, 500, 20, 80)
    settings.save({"scan": {"checks": {"file_size": {"too_large_gb_per_hour": {"hd": 25}}}}})
    d = settings.get()["scan"]["checks"]["file_size"]
    assert d["too_large_gb_per_hour"] == {"sd": 6, "hd": 25, "uhd": 45} and d["enabled"]


def test_no_action_is_proposed_for_size_issues():
    from anotherr import settings as s

    for code in ("size_too_large", "size_too_small"):
        assert code not in s.REDOWNLOAD_CODES and code not in s.RESUB_CODES
