"""Track-edit executor: every action ends done or failed, with progress, per-file logs and a reaper."""
import json
import shutil
import subprocess
import time

import pytest

from anotherr import db, media, settings, trackedit

HAVE_TOOLS = shutil.which("ffmpeg") and shutil.which("mkvpropedit")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(trackedit, "LOG_PATH", str(tmp_path / "log.jsonl"))
    root = tmp_path / "media"
    root.mkdir()
    settings.save({"retag": {"enabled": True, "allowed_paths": [str(root)]}})
    return root


def _media(path, audio=(("eng", 0), ("ger", 1), ("jpn", 0)), subs=(("eng", 1, 1, "Signs"), ("eng", 0, 0, "Full"))):
    """audio: (lang, default); subs: (lang, default, forced, title)"""
    srt = str(path) + ".srt"
    open(srt, "w").write("1\n00:00:00,500 --> 00:00:02,000\nhello there\n")
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3"]
    for _ in audio:
        args += ["-f", "lavfi", "-i", "sine=d=3"]
    for _ in subs:
        args += ["-i", srt]
    for i in range(1 + len(audio) + len(subs)):
        args += ["-map", str(i)]
    args += ["-c:v", "libx264", "-c:a", "aac", "-c:s", "srt" if str(path).endswith(".mkv") else "mov_text"]
    for i, (lang, dflt) in enumerate(audio):
        args += [f"-metadata:s:a:{i}", f"language={lang}", f"-disposition:a:{i}", "default" if dflt else "0"]
    for i, (lang, dflt, forced, title) in enumerate(subs):
        disp = "+".join([d for d, on in (("default", dflt), ("forced", forced)) if on]) or "0"
        args += [f"-metadata:s:s:{i}", f"language={lang}", f"-metadata:s:s:{i}", f"title={title}", f"-disposition:s:{i}", disp]
    subprocess.run(args + [str(path)], check=True)
    return str(path)


def _action(path, ops, kind="set_default_audio"):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', ?, 'Frog and Toad', 'series')",
               (int(time.time() * 1e6) % 10**9,))
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, label, first_seen) VALUES (?, 1, ?, ?, 'S02E06', ?)",
               (iid, path, path, time.time()))
    p = {"item_id": iid, "files": [{"file_id": fid, "label": "S02E06", "ops": ops, "scan_started": time.time() + 60}]}
    aid = db.x("INSERT INTO actions(file_id, kind, status, payload, created_at, updated_at) VALUES (?, ?, 'running', ?, ?, ?)",
               (fid, kind, json.dumps(p), time.time(), time.time()))
    return aid, fid


def _flags(path):
    info = media.summarize(media.ffprobe(path))
    return ([(a["lang"], a["default"]) for a in info["audio"]],
            [(s["default"], s["forced"]) for s in info["subs"] if not s.get("external")])  # MP4 drops sub titles


@pytest.mark.skipif(not HAVE_TOOLS, reason="ffmpeg + mkvpropedit needed")
@pytest.mark.parametrize("ext", ["mkv", "mp4"])
def test_default_audio_and_subtitle_in_one_write_then_undo(lib, ext):
    p = _media(lib / f"ep.{ext}")
    before = _flags(p)
    aid, fid = _action(p, [{"type": "a", "index": 0, "default": True, "expect_lang": "eng"},
                           {"type": "s", "index": 1, "default": True}])
    trackedit.run(aid)
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "done", a["result"]
    audio, subs = _flags(p)
    assert audio == [("eng", True), ("ger", False), ("jpn", False)]          # exactly one default audio
    assert subs == [(False, True), (True, False)]   # signs track: forced kept, no longer default; full track default
    assert "1 of 1 files done" not in a["result"] and "edited 1" in a["result"]
    assert db.q1("SELECT message FROM activity WHERE message LIKE ? ORDER BY id DESC LIMIT 1", (f"%#{aid} %edited%",))
    assert trackedit.undo(aid)["restored"] == 1
    assert _flags(p) == before                                               # full flag state restored


