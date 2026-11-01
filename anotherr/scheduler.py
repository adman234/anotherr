"""Background worker: manual/import queue, scheduled scans in time windows with disk rotation, pauses, automation."""
from __future__ import annotations

import logging
import threading
import time

from . import actions, arr, db, scanner, settings, stt, sync, unraid
from .schedule_util import active_window

log = logging.getLogger("anotherr.scheduler")

STATUS: dict = {"pause_reasons": [], "mode": "idle", "window": None, "disk": None, "window_count": 0}
_streams_cache = {"t": 0.0, "n": 0, "err": ""}


# ------------------------------------------------------------------ priority

def next_files(disks: list[str] | None, rescan_days: float, limit: int = 1) -> list[dict]:
    """Highest-priority files that are due for a scan.

    score = never scanned (1000) or days since last scan
          + recently added and unscanned (300)
          + release-group failure rate * 200
          + had problems last time (100)
    """
    now = time.time()
    args: list = [now, now - 7 * 86400, now - rescan_days * 86400]
    disk_sql = ""
    if disks is not None:
        if not disks:
            return []
        disk_sql = f" AND f.disk IN ({','.join('?' * len(disks))})"
        args += disks
    args.append(limit)
    return db.q(f"""
        SELECT f.*, i.title,
          (CASE WHEN f.last_scan_at IS NULL THEN 1000 ELSE (?1 - f.last_scan_at) / 86400.0 END)
          + (CASE WHEN f.last_scan_at IS NULL AND f.first_seen > ?2 THEN 300 ELSE 0 END)
          + COALESCE(g.badrate, 0) * 200
          + (CASE WHEN f.status IN ('warning','error','critical','failed') THEN 100 ELSE 0 END) AS score
        FROM files f JOIN items i ON i.id = f.item_id
        LEFT JOIN (SELECT release_group,
                     AVG(CASE WHEN status IN ('error','critical') THEN 1.0 ELSE 0.0 END) AS badrate
                   FROM files WHERE last_scan_at IS NOT NULL AND release_group != ''
                   GROUP BY release_group HAVING COUNT(*) >= 3) g ON g.release_group = f.release_group
        WHERE f.missing = 0 AND i.excluded = 0
          AND (f.last_scan_at IS NULL OR f.last_scan_at < ?3)
          AND f.id NOT IN (SELECT file_id FROM queue)
          {disk_sql}
        ORDER BY score DESC LIMIT ?""", args)


def all_disks() -> list[str]:
    return [r["disk"] for r in db.q("SELECT DISTINCT disk FROM files WHERE missing=0 ORDER BY disk")]


def candidate_disks(cfg: dict) -> list[str] | None:
    """None = no disk restriction."""
    sc = cfg["schedule"]
    mode = sc.get("disk_mode", "all")
    known = all_disks()
    if mode == "all":
        return None
    if mode == "specific":
        return [d for d in sc.get("disks", []) if d in known]
    if mode == "spun_up":
        up = unraid.spun_up_disks()
        return None if up is None else [d for d in known if d in up or d == ""]
    chosen = [d for d in (sc.get("disks") or known) if d in known]  # rotate
    return chosen


# ------------------------------------------------------------------ pauses

def stream_count() -> int:
    if time.time() - _streams_cache["t"] < 60:
        return _streams_cache["n"]
    t = arr.Tautulli()
    n = 0
    if t.configured:
        try:
            n = t.stream_count()
            _streams_cache["err"] = ""
        except arr.ServiceError as e:
            _streams_cache["err"] = str(e)
    _streams_cache.update(t=time.time(), n=n)
    return n


def pause_reasons(cfg: dict) -> list[str]:
    r = []
    if settings.state_get("paused", False):
        r.append("paused manually")
    sc = cfg["schedule"]
    if sc.get("pause_on_parity") and unraid.parity_running():
        r.append("parity check running")
    if sc.get("pause_on_streams"):
        n = stream_count()
        if n > int(sc.get("max_streams", 0)):
            r.append(f"{n} active Plex stream(s)")
    return r


# ------------------------------------------------------------------ worker

