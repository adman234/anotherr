"""Leftover .anotherr-edit.* temp files: an arr that adopts one must not replace the real file's row, and a
subtitle-shift remux cut off by a restart must be swept like a retag copy."""
import json
import time

from anotherr import db, subshift, sync, trackedit

MOVIE = {"id": 5, "title": "Film", "year": 1988, "runtime": 116, "path": "/movies/Film (1988)",
         "originalLanguage": {"name": "English"}}
REAL = "/movies/Film (1988)/Film (1988).mkv"
TEMP = "/movies/Film (1988)/.anotherr-edit.Film (1988).mkv"


def _sync(path, file_id):
    sync.sync_movie(dict(MOVIE, movieFile={"id": file_id, "path": path, "size": 100}), profiles={})


def test_is_temp():
    assert trackedit.is_temp(TEMP)
    assert not trackedit.is_temp(REAL)
    assert not trackedit.is_temp(None)


def test_movie_sync_ignores_a_temp_file(monkeypatch):
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    _sync(REAL, 1)
    _sync(TEMP, 2)  # Radarr rescanned while the temp existed and assigned it
    rows = db.q("SELECT arr_file_id, arr_path, missing FROM files")
    assert [(r["arr_file_id"], r["arr_path"], r["missing"]) for r in rows] == [(1, REAL, 0)]


def test_series_sync_ignores_a_temp_file(monkeypatch):
    monkeypatch.setattr(db, "log", lambda *a, **k: None)

    class S:
        def episodes(self, sid):
            return [{"id": 10, "episodeFileId": 1, "seasonNumber": 1, "episodeNumber": 1},
                    {"id": 11, "episodeFileId": 3, "seasonNumber": 1, "episodeNumber": 2}]

        def episode_files(self, sid):
            return files

    files = [{"id": 1, "path": "/tv/S/e1.mkv", "size": 1}, {"id": 2, "path": "/tv/S/e2.mkv", "size": 1}]
    series = {"id": 7, "title": "S", "path": "/tv/S", "statistics": {"episodeFileCount": 2}}
    sync.sync_series(S(), series, profiles={})
    files = [{"id": 1, "path": "/tv/S/e1.mkv", "size": 1}, {"id": 3, "path": "/tv/S/.anotherr-edit.e2.mkv", "size": 1}]
    sync.sync_series(S(), series, profiles={})
    rows = db.q("SELECT arr_path, missing FROM files ORDER BY arr_file_id")
    assert [(r["arr_path"], r["missing"]) for r in rows] == [("/tv/S/e1.mkv", 0), ("/tv/S/e2.mkv", 0)]


def test_sweep_removes_a_logged_remux_temp(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(trackedit, "LOG_PATH", str(log))
    monkeypatch.setattr(db, "log", lambda *a, **k: None)
    video = str(tmp_path / "f.mkv")
    trackedit.log_temp(video)
    open(trackedit.tmp_path(video), "w").write("partial")
    assert json.loads(log.read_text())["event"] == "temp"
    assert trackedit.sweep_temp() == 1
    assert not (tmp_path / ".anotherr-edit.f.mkv").exists()


def test_long_remux_is_not_reaped_as_stalled():
    assert trackedit.SLOW_KINDS["shift_subtitle"] > subshift.REMUX_TIMEOUT