@pytest.mark.skipif(not HAVE_TOOLS, reason="ffmpeg + mkvpropedit needed")
def test_crash_still_ends_failed(lib, monkeypatch):
    p = _media(lib / "crash.mkv")
    aid, _ = _action(p, [{"type": "a", "index": 0, "default": True}])
    monkeypatch.setattr(trackedit.unraid if hasattr(trackedit, "unraid") else __import__("anotherr.unraid").unraid,
                        "spun_up_disks", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    trackedit.run(aid)
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "failed" and "crashed: boom" in a["result"]
    assert not trackedit._lock.locked()


def test_hung_file_times_out_and_batch_continues(lib, monkeypatch):
    monkeypatch.setattr(trackedit, "EDIT_TIMEOUT", 0.3)
    calls = []

    def fake(e, cfg, aid):
        calls.append(e["file_id"])
        if len(calls) == 1:
            time.sleep(1.0)  # the first file hangs
        return {"status": "edited", "why": "ok"}

    monkeypatch.setattr(trackedit, "edit_file", fake)
    aid, f1 = _action("/x/a.mkv", [{"type": "a", "index": 0, "default": True}])
    p = json.loads(db.q1("SELECT payload FROM actions WHERE id=?", (aid,))["payload"])
    p["files"].append(dict(p["files"][0], file_id=f1, label="S02E07"))
    db.x("UPDATE actions SET payload=? WHERE id=?", (json.dumps(p), aid))
    trackedit.run(aid)
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    files = json.loads(a["payload"])["files"]
    assert files[0]["status"] == "failed" and "no result after" in files[0]["outcome"]
    assert files[1]["status"] == "edited" and a["status"] == "failed"


def test_reaper_marks_stalled_and_frees_lock():
    _, fid = _action("/x/s.mkv", [])
    aid = db.x("INSERT INTO actions(file_id, kind, status, result, created_at, updated_at) VALUES (?, 'retag_audio', "
               "'running', 'queued: 0 of 1 files done', ?, ?)", (fid, time.time() - 3600, time.time() - 3600))
    db.x("UPDATE actions SET status='done' WHERE status='running' AND id != ?", (aid,))
    trackedit._lock.acquire()
    trackedit._owner.update(action=aid, since=time.time() - 3600)
    assert trackedit.reap() == 1
    a = db.q1("SELECT status, result FROM actions WHERE id=?", (aid,))
    assert a["status"] == "failed" and a["result"].startswith("failed (stalled)")
    assert not trackedit._lock.locked()
    # a late finish from the stalled thread can't overwrite the reaper's verdict
    assert trackedit._finish(aid, "done", "late") is False


def test_expand_clears_every_other_default_from_the_real_file():
    info = {"audio": [{"lang": "eng", "default": False}, {"lang": "ger", "default": True}, {"lang": "fre", "default": True}],
            "subs": []}
    ops = trackedit.expand([{"type": "a", "index": 0, "default": True}], info)
    assert ops == [{"type": "a", "index": 0, "default": True}, {"type": "a", "index": 1, "default": False},
                   {"type": "a", "index": 2, "default": False}]
    assert trackedit.mkv_args("f.mkv", ops)[2:] == ["--edit", "track:a1", "--set", "flag-default=1", "--edit", "track:a2",
                                                    "--set", "flag-default=0", "--edit", "track:a3", "--set", "flag-default=0"]


# ====================================================================== keeping edits off the cache pool

@pytest.mark.parametrize("name,ok", [("a.avi", False), ("A.DIVX", False), ("a.mkv", True), ("a.mp4", True), (None, True)])
def test_can_edit_skips_avi(name, ok):
    assert trackedit.can_edit(name) is ok


def test_preview_skips_avi(tmp_path):
    p = tmp_path / "x.avi"
    p.write_bytes(b"x")
    out = trackedit.preview(str(p), [{"type": "a", "index": 0, "language": "eng"}], {"retag": {}})
    assert out["method"] == "skip" and ".avi" in out["command"]


def test_write_path_prefers_physical_disk(tmp_path, monkeypatch):
    import anotherr.sync
    disk = tmp_path / "disk3" / "x.mkv"
    share = tmp_path / "user" / "x.mkv"
    for p in (disk, share):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x")
    monkeypatch.setattr(anotherr.sync, "map_path", lambda _: str(share))
    assert trackedit.write_path({"arr_path": "/tv/x.mkv"}, str(disk)) == str(disk)
    # physical path not visible (e.g. not mounted): fall back to the share path
    assert trackedit.write_path({"arr_path": "/tv/x.mkv"}, str(tmp_path / "nope" / "x.mkv")) == str(share)


def test_apply_copy_removes_temp_on_crash(tmp_path, monkeypatch):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"orig")

    def boom(path, *a):
        open(trackedit.tmp_path(path), "w").write("half a copy")
        raise RuntimeError("killed mid-copy")

    monkeypatch.setattr(trackedit, "_apply", boom)
    with pytest.raises(RuntimeError):
        trackedit.apply(str(p), [], "copy", 4)
    assert not (tmp_path / ".anotherr-edit.x.mp4").exists()
    assert p.read_bytes() == b"orig"


def test_sweep_temp_removes_logged_leftovers(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(trackedit, "LOG_PATH", str(log))
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    recent, old, mkv = (str(tmp_path / n) for n in ("a.mp4", "b.mp4", "c.mkv"))
    now = time.time()
    log.write_text("\n".join(json.dumps(r) for r in [
        {"event": "before", "method": "copy", "path": recent, "ts": now - 60},
        {"event": "before", "method": "copy", "path": old, "ts": now - 30 * 86400},
        {"event": "before", "method": "mkvpropedit", "path": mkv, "ts": now},
    ]) + "\nnot json\n")
    for p in (recent, old, mkv):
        open(trackedit.tmp_path(p), "w").write("x")

    with trackedit._lock:
        assert trackedit.sweep_temp() == 0  # an edit is running: its temp is live
    assert trackedit.sweep_temp() == 1
    assert not (tmp_path / ".anotherr-edit.a.mp4").exists()
    assert (tmp_path / ".anotherr-edit.b.mp4").exists()  # outside the window
    assert (tmp_path / ".anotherr-edit.c.mkv").exists()  # in-place edits never make one
