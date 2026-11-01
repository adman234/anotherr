import time

from fastapi.testclient import TestClient

from anotherr import api, db, sync


def client():
    return TestClient(api.app)


def test_status_settings_roundtrip():
    with client() as c:
        assert c.get("/api/status").status_code == 200
        r = c.put("/api/settings", json={"sonarr": {"url": "http://s:8989", "api_key": "secret"}})
        assert r.json()["settings"]["sonarr"]["api_key"] == "********"
        # sending the mask back must keep the real key
        c.put("/api/settings", json={"sonarr": {"url": "http://s:8989", "api_key": "********"}})
        from anotherr import settings
        assert settings.get()["sonarr"]["api_key"] == "secret"
        assert settings.get()["automation"]["enabled"] is False
        assert c.get("/").status_code == 200
        assert c.get("/mcp/anotherr_mcp.py").status_code == 200


def test_scan_queue_and_webhook(monkeypatch):
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 7, 'Show', 'series')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, 70, '/tv/a.mkv', ?)", (iid, time.time()))
    with client() as c:
        r = c.post("/api/scan", json={"scope": "item", "ids": [iid], "profile": "quick"})
        assert r.json()["queued"] == 1
        assert len(c.get("/api/queue").json()) == 1
        c.delete("/api/queue")
        monkeypatch.setattr(sync, "sync_one", lambda s, i: None)
        r = c.post("/api/webhook/sonarr", json={"eventType": "Download", "series": {"id": 7}, "episodeFile": {"id": 70}})
        assert r.json()["queued"] == 1
        q = c.get("/api/queue").json()
        assert q[0]["source"] == "import" and q[0]["file_id"] == fid


def test_sync_with_fake_sonarr(monkeypatch):
    from anotherr import arr, settings

    settings.save({"sonarr": {"url": "http://x", "api_key": "k"}, "path_mappings": [{"from": "/tv", "to": "/data/tv"}]})
    series = [{"id": 1, "title": "SpongeBob", "seriesType": "standard", "runtime": 11, "originalLanguage": {"name": "English"},
               "statistics": {"episodeFileCount": 1}, "path": "/tv/SpongeBob"}]
    eps = [{"id": 100, "seasonNumber": 1, "episodeNumber": 1, "title": "Help Wanted", "episodeFileId": 9},
           {"id": 101, "seasonNumber": 1, "episodeNumber": 2, "title": "Reef Blower", "episodeFileId": 9}]
    files = [{"id": 9, "path": "/tv/SpongeBob/S01E01-E02.mkv", "size": 123, "releaseGroup": "GRP",
              "quality": {"quality": {"name": "WEBDL-1080p"}}}]
    monkeypatch.setattr(arr.Sonarr, "series", lambda self, i=None: series)
    monkeypatch.setattr(arr.Sonarr, "episodes", lambda self, i: eps)
    monkeypatch.setattr(arr.Sonarr, "episode_files", lambda self, i: files)
    res = sync.full_sync()
    assert res["ok"] and res["episode_files"] == 1
    f = db.q1("SELECT * FROM files")
    assert f["label"] == "S01E01-E02" and f["local_path"] == "/data/tv/SpongeBob/S01E01-E02.mkv"
    assert db.q1("SELECT original_language FROM items")["original_language"] == "eng"


def test_process_queue_ignores_pauses(monkeypatch):
    from anotherr import scanner, scheduler, settings

    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 8, 'Show', 'series')")
    fids = [db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, ?, '/tv/x.mkv', ?)",
                 (iid, n, time.time())) for n in (1, 2)]
    scanned = []
    monkeypatch.setattr(scanner, "scan_file", lambda fid, p, s: scanned.append(fid) or db.x("DELETE FROM queue WHERE file_id=?", (fid,)))
    monkeypatch.setattr(scheduler, "pause_reasons", lambda cfg: ["1 active Plex stream(s)"])
    with client() as c:
        c.post("/api/scan", json={"scope": "item", "ids": [iid], "profile": "quick"})
        w = scheduler.Worker()
        w.tick()
        assert scanned == [] and scheduler.STATUS["mode"].startswith("queue waiting")
        c.post("/api/queue/process")
        w.tick(); w.tick(); w.tick()
        assert sorted(scanned) == sorted(fids)
        assert settings.state_get("force_queue") is False  # resets once drained


def test_settings_store_only_changes_and_migrate():
    from anotherr import settings

    # simulate an old install that saved every default, including the old standard profile
    old = settings._merge(settings.DEFAULTS, {"scan": {"ffmpeg_threads": 2, "profiles": {"standard": {"seconds": 120}}},
                                              "path_mappings": [{"from": "/data", "to": "/mnt/user"}]})
    db.x("INSERT INTO settings(key, value) VALUES('config', ?)", (db.j(old),))
    settings._cache = None
    s = settings.get()
    assert s["scan"]["profiles"]["standard"]["seconds"] == 90 and s["scan"]["ffmpeg_threads"] == 0
    assert s["path_mappings"] == [{"from": "/data", "to": "/mnt/user"}]
    settings.save({"schedule": {"max_streams": 2}})
    stored = db.unj(db.scalar("SELECT value FROM settings WHERE key='config'"))
    assert stored["schedule"] == {"max_streams": 2} and "whisper" not in stored
