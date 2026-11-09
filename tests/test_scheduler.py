import datetime as dt
import os
import time

import pytest

from anotherr import arr, db, scanner, scheduler, settings, sync
from anotherr.schedule_util import active_window, next_window_start


def test_empty_windows_mean_anytime_and_next_window():
    assert active_window([])[0]["anytime"]
    mon_noon = dt.datetime(2026, 11, 2, 12, 0)
    nxt = next_window_start([{"days": [0, 1, 2, 3, 4, 5, 6], "start": "01:00", "end": "07:00"}], mon_noon)
    assert nxt == dt.datetime(2026, 11, 3, 1, 0)
    assert next_window_start([], mon_noon) is None


def test_next_run_explains_itself():
    scheduler.STATUS["pause_reasons"] = []
    assert "around the clock" in scheduler.next_run(settings.get())["why"]


EM = os.environ["ANOTHERR_EMHTTP"]


@pytest.fixture
def array(monkeypatch):
    """cache (SSD), disk1 spinning, disk2 + disk3 spun down; one due file on each."""
    os.makedirs(EM, exist_ok=True)
    with open(os.path.join(EM, "disks.ini"), "w") as fh:
        fh.write('["cache"]\nname="cache"\nrotational="0"\nspundown="0"\ntype="Cache"\n'
                 '["disk1"]\nname="disk1"\nrotational="1"\nspundown="0"\n'
                 '["disk2"]\nname="disk2"\nrotational="1"\nspundown="1"\n'
                 '["disk3"]\nname="disk3"\nrotational="1"\nspundown="1"\n')
    open(os.path.join(EM, "var.ini"), "w").write('mdResyncPos="0"\nshareMoverActive="no"\n')
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 1, 'S', 'series')")
    ids = {}
    for n, disk in enumerate(("cache", "disk1", "disk2", "disk3")):
        ids[disk] = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, disk, first_seen) VALUES (?, ?, ?, ?, ?)",
                         (iid, n, f"/tv/S/{disk}.mkv", disk, time.time()))
    scanned = []

    def fake_scan(fid, profile=None, source="", seed=None, slot="", why=""):
        scanned.append((slot, db.q1("SELECT disk FROM files WHERE id=?", (fid,))["disk"], why))
        db.x("UPDATE files SET last_scan_at=? WHERE id=?", (time.time(), fid))
        db.x("DELETE FROM queue WHERE file_id=?", (fid,))

    monkeypatch.setattr(scanner, "scan_file", fake_scan)
    settings.state_set("rotation_last", None)
    yield ids, scanned
    for f in ("disks.ini", "var.ini"):
        os.remove(os.path.join(EM, f))


def _worker():
    w = scheduler.Worker(inline=True)
    w._last_poll = w._last_recheck = w._last_auto = time.time()
    return w


def test_cache_slot_plus_one_disk_preferring_spinning(array):
    ids, scanned = array
    w = _worker()
    w.tick()
    assert scanned == [("cache", "cache", "cache/SSD"), ("hdd1", "disk1", "already spinning")]
    w.tick()   # disk1 has nothing left -> wake the next idle disk, one at a time
    assert scanned[2:] == [("hdd1", "disk2", "woke idle disk (rotation)")]


def test_max_disks_two_uses_two_different_disks(array):
    ids, scanned = array
    settings.save({"schedule": {"max_disks": 2, "cache_slot": False}})
    _worker().tick()
    assert [d for _, d, _ in scanned] == ["disk1", "disk2"]


def test_spinup_never_only_uses_spinning_disks_but_queue_may_wake(array):
    ids, scanned = array
    settings.save({"schedule": {"spinup": "never", "cache_slot": False}})
    w = _worker()
    w.tick()
    w.tick()
    assert [d for _, d, _ in scanned] == ["disk1"]          # disk2/3 left asleep
    scheduler.enqueue([ids["disk3"]], "quick")               # ...unless you queue a file there
    w.tick()
    assert scanned[-1][1] == "disk3" and "woke disk" in scanned[-1][2]


def test_quiet_window_mover_and_parity_pause(array):
    ids, scanned = array
    settings.save({"schedule": {"quiet_windows": [{"days": list(range(7)), "start": "00:00", "end": "00:00"}]}})
    assert any(r.startswith("quiet window") for r in scheduler.pause_reasons(settings.get()))
    _worker().tick()
    assert scanned == []
    settings.save({"schedule": {"quiet_windows": []}})
    open(os.path.join(EM, "var.ini"), "w").write('mdResyncPos="0"\nshareMoverActive="yes"\n')
    assert "mover running" in scheduler.pause_reasons(settings.get())
    open(os.path.join(EM, "var.ini"), "w").write('mdResyncPos="123"\nshareMoverActive="no"\n')
    assert "parity check running" in scheduler.pause_reasons(settings.get())


