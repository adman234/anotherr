"""Background worker: manual/import queue, scheduled scans in time windows with disk rotation, pauses, automation."""
from __future__ import annotations

import logging
import os
import threading
import time

from . import actions, arr, db, paths, scanner, settings, stt, sync, unraid
from .schedule_util import active_window, next_window_start

log = logging.getLogger("anotherr.scheduler")

STATUS: dict = {"pause_reasons": [], "mode": "idle", "window": None, "disk": None, "window_count": 0}
_streams_cache = {"t": 0.0, "n": 0, "err": "", "sessions": [], "busy_disks": set()}


# ------------------------------------------------------------------ priority

def next_files(disks: list[str] | None, rescan_days: float, limit: int = 1, exclude: set[str] | None = None) -> list[dict]:
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
    if exclude:
        disk_sql += f" AND f.disk NOT IN ({','.join('?' * len(exclude))})"
        args += sorted(exclude)
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

def _disk_of(plex_path: str) -> str | None:
    """Which disk a Plex session is reading, by matching the file name against the library."""
    base = (plex_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not base:
        return None
    tail = "/" + base
    row = db.q1("SELECT disk FROM files WHERE missing=0 AND substr(arr_path, length(arr_path) - ? + 1) = ? LIMIT 1",
                (len(tail), tail))
    return row["disk"] if row else None


def stream_info() -> dict:
    """Active Plex streams (via Tautulli), cached for 60 s, with the disks they read from."""
    if time.time() - _streams_cache["t"] < 60:
        return _streams_cache
    t = arr.Tautulli()
    n, sessions, busy = 0, [], set()
    if t.configured:
        try:
            sessions = t.sessions()
            n = len(sessions)
            for s in sessions:
                s["disk"] = _disk_of(s["file"])
                if s["disk"] is not None:
                    busy.add(s["disk"])
            _streams_cache["err"] = ""
        except arr.ServiceError as e:
            _streams_cache["err"] = str(e)
    _streams_cache.update(t=time.time(), n=n, sessions=sessions, busy_disks=busy)
    return _streams_cache


def stream_count() -> int:
    return stream_info()["n"]


def stream_mode(cfg: dict) -> str:
    sc = cfg["schedule"]
    if not sc.get("pause_on_streams", True):
        return "off"
    return sc.get("stream_pause", "same_disk")


def busy_disks(cfg: dict) -> set[str]:
    """Disks to leave alone right now (same_disk mode)."""
    return set(stream_info()["busy_disks"]) if stream_mode(cfg) == "same_disk" else set()


def pause_reasons(cfg: dict) -> list[str]:
    r = []
    if settings.state_get("paused", False):
        r.append("paused manually")
    sc = cfg["schedule"]
    if sc.get("pause_on_parity") and unraid.parity_running():
        r.append("parity check running")
    if stream_mode(cfg) == "any":
        n = stream_count()
        if n > int(sc.get("max_streams", 0)):
            r.append(f"{n} active Plex stream(s)")
    if paths.mappings_look_wrong():
        r.append("files not found - fix path mappings in Settings")
    return r


# ------------------------------------------------------------------ worker

class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="anotherr-worker")
        self.stop = threading.Event()
        self._last_auto = 0.0
        self._last_poll = 0.0
        self._last_recheck = 0.0

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

        self._periodic(cfg)
        force = bool(settings.state_get("force_queue", False))
        busy = set() if force else busy_disks(cfg)
        STATUS["busy_disks"] = sorted(busy)
        jobs = db.q("SELECT q.*, f.disk FROM queue q JOIN files f ON f.id = q.file_id WHERE q.created_at <= ? "
                    "ORDER BY CASE q.source WHEN 'manual' THEN 0 WHEN 'import' THEN 1 ELSE 2 END, q.created_at LIMIT 200",
                    (float("inf") if force else time.time(),))
        job = next((j for j in jobs if j["disk"] not in busy), None)
        if force and not jobs:
            settings.state_set("force_queue", False)  # queue drained
            db.log("Queue processed")
        blocked = ("queue waiting: Plex is reading " + ", ".join(sorted(busy))) if jobs and not job else None
        if job:
            manual_paused = "paused manually" in reasons
            if not force and (manual_paused or (reasons and cfg["schedule"].get("manual_respects_pause", True))):
                STATUS["mode"] = "queue waiting: " + ", ".join(reasons)
                return False
            STATUS["mode"] = f"processing queue ({job['source']})" + (" - forced" if force else "")
            f = db.q1("SELECT id FROM files WHERE id=? AND missing=0", (job["file_id"],))
            if not f:
                db.x("DELETE FROM queue WHERE id=?", (job["id"],))
                return True
            scanner.scan_file(job["file_id"], job["profile"], job["source"])
            db.x("DELETE FROM queue WHERE id=?", (job["id"],))
            return True
        worked = self._scheduled(cfg, reasons, busy)
        if not worked and blocked:
            STATUS["mode"] = blocked
        return worked

    def _scheduled(self, cfg: dict, reasons: list[str], busy: set[str]) -> bool:
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
        STATUS.update(window="anytime" if w.get("anytime") else f"{w.get('start')}-{w.get('end')}",
                      window_count=st["count"], disk=st.get("disk"))
        cap = int(sc.get("max_files_per_window", 0) or 0)
        if cap and st["count"] >= cap:
            STATUS["mode"] = "idle (window limit reached)"
            settings.state_set("window", st)
            return False
        if busy and st.get("disk") in busy and sc.get("disk_mode") == "rotate":
            STATUS["mode"] = f"waiting: Plex is reading {st['disk']} (this window's disk)"
            return False
        f = self._pick(cfg, st, busy)
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

    def _pick(self, cfg: dict, st: dict, busy: set[str] | None = None) -> dict | None:
        sc = cfg["schedule"]
        rescan = float(sc.get("rescan_after_days", 180))
        mode = sc.get("disk_mode", "all")
        if mode != "rotate":
            disks = candidate_disks(cfg)
            if mode == "spun_up" and disks and st.get("disk") in disks and st.get("disk") not in (busy or ()):
                got = next_files([st["disk"]], rescan)  # stay on the disk that's already spinning
                if got:
                    return got[0]
            got = next_files(disks, rescan, exclude=busy)
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

    def _periodic(self, cfg: dict):
        now = time.time()
        if now - self._last_poll > 15 * 60 and cfg["imports"].get("enabled") and not sync.STATE["running"]:
            self._last_poll = now
            try:
                sync.poll_imports()
            except Exception:  # noqa: BLE001
                log.exception("import poll failed")
        if now - self._last_recheck > 60 * 60:
            self._last_recheck = now
            try:
                recheck_subs(cfg)
            except Exception:  # noqa: BLE001
                log.exception("subtitle recheck failed")

    def _maybe_sync(self, cfg: dict):
        mins = float(cfg["sync"].get("interval_minutes", 360))
        last = sync.STATE.get("last") or settings.state_get("last_sync", 0) or 0
        if mins > 0 and not sync.STATE["running"] and time.time() - last > mins * 60:
            if arr.Sonarr().configured or arr.Radarr().configured:
                sync.STATE["last"] = time.time()  # avoid re-trigger while starting
                threading.Thread(target=sync.full_sync, daemon=True, name="anotherr-sync").start()


