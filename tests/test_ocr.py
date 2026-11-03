import json
import shutil
import subprocess
import time

import pytest

from anotherr import db, ocr, scanner

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
pytestmark = pytest.mark.skipif(not (ocr.available() and shutil.which("ffmpeg") and __import__("os").path.exists(FONT)),
                                reason="tesseract, ffmpeg and a font are needed")


@pytest.fixture(scope="module")
def card(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("ocr") / "card.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=6", "-vf",
                    f"drawbox=x=200:y=250:w=880:h=220:color=0x2a1a40:t=fill,drawtext=fontfile={FONT}:"
                    "text='The Curse of Shirley':fontsize=72:fontcolor=0xf0e070:x=(w-tw)/2:y=320",
                    "-c:v", "libx264", "-preset", "ultrafast", out], check=True)
    return out


def _ctx(card, mapped_title):
    eps = [{"id": 1, "season": 2, "episode": 5, "title": "The Curse of Shirley"},
           {"id": 2, "season": 2, "episode": 6, "title": "Night of the Scarecrow"}]
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 5, 'Courage', 'series')")
    db.x("INSERT INTO files(item_id, arr_file_id, arr_path, episodes, first_seen) VALUES (?, 1, '/x', ?, ?)",
         (iid, json.dumps(eps), time.time()))
    ocr._cache.clear()
    mine = [e for e in eps if e["title"] == mapped_title]
    c = scanner.Ctx(file={"id": 9, "episodes": json.dumps(mine)}, item=db.q1("SELECT * FROM items WHERE id=?", (iid,)),
                    cfg={}, profile="standard", pcfg={}, seed=1)
    c.path = card
    return c


def test_matching_title_card_auto_passes(card):
    c = _ctx(card, "The Curse of Shirley")
    review = {"timestamps": [2.0, 4.0]}
    scanner.auto_title_cards(c, review)
    assert review.get("status") == "done" and review["verdict"] == "ok", c.result["title_cards_ocr"]
    assert c.issues == []


def test_other_episodes_title_card_is_wrong_content(card):
    c = _ctx(card, "Night of the Scarecrow")
    review = {"timestamps": [2.0, 4.0]}
    scanner.auto_title_cards(c, review)
    assert [i["code"] for i in c.issues] == ["wrong_content"]
    assert "Curse of Shirley" in c.issues[0]["message"] and review.get("status") is None  # stays for a person


def test_fuzzy_matching_thresholds():
    assert ocr.title_score("The Curse of Shirley", "» The Curse of Shir1ey -") > 0.9
    assert ocr.match(["Pilot program notes"], [{"id": 1, "title": "Pilot"}])  # exact short word
    assert not ocr.match(["Pilots"], [{"id": 1, "title": "Pilet"}])  # short titles must be near-exact