def test_streams_dont_pause_by_default(array, monkeypatch):
    ids, scanned = array
    monkeypatch.setattr(arr.Tautulli, "configured", property(lambda self: True))
    monkeypatch.setattr(arr.Tautulli, "sessions", lambda self: [{"file": "/plex/S/disk1.mkv", "title": "S", "state": "playing"}])
    scheduler._streams_cache["t"] = 0
    settings.save({"schedule": {"cache_slot": False}})
    _worker().tick()
    assert [d for _, d, _ in scanned] == ["disk1"]   # still scans the disk Plex is reading
    settings.save({"schedule": {"stream_pause": "same_disk"}})
    scheduler._streams_cache["t"] = 0
    db.x("UPDATE files SET last_scan_at=NULL")
    scanned.clear()
    _worker().tick()
    assert [d for _, d, _ in scanned] == ["disk2"]   # same_disk option skips it


def _fake_sonarr(monkeypatch, files):
    settings.save({"sonarr": {"url": "http://s", "api_key": "k"}})
    series = [{"id": 1, "title": "Courage", "runtime": 11, "statistics": {"episodeFileCount": 1}, "path": "/tv/C"}]
    monkeypatch.setattr(arr.Sonarr, "series", lambda self, i=None: series[0] if i else series)
    monkeypatch.setattr(arr.Sonarr, "episodes", lambda self, i: [{"id": 10 + f["id"], "seasonNumber": 1, "episodeNumber": f["id"],
                                                                   "title": "t", "episodeFileId": f["id"]} for f in files])
    monkeypatch.setattr(arr.Sonarr, "episode_files", lambda self, i: files)


def test_new_files_after_first_sync_are_queued(monkeypatch):
    files = [{"id": 1, "path": "/tv/C/1.mkv", "size": 1}]
    _fake_sonarr(monkeypatch, files)
    sync.full_sync()                                   # first sync: nothing queued
    assert db.scalar("SELECT COUNT(*) FROM queue") == 0
    files.append({"id": 2, "path": "/tv/C/2.mkv", "size": 2})  # ViSiON replacement arrives
    files[0]["size"] = 5                                        # and episode 1 was replaced
    sync.full_sync()
    queued = {r["arr_file_id"] for r in db.q("SELECT f.arr_file_id FROM queue q JOIN files f ON f.id=q.file_id")}
    assert queued == {1, 2}


def test_history_poll_catches_imports_without_webhooks(monkeypatch):
    files = [{"id": 1, "path": "/tv/C/1.mkv", "size": 1}]
    _fake_sonarr(monkeypatch, files)
    sync.full_sync()
    files.append({"id": 3, "path": "/tv/C/3.mkv", "size": 3})
    monkeypatch.setattr(arr.Sonarr, "history_since", lambda self, iso: [{"eventType": "downloadFolderImported", "seriesId": 1},
                                                                         {"eventType": "grabbed", "seriesId": 2}])
    assert sync.poll_imports() == 1
    assert settings.state_get("imports_polled_at") > 0


def test_any_mode_pauses_everything(monkeypatch):
    settings.save({"schedule": {"stream_pause": "any"}})
    monkeypatch.setattr(arr.Tautulli, "configured", property(lambda self: True))
    monkeypatch.setattr(arr.Tautulli, "sessions", lambda self: [{"file": "/x/y.mkv", "title": "", "state": ""}])
    scheduler._streams_cache["t"] = 0
    assert "1 active Plex stream(s)" in scheduler.pause_reasons(settings.get())


def _file(missing: int) -> int:
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', ?, 'Gone', 'series')", (77 + missing,))
    return db.x("INSERT INTO files(item_id, arr_file_id, arr_path, disk, first_seen, missing) VALUES (?,?,?,?,?,?)",
                (iid, 78 + missing, f"/tv/Gone/{missing}.mkv", "cache", time.time(), missing))


def test_enqueue_skips_files_that_left_the_library():
    live, gone = _file(0), _file(1)
    assert scheduler.enqueue([live, gone], "quick") == 1
    assert [r["file_id"] for r in db.q("SELECT file_id FROM queue")] == [live]


def test_orphan_queue_rows_dont_count_or_keep_forcing_on():
    live, gone = _file(0), _file(1)
    db.x("INSERT INTO queue(file_id, profile, source, created_at) VALUES (?, 'quick', 'action', 0)", (gone,))
    assert scheduler.queue_count() == 0 and scheduler.queue_count(due_only=True) == 0
    scheduler.STATUS["pause_reasons"] = []
    assert "queued" not in scheduler.next_run(settings.get())["why"]
    scheduler.enqueue([live], "quick")
    assert scheduler.queue_count() == 1
