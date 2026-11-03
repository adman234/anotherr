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
    monkeypatch.setattr(scanner, "scan_file", lambda fid, p, s, **kw: scanned.append(fid) or db.x("DELETE FROM queue WHERE file_id=?", (fid,)))
    monkeypatch.setattr(scheduler, "pause_reasons", lambda cfg: ["parity check running"])
    with client() as c:
        c.post("/api/scan", json={"scope": "item", "ids": [iid], "profile": "quick"})
        w = scheduler.Worker(inline=True)
        w._last_poll = w._last_recheck = time.time()
        w.tick()
        assert scanned == [] and scheduler.STATUS["mode"].startswith("paused")
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


def test_missing_file_resyncs_and_parks_reviews(monkeypatch):
    from anotherr import scanner

    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 42, 'Death Note', 'series')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, first_seen) VALUES (?, 1, '/tv/DN/10.mkv', "
               "'/nope/10.mkv', ?)", (iid, time.time()))
    db.x("INSERT INTO reviews(file_id, kind, status, created_at) VALUES (?, 'title_cards', 'pending', ?)", (fid, time.time()))
    calls = []
    monkeypatch.setattr(sync, "sync_one", lambda s, i, queue=True: calls.append((s, i)) or 0)
    scanner._resync_at.clear()
    with client() as c:
        assert c.get(f"/api/files/{fid}/frame?t=10").status_code == 404
        import threading
        for t in threading.enumerate():
            if t.name == "anotherr-resync":
                t.join(10)
        assert calls == [("sonarr", 42)]
        assert c.get("/api/status").json()["files_not_found"] == 1
        assert db.q1("SELECT status FROM reviews WHERE file_id=?", (fid,))["status"] == "dismissed"


def test_streams_and_issue_evidence_endpoints(tmp_path):
    import subprocess

    v = str(tmp_path / "v.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-metadata:s:a:0", "language=jpn", "-c:v", "libx264", "-c:a", "aac", v],
                   check=True)
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('radarr', 7, 'Ono', 'movie')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, first_seen) VALUES (?, 1, ?, ?, ?)",
               (iid, v, v, time.time()))
    sid = db.x("INSERT INTO scans(file_id, status, result) VALUES (?, 'done', ?)",
               (fid, db.j({"audio_tracks": [{"a_index": 0, "windows": [{"lang": "eng", "prob": 0.9}]}]})))
    xid = db.x("INSERT INTO issues(file_id, scan_id, category, code, severity, message, details, status) VALUES "
               "(?, ?, 'language', 'original_audio_missing', 'error', 'No Russian audio', ?, 'open')",
               (fid, sid, db.j({"evidence_ts": [30.0]})))
    with client() as c:
        s = c.get(f"/api/files/{fid}/streams").json()
        assert s["audio"][0]["lang"] == "jpn" and s["video"]
        e = c.get(f"/api/issues/{xid}").json()
        assert e["evidence"]["audio_tracks"][0]["windows"] and e["evidence_ts"] == [30.0]


def test_issue_code_summary_covers_every_open_issue():
    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 3, 'S', 'series')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, 1, '/x', ?)", (iid, time.time()))
    for n in range(20):  # 20 distinct codes - more than the old 15-code cut-off
        db.x("INSERT INTO issues(file_id, code, severity, status) VALUES (?, ?, 'info', 'open')", (fid, f"code_{n}"))
    with client() as c:
        r = c.get("/api/issues?per_page=5").json()
        assert sum(x["n"] for x in r["codes"]) == r["total"] == 20


def test_files_with_revised_issue_codes_are_requeued_once():
    from anotherr import settings

    iid = db.x("INSERT INTO items(source, arr_id, title, kind) VALUES ('sonarr', 9, 'NCIS', 'series')")
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, first_seen) VALUES (?, 1, '/x', ?)", (iid, time.time()))
    db.x("INSERT INTO issues(file_id, code, severity, status) VALUES (?, 'frozen_video', 'warning', 'open')", (fid,))
    settings.state_set("detector_fixes_requeued", False)
    api._requeue_after_detector_fixes()
    assert db.q1("SELECT source FROM queue WHERE file_id=?", (fid,))["source"] == "recheck"
    db.x("DELETE FROM queue")
    api._requeue_after_detector_fixes()
    assert db.scalar("SELECT COUNT(*) FROM queue") == 0


def test_retired_settings_are_dropped():
    from anotherr import settings

    db.x("INSERT INTO settings(key, value) VALUES('config', ?)",
         (db.j({"_v": 2, "schedule": {"disk_mode": "rotate", "max_files_per_window": 150, "max_disks": 2},
                "scan": {"profiles": {"standard": {"seconds": 60}}}}),))
    settings._cache = None
    s = settings.get()
    assert "disk_mode" not in s["schedule"] and "max_files_per_window" not in s["schedule"]
    assert s["schedule"]["max_disks"] == 2 and s["scan"]["profiles"]["standard"]["seconds"] == 60
