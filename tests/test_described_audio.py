"""Audio-description (narrated for the blind) tracks: only-AD files and an AD default."""
from anotherr import defaults, media, scanner


def _ctx(audio, expected="eng"):
    c = scanner.Ctx(file={"id": 1}, item={"id": None, "kind": "series", "original_language": expected,
                                          "language_override": ""},
                    cfg={"anime": {"dub_language": "eng", "prefer_dual_audio": False}}, profile="standard", pcfg={}, seed=1)
    c.info = {"audio": [{"a_index": i, "default": False, "commentary": False, **a} for i, a in enumerate(audio)]}
    c.expected = expected
    return c


def _codes(ctx):
    scanner.check_described_audio(ctx)
    return {i["code"] for i in ctx.issues}


def test_described_regex():
    r = media.DESCRIBED_RE
    for t in ("Descriptive", "English [Audio Description]", "English AD", "DVS", "Visually Impaired"):
        assert r.search(t), t
    for t in ("English", "Surround 5.1", "Commentary", "Dad"):
        assert not r.search(t), t


def test_only_described_english_is_flagged():
    c = _ctx([{"lang": "eng", "title": "Descriptive", "described": True}])
    assert _codes(c) == {"audio_description_only"}


def test_plain_track_alongside_is_fine():
    c = _ctx([{"lang": "eng", "title": "English", "described": False},
              {"lang": "eng", "title": "Descriptive", "described": True}])
    assert _codes(c) == set()


def test_untagged_plain_track_counts_as_covering():
    c = _ctx([{"lang": "", "title": "", "described": False}, {"lang": "eng", "title": "AD", "described": True}])
    assert _codes(c) == set()


def test_unrelated_language_described_track_ignored():
    c = _ctx([{"lang": "ger", "title": "Descriptive", "described": True}])
    assert _codes(c) == set()


def test_pick_audio_never_picks_described():
    tracks = [
        {"a_index": 0, "effective": "eng", "confident": True, "all_agree": True, "windows": [1], "described": True,
         "title": "", "channels": 6},
        {"a_index": 1, "effective": "eng", "confident": True, "all_agree": True, "windows": [1], "title": "", "channels": 2},
    ]
    assert defaults.pick_audio(tracks, "eng")["a_index"] == 1


def test_plan_moves_default_off_described_track():
    tracks = [
        {"a_index": 0, "effective": "eng", "confident": True, "all_agree": True, "windows": [1], "described": True,
         "default": True, "title": "", "channels": 2, "tag": "eng"},
        {"a_index": 1, "effective": "eng", "confident": True, "all_agree": True, "windows": [1], "default": False,
         "title": "", "channels": 2, "tag": "eng"},
    ]
    plan = defaults.plan_file(tracks, [], {"default_audio_not_preferred"}, "eng", "eng")
    assert plan["audio"] and plan["audio"]["to"] == 1
