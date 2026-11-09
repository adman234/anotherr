"""Background worker: scan slots, disk choice, pauses, automation.

Scans run in slots: up to `max_disks` hard-drive slots (each on a different disk) plus one cache/SSD slot.
Each slot takes the cheapest work first:
  cache/SSD slot: queued files on cache, then scheduled files on cache
  hard-drive slot: queued files on a spinning disk, scheduled files on a spinning disk (sticking with the disk
                   it's on), queued files on an idle disk, then wakes the next idle disk in rotation
Nothing new starts during a manual pause, parity check, mover run or quiet window.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import actions, arr, db, frames, health, paths, scanner, settings, stt, sync, trackedit, unraid
from .schedule_util import active_window, next_window_start

log = logging.getLogger("anotherr.scheduler")

STATUS: dict = {"pause_reasons": [], "mode": "idle", "slots": [], "busy_disks": []}
FAST_SLOT = "cache"
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
    if sc.get("pause_on_mover", True) and unraid.mover_running():
        r.append("mover running")
    qw = sc.get("quiet_windows") or []
    hit = active_window(qw) if qw else None
    if hit:
        r.append(f"quiet window until {hit[0].get('end')}")
    if stream_mode(cfg) == "any":
        n = stream_count()
        if n > int(sc.get("max_streams", 0)):
            r.append(f"{n} active Plex stream(s)")
    from . import subgen

    if (cfg.get("subgen") or {}).get("gpu") and subgen.in_flight():
        r.append("subgen job using the GPU")
    if paths.mappings_look_wrong():
        r.append("files not found - fix path mappings in Settings")
    return r


# ------------------------------------------------------------------ worker

class Worker(threading.Thread):
    def __init__(self, inline: bool = False):
        super().__init__(daemon=True, name="anotherr-worker")
        self.stop = threading.Event()
        self.inline = inline  # tests: run scans synchronously instead of in slot threads
        self.active: dict[str, dict] = {}  # slot -> {"thread", "disk", "file_id"}
        self.last_disk: dict[str, str] = {}
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
            self.stop.wait(1 if worked else (2 if self.active else 10))

    # -------------------------------------------------------------- main loop
    def tick(self) -> bool:
        cfg = settings.get()
        sc = cfg["schedule"]
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
        self._reap()

        if scanner.CANCEL.is_set():  # "stop scan": wait for running scans to stop, then carry on
            if self.active:
                STATUS["mode"] = "stopping scans"
                return False
            scanner.CANCEL.clear()

        force = bool(settings.state_get("force_queue", False))
        if force and not queue_count():
            settings.state_set("force_queue", False)  # queue drained
            db.log("Queue processed")
            force = False
        busy = set() if force else busy_disks(cfg)
        STATUS["busy_disks"] = sorted(busy)
        queue_ok = force or not reasons
        sched_ok = not reasons and bool(sc.get("enabled"))
        if reasons and not force:
            STATUS["mode"] = "paused: " + ", ".join(reasons)
        started = 0
        if queue_ok or sched_ok:
            ctx = self._disk_context(cfg, busy)
            if sc.get("cache_slot", True) and FAST_SLOT not in self.active:
                started += self._fill(FAST_SLOT, cfg, ctx, queue_ok, sched_ok and not force, fast=True)
            hdd_slots = [f"hdd{i + 1}" for i in range(max(0, int(sc.get("max_disks", 1) or 0)))]
            for slot in hdd_slots:
                if slot not in self.active:
                    started += self._fill(slot, cfg, ctx, queue_ok, sched_ok and not force, fast=False)
        self._publish(reasons, force)
        return bool(started)

    def _reap(self):
        for slot, a in list(self.active.items()):
            if not a["thread"].is_alive():
                self.active.pop(slot, None)

    def _publish(self, reasons, force):
        live = {s["slot"]: s for s in scanner.current_scans()}
        STATUS["slots"] = [dict(live.get(slot, {}), slot=slot, disk=a["disk"], why=a["why"]) for slot, a in self.active.items()]
        if STATUS["slots"]:
            STATUS["mode"] = ("processing queue (forced) - " if force else "scanning: ") + ", ".join(
                f"{s['disk'] or 'disk?'} ({s['why']})" for s in STATUS["slots"])
        elif not reasons or force:
            STATUS["mode"] = "idle (nothing due)"

    # -------------------------------------------------------------- choosing work
    def _disk_context(self, cfg: dict, busy: set[str]) -> dict:
        state = unraid.disks_state()
        spun = unraid.spun_up_disks()  # None = unknown (not Unraid / emhttp not mounted)
        disks = all_disks()
        only = set(cfg["schedule"].get("disks") or [])
        fast = {d for d in disks if unraid.is_fast_disk(d, state)}
        return {"state": state, "spun": spun, "disks": disks, "only": only, "fast": fast, "busy": busy,
                "taken": {a["disk"] for a in self.active.values()}}

    def _queue_job(self, ctx: dict, disks: set[str] | None, exclude: set[str]) -> dict | None:
        """Next due queue job on one of `disks` (None = any disk not excluded)."""
        force = bool(settings.state_get("force_queue", False))
        rows = db.q("SELECT q.*, f.disk FROM queue q JOIN files f ON f.id = q.file_id WHERE f.missing=0 AND "
                    "q.created_at <= ? ORDER BY CASE q.source WHEN 'manual' THEN 0 WHEN 'import' THEN 1 ELSE 2 END, "
                    "q.created_at LIMIT 500", (float("inf") if force else time.time(),))
        for r in rows:
            if r["disk"] in exclude or r["file_id"] in {a["file_id"] for a in self.active.values()}:
                continue
            if disks is None or r["disk"] in disks:
                return r
        return None

    def _fill(self, slot: str, cfg: dict, ctx: dict, queue_ok: bool, sched_ok: bool, fast: bool) -> int:
        sc = cfg["schedule"]
        rescan = float(sc.get("rescan_after_days", 180))
        profile = sc.get("profile") or cfg["scan"]["default_profile"]
        taken = ctx["taken"] | ctx["busy"]
        pick = None  # (file_id, profile, source, why, queue_id, disk)
        if fast:
            pool = ctx["fast"] - taken
            if queue_ok and pool:
                j = self._queue_job(ctx, pool, taken)
                if j:
                    pick = (j["file_id"], j["profile"], j["source"], f"cache/SSD - {j['source']}", j["id"], j["disk"])
            if not pick and sched_ok:
                allowed = sorted((pool & ctx["only"]) if ctx["only"] else pool)
                got = next_files(allowed, rescan) if allowed else []
                if got:
                    pick = (got[0]["id"], profile, "scheduled", "cache/SSD", None, got[0]["disk"])
        else:
            hdds = set(ctx["disks"]) - ctx["fast"]
            spinning = hdds if ctx["spun"] is None else hdds & set(ctx["spun"])
            exclude = taken | ctx["fast"]
            sticky = self.last_disk.get(slot)
            allowed = (lambda ds: ds & ctx["only"]) if ctx["only"] else (lambda ds: ds)
            # 1. queued files on a disk that's already spinning
            if queue_ok:
                j = self._queue_job(ctx, spinning - exclude, exclude)
                if j:
                    pick = (j["file_id"], j["profile"], j["source"], f"already spinning - {j['source']}", j["id"], j["disk"])
            # 2. scheduled files on a spinning disk, staying on this slot's disk while it has work
            if not pick and sched_ok:
                cands = allowed(spinning - exclude)
                order = ([sticky] if sticky in cands else []) + sorted(cands - {sticky})
                for d in order:
                    got = next_files([d], rescan)
                    if got:
                        pick = (got[0]["id"], profile, "scheduled", "already spinning", None, d)
                        break
            # 3. queued files on an idle disk (you asked for them, so they may wake it)
            if not pick and queue_ok:
                j = self._queue_job(ctx, None, exclude)
                if j:
                    pick = (j["file_id"], j["profile"], j["source"], f"woke disk - {j['source']}", j["id"], j["disk"])
            # 4. wake the next idle disk in rotation
            if not pick and sched_ok and self._may_spin_up(sc):
                idle = sorted(allowed(hdds - spinning - exclude))
                if idle:
                    last = settings.state_get("rotation_last")
                    start = (idle.index(last) + 1) if last in idle else 0
                    for d in idle[start:] + idle[:start]:
                        got = next_files([d], rescan)
                        if got:
                            settings.state_set("rotation_last", d)
                            pick = (got[0]["id"], profile, "scheduled", "woke idle disk (rotation)", None, d)
                            break
        if not pick:
            return 0
        fid, prof, source, why, qid, disk = pick
        ctx["taken"].add(disk)
        self.last_disk[slot] = disk
        self._start(slot, fid, prof, source, why, qid, disk)
        return 1

    @staticmethod
    def _may_spin_up(sc: dict) -> bool:
        mode = sc.get("spinup", "anytime")
        if mode == "never":
            return False
        if mode == "windows":
            w = sc.get("windows") or []
            return bool(w) and active_window(w) is not None
        return True

    def _start(self, slot, fid, profile, source, why, qid, disk):
        def run():
            try:
                scanner.scan_file(fid, profile, source, slot=slot, why=why)
            except Exception:  # noqa: BLE001
                log.exception("scan failed in slot %s", slot)
            finally:
                if qid:
                    db.x("DELETE FROM queue WHERE id=?", (qid,))

        t = threading.Thread(target=run, daemon=True, name=f"anotherr-scan-{slot}")
        self.active[slot] = {"thread": t, "disk": disk, "file_id": fid, "why": why}
        if self.inline:
            t.run()
            self.active.pop(slot, None)
        else:
            t.start()

    def _periodic(self, cfg: dict):
        now = time.time()
        if now - getattr(self, "_last_beat", 0) > 60:
            self._last_beat = now
            try:
                trackedit.reap()
            except Exception:  # noqa: BLE001
                log.exception("action reaper failed")
            health.beat({"scans": [s.get("disk") for s in STATUS.get("slots", [])]})
        if now - getattr(self, "_last_prune", 0) > 3600:
            self._last_prune = now
            try:
                frames.prune()
            except Exception:  # noqa: BLE001
                log.exception("frame cache prune failed")
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
    from . import langs
    from .scanner import file_sidecars

    sub_lang = langs.norm(cfg["languages"]["subtitle_language"]) or "eng"
    rows = db.q("""SELECT DISTINCT f.id, f.local_path, f.arr_path, MAX(x.created_at) AS since FROM issues x JOIN files f ON f.id = x.file_id
                   WHERE x.status='open' AND x.code IN ('subs_missing','subs_signs_only','subs_maybe_signs_only')
                   AND f.missing=0 AND f.id NOT IN (SELECT file_id FROM queue) GROUP BY f.id LIMIT 500""")
    ids = []
    for r in rows:
        try:
            new = [sd for sd in file_sidecars(r, r["local_path"] or "") if langs.same(sd["lang"], sub_lang)
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
    due = queue_count(due_only=True)
    if reasons:
        return {"at": None, "why": "paused: " + ", ".join(reasons)}
    if due:
        return {"at": time.time(), "why": f"{due} queued file(s)"}
    if not sc.get("enabled"):
        return {"at": None, "why": "scheduled scanning is turned off; only queued scans run"}
    spin = sc.get("spinup", "anytime")
    w = sc.get("windows") or []
    if spin == "windows" and not (w and active_window(w)):
        nxt = next_window_start(w)
        return {"at": time.time(), "why": "scanning cache and spinning disks; idle disks wake "
                + (f"from {nxt:%a %H:%M}" if nxt else "never (no spin-up window set)")}
    return {"at": time.time(), "why": "around the clock: cache first, then spinning disks, then wakes one idle disk"
            + ("" if spin != "never" else " (never wakes idle disks)")}


WORKER: Worker | None = None


def start():
    global WORKER
    if WORKER is None:
        WORKER = Worker()
        WORKER.start()


def enqueue(file_ids: list[int], profile: str, source: str = "manual") -> int:
    """Queue files for a scan. Files no longer in the library (missing=1) are skipped: the worker never picks
    them, so their rows would sit in the queue forever and keep it from ever looking empty."""
    now = time.time()
    n = 0
    for fid in file_ids:
        n += db.n("INSERT INTO queue(file_id, profile, source, created_at) SELECT id, ?, ?, ? FROM files "
                  "WHERE id=? AND missing=0 "
                  "ON CONFLICT(file_id) DO UPDATE SET profile=excluded.profile, source=excluded.source",
                  (profile, source, now, fid)) and 1
    return n


def queue_count(due_only: bool = False) -> int:
    """Queued files the worker can actually scan (rows for files that left the library don't count)."""
    sql = "SELECT COUNT(*) FROM queue q JOIN files f ON f.id = q.file_id WHERE f.missing=0"
    if due_only:
        return db.scalar(sql + " AND q.created_at <= ?", (time.time(),)) or 0
    return db.scalar(sql) or 0
