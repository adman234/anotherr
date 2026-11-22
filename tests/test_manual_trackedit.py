"""A person's own retag / default-audio proposal for one file (the automatic proposers only retag toward the
series' language, so a Pokemon dub tagged Finnish could never be fixed)."""
import json
import time

import pytest

from anotherr import db, media, retag, scanner, settings, trackedit


@pytest.fixture
def ep(tmp_path, monkeypatch):
    settings.save({"retag": {"enabled": True, "allowed_paths": [str(tmp_path)]}})
    path = str(tmp_path / "ep.mkv")
    open(path, "wb").close()
    monkeypatch.setattr(scanner, "resolve_path", lambda f: path)
    monkeypatch.setattr(media, "ffprobe", lambda p: {})
    monkeypatch.setattr(media, "summarize", lambda probe: {"audio": [
        {"lang": "fin", "default": True}, {"lang": "jpn", "default": False}], "subs": []})
    iid = db.x("INSERT INTO items(source, arr_id, title, kind, original_language) VALUES ('sonarr', ?, 'Pokemon', "
               "'series', 'jpn')", (int(time.time() * 1e6) % 10**9,))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, label, first_seen) "
               "VALUES (?, 1, ?, ?, 'S05E13', ?)", (iid, path, path, time.time()))
    return iid, fid


def _payload(aid):
    return json.loads(db.q1("SELECT payload FROM actions WHERE id=?", (aid,))["payload"])


def test_manual_retag_proposal(ep):
    iid, fid = ep
    aid = trackedit.propose_manual(fid, "retag_audio", 1, "eng")
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["kind"] == "retag_audio" and a["status"] == "proposed"
    e = _payload(aid)["files"][0]
    assert e["ops"] == [{"type": "a", "index": 0, "language": "eng", "old": "fin"}]
    assert "language=eng" in e["command"]
    assert trackedit.propose_manual(fid, "retag_audio", 1, "eng") == 0  # one open manual proposal per file
    retag.refresh_item(iid)  # the automatic refresh must not withdraw it
    assert db.q1("SELECT status FROM actions WHERE id=?", (aid,))["status"] == "proposed"


def test_manual_default_audio_proposal(ep):
    _, fid = ep
    aid = trackedit.propose_manual(fid, "set_default_audio", 2)
    assert _payload(aid)["files"][0]["ops"] == [{"type": "a", "index": 1, "default": True, "expect_lang": "jpn"}]


@pytest.mark.parametrize("kind,track,lang", [("retag_audio", 3, "eng"), ("retag_audio", 1, "xx"),
                                             ("retag_audio", 1, "fin"), ("set_default_audio", 1, ""),
                                             ("resub", 1, "")])
def test_manual_rejects_bad_requests(ep, kind, track, lang):
    with pytest.raises(ValueError):
        trackedit.propose_manual(ep[1], kind, track, lang)
