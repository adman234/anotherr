"""Sidecar subtitles on another Unraid disk.

Unraid can keep a folder's files on different disks: a replaced video lands on the cache while its old .en.srt stays
on disk3. Anotherr reads the video through its physical path, so the .srt is only visible in the user-share folder
(map_path(arr_path)). Scans, the streams view and the stale-issue recheck must look there too."""
import os
import shutil
import subprocess
import time

import pytest
from test_scan import CUES, TEXTS, _add, _small_profiles, _srt, fake_whisper  # noqa: F401

from anotherr import db, media, scanner, scheduler, settings


def _split(tmp_path, name="Show - S01E01.mp4"):
    phys, share = tmp_path / "cache" / "Show", tmp_path / "user" / "Show"
    phys.mkdir(parents=True)
    share.mkdir(parents=True)
    return phys / name, share / name


def test_file_sidecars_sees_the_share_folder_and_lists_each_name_once(tmp_path):
    video, shared = _split(tmp_path)
    video.write_bytes(b"x")
    shared.write_bytes(b"x")  # the share shows the video too
    (tmp_path / "user" / "Show" / "Show - S01E01.en.srt").write_text("x")
    (tmp_path / "cache" / "Show" / "Show - S01E01.de.srt").write_text("x")
    (tmp_path / "user" / "Show" / "Show - S01E01.de.srt").write_text("x")  # the same file seen through the share
    f = {"arr_path": str(shared), "local_path": str(video)}
    subs = scanner.file_sidecars(f, str(video))
    assert sorted((s["lang"], os.path.dirname(s["path"])) for s in subs) == [
        ("eng", str(shared.parent)), ("ger", str(video.parent))]  # the physical copy wins for a duplicate name
    assert media.sidecar_subs(str(video))[0]["lang"] == "ger"  # the old, physical-only view misses the .en.srt


def test_recheck_rescans_when_a_subtitle_appears_on_another_disk(tmp_path):
    settings.save({"languages": {"subtitle_language": "eng"}})
    video, shared = _split(tmp_path)
    video.write_bytes(b"x")
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 7, 'Show', 'series')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, label, first_seen) VALUES (?,?,?,?,?,?)",
               (iid, 9, str(shared), str(video), "S01E01", time.time()))
    db.x("INSERT INTO issues(file_id, code, severity, message, status, created_at) VALUES (?, 'subs_missing', 'error', "
         "'m', 'open', ?)", (fid, time.time() - 60))
    assert scheduler.recheck_subs(settings.get()) == 0
    (shared.parent / "Show - S01E01.en.srt").write_text("x")  # Bazarr wrote it through the share, onto disk3
    assert scheduler.recheck_subs(settings.get()) == 1
    assert db.scalar("SELECT source FROM queue WHERE file_id=?", (fid,)) == "recheck"


@pytest.fixture(scope="module")
def bare_video(tmp_path_factory):
    """90 s, one Japanese audio track, no subtitles at all."""
    d = tmp_path_factory.mktemp("bare")
    out = d / "Show - S01E01.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=90",
                    "-f", "lavfi", "-i", "sine=f=880:d=90", "-map", "0", "-map", "1", "-c:v", "mpeg4", "-c:a", "aac",
                    "-metadata:s:a:0", "language=jpn", str(out)], check=True)
    return out


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
@pytest.mark.parametrize("srt_on_other_disk", [False, True])
def test_scan_counts_an_en_srt_that_only_the_share_folder_shows(tmp_path, bare_video, fake_whisper,  # noqa: F811
                                                               srt_on_other_disk):
    settings.save({"languages": {"subtitle_language": "eng"}})
    _small_profiles()
    video, shared = _split(tmp_path, bare_video.name)
    shutil.copy(bare_video, video)
    if srt_on_other_disk:
        (shared.parent / "Show - S01E01.en.srt").write_text(_srt(CUES, TEXTS))
    fid = _add(video, original="jpn", anime=0)
    db.x("UPDATE files SET arr_path=? WHERE id=?", (str(shared), fid))
    res = scanner.scan_file(fid, "standard")
    assert res["status"] == "done", res.get("error")
    codes = {i["code"] for i in res["issues"]}
    assert ("subs_missing" in codes) is not srt_on_other_disk, codes
    r = db.unj(db.q1("SELECT result FROM scans WHERE id=?", (res["scan_id"],))["result"])
    external = [s for s in r["streams"]["subs"] if s.get("external")]
    assert [os.path.dirname(s["path"]) for s in external] == ([str(shared.parent)] if srt_on_other_disk else [])