class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="anotherr-worker")
        self.stop = threading.Event()
        self._last_auto = 0.0

    def run(self):
        time.sleep(3)
        while not self.stop.is_set():
            worked = False
            try:
                worked = self.tick()
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")
            self.stop.wait(1 if worked else 10)

    def tick(self) -> bool:
        cfg = settings.get()
        self._maybe_sync(cfg)
        stt.WHISPER.maybe_unload_idle()
        reasons = pause_reasons(cfg)
        STATUS["pause_reasons"] = reasons
        if time.time() - self._last_auto > 60:
            self._last_auto = time.time()
            try:
                actions.maybe_auto()
            except Exception:  # noqa: BLE001
                log.exception("automation failed")

        job = db.q1("SELECT * FROM queue WHERE created_at <= ? ORDER BY CASE source WHEN 'manual' THEN 0 "
                    "WHEN 'import' THEN 1 ELSE 2 END, created_at LIMIT 1", (time.time(),))
        if job:
            manual_paused = "paused manually" in reasons
            if manual_paused or (reasons and cfg["schedule"].get("manual_respects_pause", True)):
                STATUS["mode"] = "paused"
                return False
            STATUS["mode"] = f"queue ({job['source']})"
            f = db.q1("SELECT id FROM files WHERE id=? AND missing=0", (job["file_id"],))
            if not f:
                db.x("DELETE FROM queue WHERE id=?", (job["id"],))
                return True
            scanner.scan_file(job["file_id"], job["profile"], job["source"])
            db.x("DELETE FROM queue WHERE id=?", (job["id"],))
            return True

        sc = cfg["schedule"]
        if not sc.get("enabled"):
            STATUS.update(mode="idle (schedule disabled)", window=None)
            return False
        win = active_window(sc.get("windows") or [])
        if not win:
            STATUS.update(mode="idle (outside scan window)", window=None)
            return False
        if reasons:
            STATUS["mode"] = "paused"
            return False
        w, key = win
        st = settings.state_get("window", {}) or {}
        if st.get("key") != key:
            st = {"key": key, "count": 0, "tried": [], "disk": self._first_disk(cfg)}
        STATUS.update(window=f"{w.get('start')}-{w.get('end')}", window_count=st["count"], disk=st.get("disk"))
        if st["count"] >= int(sc.get("max_files_per_window", 150)):
            STATUS["mode"] = "idle (window limit reached)"
            settings.state_set("window", st)
            return False
        f = self._pick(cfg, st)
        settings.state_set("window", st)
        if not f:
            STATUS["mode"] = "idle (nothing due)"
            return False
        STATUS["mode"] = "scheduled scan"
        scanner.scan_file(f["id"], sc.get("profile") or cfg["scan"]["default_profile"], "scheduled")
        st["count"] += 1
        settings.state_set("window", st)
        STATUS["window_count"] = st["count"]
        return True

    # -------------------------------------------------------------- helpers
    def _first_disk(self, cfg: dict) -> str | None:
        if cfg["schedule"].get("disk_mode") != "rotate":
            return None
        disks = candidate_disks(cfg) or []
        if not disks:
            return None
        last = settings.state_get("rotation_last")
        nxt = disks[(disks.index(last) + 1) % len(disks)] if last in disks else disks[0]
        settings.state_set("rotation_last", nxt)
        return nxt

    def _pick(self, cfg: dict, st: dict) -> dict | None:
        sc = cfg["schedule"]
        rescan = float(sc.get("rescan_after_days", 180))
        mode = sc.get("disk_mode", "all")
        if mode != "rotate":
            disks = candidate_disks(cfg)
            if mode == "spun_up" and disks and st.get("disk") in disks:
                got = next_files([st["disk"]], rescan)  # stay on the disk that's already spinning
                if got:
                    return got[0]
            got = next_files(disks, rescan)
            if got:
                st["disk"] = got[0]["disk"]
            return got[0] if got else None
        disks = candidate_disks(cfg) or []
        while st.get("disk") is not None:
            got = next_files([st["disk"]], rescan)
            if got:
                return got[0]
            st.setdefault("tried", []).append(st["disk"])
            remaining = [d for d in disks if d not in st["tried"]]
            if not remaining:
                st["disk"] = None
                break
            st["disk"] = remaining[0]
            settings.state_set("rotation_last", st["disk"])
        return None

    def _maybe_sync(self, cfg: dict):
        mins = float(cfg["sync"].get("interval_minutes", 360))
        last = sync.STATE.get("last") or settings.state_get("last_sync", 0) or 0
        if mins > 0 and not sync.STATE["running"] and time.time() - last > mins * 60:
            if arr.Sonarr().configured or arr.Radarr().configured:
                sync.STATE["last"] = time.time()  # avoid re-trigger while starting
                threading.Thread(target=sync.full_sync, daemon=True, name="anotherr-sync").start()


WORKER: Worker | None = None


def start():
    global WORKER
    if WORKER is None:
        WORKER = Worker()
        WORKER.start()


def enqueue(file_ids: list[int], profile: str, source: str = "manual") -> int:
    now = time.time()
    n = 0
    for fid in file_ids:
        n += db.x("INSERT INTO queue(file_id, profile, source, created_at) VALUES (?,?,?,?) "
                  "ON CONFLICT(file_id) DO UPDATE SET profile=excluded.profile, source=excluded.source",
                  (fid, profile, source, now)) and 1
    return n
