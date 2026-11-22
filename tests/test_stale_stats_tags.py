"""mkvmerge statistics tags copied unchanged into a file cut from a longer one (Adventure Time S06E21: 11:12 file,
tags still saying 22:34) must not read as a stream outlasting its container."""
from anotherr import media


def _probe(container, tag, real=None):
    v = {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {"DURATION": tag, "DURATION-eng": tag}}
    a = {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng", "DURATION": tag}}
    if real:
        v["duration"] = real
    return {"format": {"duration": str(container)}, "streams": [v, a]}


def test_stale_tag_longer_than_container_is_unknown():
    info = media.summarize(_probe(672.4, "00:22:34.604000000"))
    assert info["video"][0]["duration"] == 0.0
    assert info["audio"][0]["end"] is None


def test_tag_within_container_is_kept():
    info = media.summarize(_probe(1354.6, "00:22:34.604000000"))
    assert round(info["video"][0]["duration"], 1) == 1354.6
    # a tag that ends early is still reported (that's the real "audio stops before the file" case)
    info = media.summarize(_probe(1800.0, "00:22:34.604000000"))
    assert round(info["audio"][0]["end"], 1) == 1354.6


def test_real_stream_duration_is_not_second_guessed():
    info = media.summarize(_probe(672.4, "00:22:34.604000000", real="1354.6"))
    assert info["video"][0]["duration"] == 1354.6