def recheck_subs(cfg: dict) -> int:
    """Rescan files whose 'subs missing/signs only' issue may be stale (Bazarr has since added a subtitle file)."""
    from . import langs, media

    sub_lang = langs.norm(cfg["languages"]["subtitle_language"]) or "eng"
    rows = db.q("""SELECT DISTINCT f.id, f.local_path, MAX(x.created_at) AS since FROM issues x JOIN files f ON f.id = x.file_id
                   WHERE x.status='open' AND x.code IN ('subs_missing','subs_signs_only','subs_maybe_signs_only')
                   AND f.missing=0 AND f.id NOT IN (SELECT file_id FROM queue) GROUP BY f.id LIMIT 500""")
    ids = []
    for r in rows:
        try:
            new = [sd for sd in media.sidecar_subs(r["local_path"] or "") if langs.same(sd["lang"], sub_lang)
                   and os.path.getmtime(sd["path"]) > (r["since"] or 0)]
        except OSError:
            continue
        if new:
            ids.append(r["id"])
    if ids:
        enqueue(ids, "standard", "recheck")
        db.log(f"Subtitles appeared for {len(ids)} file(s) flagged as missing subs - rescanning")
    return len(ids)


def next_run(cfg: dict) -> dict:
    """When scanning will next happen and why not now."""
    sc = cfg["schedule"]
    reasons = STATUS.get("pause_reasons") or []
    due = db.scalar("SELECT COUNT(*) FROM queue WHERE created_at <= ?", (time.time(),)) or 0
    if reasons:
        return {"at": None, "why": "paused: " + ", ".join(reasons)}
    if due:
        return {"at": time.time(), "why": f"{due} queued file(s)"}
    if not sc.get("enabled"):
        return {"at": None, "why": "scheduled scanning is turned off; only queued scans run"}
    if active_window(sc.get("windows") or []):
        return {"at": time.time(), "why": "inside a scan window" if sc.get("windows") else "no window set - scans anytime"}
    nxt = next_window_start(sc.get("windows") or [])
    return {"at": nxt.timestamp() if nxt else None,
            "why": f"next scan window opens {nxt:%a %H:%M}" if nxt else "no upcoming window"}


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
