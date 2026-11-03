import datetime as dt
import time

from anotherr import arr, db, scanner, scheduler, settings, sync
from anotherr.schedule_util import active_window, next_window_start


def test_empty_windows_mean_anytime_and_next_window():
    assert active_window([])[0]["anytime"]
    mon_noon = dt.datetime(2026, 11, 2, 12, 0)
    nxt = next_window_start([{"days": [0, 1, 2, 3, 4, 5, 6], "start": "01:00", "end": "07:00"}], mon_noon)
    assert nxt == dt.datetime(2026, 11, 3, 1, 0)
    assert next_window_start([], mon_noon) is None


def test_next_run_explains_itself():
    settings.save({"schedule": {"windows": []}})
    scheduler.STATUS["pause_reasons"] = []
    assert "anytime" in scheduler.next_run(settings.get())["why"]


def _two_disk_queue():
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 1, 'S', 'series')")
    a = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, disk, first_seen) VALUES (?, 1, '/tv/S/a.mkv', 'disk1', ?)",
             (iid, time.time()))
    b = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, disk, first_seen) VALUES (?, 2, '/tv/S/b.mkv', 'disk2', ?)",
             (iid, time.time()))
    scheduler.enqueue([a], "quick")
    time.sleep(0.01)
    scheduler.enqueue([b], "quick")
    return a, b


def test_same_disk_stream_only_skips_that_disk(monkeypatch):
    a, b = _two_disk_queue()
    # Plex sees the file under its own path; matched by file name
    monkeypatch.setattr(arr.Tautulli, "configured", property(lambda self: True))
    monkeypatch.setattr(arr.Tautulli, "sessions", lambda self: [{"file": "/data/media/tv/S/a.mkv", "title": "S", "state": "playing"}])
    scheduler._streams_cache["t"] = 0
    scanned = []
    monkeypatch.setattr(scanner, "scan_file", lambda fid, p, s: scanned.append(fid) or db.x("DELETE FROM queue WHERE file_id=?", (fid,)))
    w = scheduler.Worker()
    w._last_poll = w._last_recheck = time.time()
    w.tick()
    assert scanned == [b]                      # disk2 file scanned while disk1 is streaming
    assert scheduler.STATUS["busy_disks"] == ["disk1"]
    w.tick()
    assert scanned == [b] and "Plex is reading disk1" in scheduler.STATUS["mode"]


def test_any_mode_keeps_old_behaviour(monkeypatch):
    settings.save({"schedule": {"stream_pause": "any"}})
    monkeypatch.setattr(arr.Tautulli, "configured", property(lambda self: True))
    monkeypatch.setattr(arr.Tautulli, "sessions", lambda self: [{"file": "/x/y.mkv", "title": "", "state": ""}])
    scheduler._streams_cache["t"] = 0
    assert "1 active Plex stream(s)" in scheduler.pause_reasons(settings.get())


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
