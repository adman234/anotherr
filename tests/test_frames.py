import json
import os
import threading
import time

from anotherr import frames, health, media


def test_live_extractions_are_capped_and_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(frames, "CACHE_DIR", str(tmp_path))
    live, peak, lock = [0], [0], threading.Lock()

    def slow(path, t, w, threads=2):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return b"jpeg"

    monkeypatch.setattr(media, "extract_frame", slow)
    f = {"id": 7, "size": 123}
    ts = [threading.Thread(target=frames.extract, args=(f, "x", t)) for t in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= frames.MAX_LIVE                       # never more than 2 ffmpegs at once
    assert frames.cached(f, 5) and frames.cached(f, 5, 480)  # small sizes share the standard frame
    calls = []
    monkeypatch.setattr(media, "extract_frame", lambda *a, **k: calls.append(1) or b"x")
    assert frames.extract(f, "x", 5) == b"jpeg" and calls == []  # served from cache
    frames.drop_file(7)
    assert not frames.cached(f, 5)


def test_prune_keeps_cache_under_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(frames, "CACHE_DIR", str(tmp_path))
    d = tmp_path / "1-1"
    d.mkdir()
    for i in range(10):
        p = d / f"{i}.jpg"
        p.write_bytes(b"x" * 1000)
        os.utime(p, (i, i))
    assert frames.prune(cap=5000) > 0
    left = sorted(p.name for p in d.iterdir())
    assert sum(1000 for _ in left) <= 4000 and "9.jpg" in left and "0.jpg" not in left


def test_unclean_shutdown_is_reported(monkeypatch, tmp_path):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(health, "HEARTBEAT", str(hb))
    monkeypatch.setattr(health, "oom_kills", lambda: 3)
    hb.write_text(json.dumps({"ts": time.time(), "clean": False, "anotherr_mb": 3000, "ffmpeg": 29,
                              "children_mb": 11800, "oom_kills": 2}))
    msg = health.check_previous_run()
    assert "out-of-memory" in msg and "29 ffmpeg" in msg
    health.beat()
    health.mark_clean_shutdown()
    assert health.check_previous_run() is None
