"""FastAPI app: REST API, webhooks, web UI, MCP shim download."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, actions, arr, audio_request, db, defaults, frames, health, media, paths, redact, retag, scanner, scheduler, settings, stt, subgen, sync, trackedit, unraid

log = logging.getLogger("anotherr.api")
API_KEY = os.environ.get("ANOTHERR_API_KEY", "")
HERE = os.path.dirname(__file__)
MCP_SCRIPT = next((p for p in (os.path.join(HERE, "mcp_shim.py"),
                                os.path.join(os.path.dirname(HERE), "mcp", "anotherr_mcp.py")) if os.path.exists(p)),
                  os.path.join(HERE, "mcp_shim.py"))



@asynccontextmanager
async def lifespan(_app):
    _startup()
    yield
    health.mark_clean_shutdown()


app = FastAPI(title="Anotherr", version=__version__, lifespan=lifespan)


def auth(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("x-api-key") or request.query_params.get("apikey")
    if key != API_KEY:
        raise HTTPException(401, "invalid or missing API key")


def _thread_crash(args):
    """Any background thread that dies unexpectedly says so in the activity log (not just stderr)."""
    if args.exc_type is SystemExit:
        return
    log.error("thread %s crashed", getattr(args.thread, "name", "?"), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    try:
        db.log(f"Background task {getattr(args.thread, 'name', '?')} crashed: {args.exc_type.__name__}: {args.exc_value}", "error")
    except Exception:  # noqa: BLE001
        pass


def _startup():
    redact.install()
    threading.excepthook = _thread_crash
    db.init()
    note = health.check_previous_run()
    if note:
        db.log(note, "warning")
    health.beat()
    settings.get()  # load config so the log redactor knows the configured keys
    _scrub_stored_secrets()
    _requeue_after_detector_fixes()
    n = actions.reconcile()
    if n:
        db.log(f"Withdrew {n} proposed action(s) whose issues are no longer open")
    n = actions.upgrade_resubs_to_whisper()
    if n:
        db.log(f"Replaced {n} resub proposal(s) with Whisper subtitle proposals (Bazarr found nothing for those series before)")
    def _refresh_track_proposals():
        trackedit.sweep_temp()
        retag.refresh_all()
        defaults.refresh_all()
        subgen.refresh_all()

    threading.Thread(target=_refresh_track_proposals, daemon=True, name="anotherr-track-proposals").start()
    db.x("UPDATE scans SET status='failed', error='interrupted by restart' WHERE status='running'")
    # subgen batches survive a restart: the worker reconciles them with subgen's /queue
    db.x("UPDATE actions SET status='failed', result='interrupted by restart' WHERE status='running' AND kind!=?",
         (subgen.KIND,))
    db.x("UPDATE actions SET status='proposed', result='' WHERE status='queued' AND kind!=?", (subgen.KIND,))
    if os.environ.get("ANOTHERR_NO_WORKER") != "1":
        scheduler.start()
        if db.scalar("SELECT COUNT(*) FROM actions WHERE kind=? AND status IN ('queued','running')", (subgen.KIND,)):
            subgen.kick()
    db.log(f"Anotherr {__version__} started")


# Codes produced by detectors that changed in the Nov 2026 detector fixes - rescan once so stale results clear
_REVISED_CODES = ["frozen_video", "duration_short", "duration_short_warn", "possible_multi_episode",
                  "multi_episode_too_short", "segment_break_not_found", "subs_mismatch", "subs_mismatch_partial",
                  "subs_text_mismatch", "subs_out_of_sync", "subs_signs_only", "default_subs_signs", "dubtitles",
                  "audio_mislabeled", "original_audio_missing", "decode_errors_severe"]


def _requeue_after_detector_fixes():
    if settings.state_get("detector_fixes_requeued", False):
        return
    ids = [r["file_id"] for r in db.q(
        f"SELECT DISTINCT x.file_id FROM issues x JOIN files f ON f.id = x.file_id WHERE x.status='open' AND f.missing=0 "
        f"AND x.code IN ({','.join('?' * len(_REVISED_CODES))})", _REVISED_CODES)]
    if ids:
        scheduler.enqueue(ids, "standard", "recheck")
        db.log(f"Detectors were updated: queued {len(ids)} file(s) with affected issues for a rescan")
    settings.state_set("detector_fixes_requeued", True)


def _scrub_stored_secrets():
    """One-off clean-up of secrets that older versions may have written into the activity feed etc."""
    n = 0
    for table, col in (("activity", "message"), ("scans", "error"), ("actions", "result")):
        for row in db.q(f"SELECT id, {col} AS v FROM {table} WHERE {col} != ''"):
            clean = redact.redact(row["v"])
            if clean != row["v"]:
                db.x(f"UPDATE {table} SET {col}=? WHERE id=?", (clean, row["id"]))
                n += 1
    if n:
        log.info("Removed secrets from %d stored log/error entries", n)


def page(sql_base: str, args: list, page_no: int, per: int, order: str) -> dict:
    total = db.scalar(f"SELECT COUNT(*) FROM ({sql_base})", args)
    rows = db.q(f"{sql_base} ORDER BY {order} LIMIT ? OFFSET ?", args + [per, (page_no - 1) * per])
    return {"total": total, "page": page_no, "per_page": per, "rows": rows}


# ====================================================================== scan time

# timing group (prefix) -> what controls it, for the "where scan time goes" table
STEP_CONTROLS = [
    ("decoding", "Scan profile: segments x seconds (and GPU decode)"),
    ("video checks", "Scan profile: segments"),
    ("interlace check", "scan.checks.interlace"),
    ("re-checking a segment in software", "automatic: a GPU decode stopped early"),
    ("full decode", "Scan profile: full_decode (deep only by default)"),
    ("confirming possible freeze", "automatic: only when a freeze is suspected"),
    ("reading audio", "Scan profile: segments x seconds; scan.max_audio_tracks"),
    ("speech detection", "scan.max_audio_tracks"),
    ("language detection", "scan.max_audio_tracks (Whisper on the GPU)"),
    ("extra language samples", "scan.checks.extra_language_samples"),
    ("checking for burned-in subtitles", "scan.checks.hardsubs"),
    ("reading subtitles", "Scan profile: subtitles"),
    ("reading image subtitle timing", "Scan profile: subtitles"),
    ("subtitle sync", "scan.checks.subtitle_sync"),
    ("transcribing for subtitle content check", "scan.checks.subtitle_content"),
    ("subtitle mismatch recheck", "scan.subtitle_mismatch_windows"),
    ("finding segment break", "automatic: suspected multi-episode files"),
    ("reading title cards", "review.ocr_title_cards"),
    ("saving review frames", "review.title_cards"),
    ("probing", "always (ffprobe)"),
    ("locating file", "always (disk spin-up shows here)"),
]


def _step_group(name: str) -> str:
    import re

    return re.sub(r"\s*\d+(/\d+)?$", "", name).strip()


@app.get("/api/stats/timings", dependencies=[Depends(auth)])
def scan_timings(days: float = 7, profile: str = ""):
    """Where scan time goes: per-step totals over finished scans, with the setting that controls each step."""
    sql = ("SELECT profile, json_extract(result, '$.timings') t, json_extract(result, '$.elapsed_s') e, "
           "finished_at - started_at d FROM scans WHERE status='done' AND finished_at > ?")
    args: list = [time.time() - days * 86400]
    if profile:
        sql += " AND profile=?"
        args.append(profile)
    rows = db.q(sql + " ORDER BY id DESC LIMIT 5000", args)
    steps: dict[str, dict] = {}
    profiles: dict[str, dict] = {}
    total = 0.0
    for r in rows:
        el = float(r["e"] or r["d"] or 0)
        p = profiles.setdefault(r["profile"] or "?", {"scans": 0, "seconds": 0.0})
        p["scans"] += 1
        p["seconds"] += el
        total += el
        for k, v in (db.unj(r["t"], {}) or {}).items():
            g = steps.setdefault(_step_group(k), {"seconds": 0.0, "scans": 0, "max": 0.0})
            g["seconds"] += v
            g["scans"] += 1
            g["max"] = max(g["max"], v)
    timed = sum(g["seconds"] for g in steps.values()) or 1
    out = []
    for name, g in sorted(steps.items(), key=lambda kv: -kv[1]["seconds"]):
        ctl = next((c for pre, c in STEP_CONTROLS if name.startswith(pre)), "")
        out.append({"step": name, "seconds": round(g["seconds"], 1), "share": round(100 * g["seconds"] / timed, 1),
                    "scans": g["scans"], "avg": round(g["seconds"] / g["scans"], 1), "max": round(g["max"], 1),
                    "control": ctl})
    return {"days": days, "scans": len(rows), "seconds": round(total, 1),
            "avg_per_scan": round(total / len(rows), 1) if rows else None,
            "profiles": {k: {"scans": v["scans"], "avg": round(v["seconds"] / v["scans"], 1)} for k, v in profiles.items()},
            "steps": out}


# ====================================================================== status

@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM files WHERE missing=0 GROUP BY status")}
    cfg = settings.get()
    # every badge counts exactly the rows its tab lists (same joins and filters as the list queries)
    proposed = db.q("SELECT a.kind, a.codes, a.file_id, a.payload FROM actions a JOIN files f ON f.id=a.file_id "
                    "JOIN items i ON i.id=f.item_id WHERE a.status='proposed'")
    n_dual = sum(actions.is_dual({**r, "codes": db.unj(r["codes"], [])}) for r in proposed)
    return {
        "version": __version__,
        "build": os.environ.get("ANOTHERR_BUILD", "dev"),
        "mcp_bridge_version": MCP_BRIDGE_VERSION,
        "current": (scanner.current_scans() or [{}])[0],  # first running scan (older clients)
        "scans": scanner.current_scans(),
        "scheduler": {**scheduler.STATUS, "next_run": scheduler.next_run(cfg),
                      "windows": cfg["schedule"].get("windows") or [], "stream_pause": scheduler.stream_mode(cfg),
                      "streams": [{k: s.get(k) for k in ("title", "disk", "state")}
                                  for s in scheduler._streams_cache.get("sessions", [])]},
        "paused": bool(settings.state_get("paused", False)),
        "sync": {**sync.STATE, "last": sync.STATE.get("last") or settings.state_get("last_sync")},
        "gpu_decode": {"available": media.HW["cuda"], "reason": media.HW["reason"],
                       "mode": cfg["scan"].get("hw_decode", "auto")},
        "whisper": {"loaded": stt.WHISPER._model is not None, "device": stt.WHISPER.device,
                    "model": stt.WHISPER.model_name or cfg["whisper"]["model"], "error": stt.WHISPER.load_error},
        "files": {"total": sum(counts.values()), **counts},
        "queue": scheduler.queue_count(),
        "memory": health.memory(),
        "files_not_found": db.scalar("SELECT COUNT(*) FROM issues WHERE code='file_not_found' AND status='open'"),
        "forcing_queue": bool(settings.state_get("force_queue", False)),
        "open_issues": db.scalar(f"SELECT COUNT(*) FROM ({ISSUE_SQL} AND x.status='open' AND x.severity!='dual')"),
        "open_dual_issues": db.scalar(f"SELECT COUNT(*) FROM ({ISSUE_SQL} AND x.status='open' AND x.severity='dual')"),
        "pending_reviews": db.scalar(f"SELECT COUNT(*) FROM ({REVIEW_SQL} AND r.status='pending')"),
        "proposed_actions": len(proposed) - n_dual,
        "proposed_dual_actions": n_dual,
        "subgen_configured": subgen.configured(),
        "automation": {"enabled": cfg["automation"]["enabled"], "active_now": actions.automation_active(cfg),
                       "today": actions.actions_today(), "max_per_day": cfg["automation"]["max_actions_per_day"]},
        "unraid": {"detected": unraid.is_unraid(), "parity_running": unraid.parity_running()},
        "configured": {k: c().configured for k, c in arr.CLIENTS.items()},
    }


@app.get("/api/coverage", dependencies=[Depends(auth)])
def coverage():
    def grp(expr: str):
        return db.q(f"""SELECT {expr} AS name, COUNT(*) total,
            SUM(f.last_scan_at IS NOT NULL) scanned, SUM(f.status='ok') ok, SUM(f.status IN ('info','warning')) warn,
            SUM(f.status='dual') dual, SUM(f.status IN ('error','critical','failed')) bad
            FROM files f JOIN items i ON i.id=f.item_id WHERE f.missing=0 GROUP BY 1 ORDER BY 1""")
    return {
        "libraries": grp("CASE WHEN i.is_anime=1 THEN 'Anime (' || i.source || ')' WHEN i.source='sonarr' THEN 'TV' ELSE 'Movies' END"),
        "disks": grp("CASE WHEN f.disk='' THEN '(unknown)' ELSE f.disk END"),
        "disk_state": unraid.disks_state(),
        "oldest_unscanned": db.q("""SELECT f.id, i.title, f.label, f.disk, f.date_added FROM files f JOIN items i ON
            i.id=f.item_id WHERE f.missing=0 AND f.last_scan_at IS NULL ORDER BY f.first_seen LIMIT 10"""),
        "next_up": [{"id": r["id"], "title": r["title"], "label": r["label"], "disk": r["disk"],
                     "score": round(r["score"], 1)}
                    for r in scheduler.next_files(None, settings.get()["schedule"]["rescan_after_days"], 10)],
    }


@app.get("/api/activity", dependencies=[Depends(auth)])
def activity(limit: int = 100):
    return db.q("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (min(limit, 1000),))


# ====================================================================== library

@app.get("/api/items", dependencies=[Depends(auth)])
def items(search: str = "", source: str = "", anime: int | None = None, status: str = "", page_no: int = Query(1, alias="page"),
          per_page: int = 50):
    where, args = ["1=1"], []
    if search:
        where.append("i.title LIKE ?")
        args.append(f"%{search}%")
    if source:
        where.append("i.source=?")
        args.append(source)
    if anime is not None:
        where.append("i.is_anime=?")
        args.append(anime)
    having = ""
    if status == "problems":
        having = "HAVING bad > 0"
    elif status == "unscanned":
        having = "HAVING scanned < files"
    base = f"""SELECT i.*, COUNT(f.id) files, SUM(f.last_scan_at IS NOT NULL) scanned,
        SUM(f.status IN ('error','critical','failed')) bad, SUM(f.status IN ('warning')) warn, SUM(f.status='dual') dual
        FROM items i LEFT JOIN files f ON f.item_id=i.id AND f.missing=0 WHERE {' AND '.join(where)}
        GROUP BY i.id {having}"""
    return page(base, args, page_no, min(per_page, 500), "title COLLATE NOCASE")


@app.get("/api/items/{item_id}", dependencies=[Depends(auth)])
def item(item_id: int):
    it = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not it:
        raise HTTPException(404)
    it["extra"] = db.unj(it["extra"], {})
    it["files"] = db.q("SELECT * FROM files WHERE item_id=? AND missing=0 ORDER BY label, arr_path", (item_id,))
    for f in it["files"]:
        f["episodes"] = db.unj(f["episodes"], [])
    it.update(audio_request.describe(it))
    it["audio_exceptions"] = audio_request.list_for_item(item_id)
    it["open_audio_gaps"] = audio_request.open_gaps(item_id)
    return it


@app.patch("/api/items/{item_id}", dependencies=[Depends(auth)])
def item_update(item_id: int, body: dict = Body(...)):
    allowed = {"language_override", "multi_segment", "excluded", "is_anime", "audio_request_override"}
    sets = {k: v for k, v in body.items() if k in allowed}
    if "audio_request_override" in sets:
        v = (sets["audio_request_override"] or "").strip().lower()
        if v not in ("", "original", "english", "either"):
            raise HTTPException(400, "audio_request_override must be '' (follow the profile), original, english or either")
        sets["audio_request_override"] = v
    if "language_override" in sets:
        from . import langs

        v = sets["language_override"] or ""
        sets["language_override"] = langs.norm(v) if v else ""
        if v and not sets["language_override"]:
            raise HTTPException(400, f"unknown language '{v}'")
    for k, v in sets.items():
        db.x(f"UPDATE items SET {k}=? WHERE id=?", (v, item_id))
    return item(item_id)


@app.get("/api/items/{item_id}/audio_exceptions", dependencies=[Depends(auth)])
def item_audio_exceptions(item_id: int):
    if not db.q1("SELECT id FROM items WHERE id=?", (item_id,)):
        raise HTTPException(404)
    return audio_request.list_for_item(item_id)


@app.get("/api/audio_exceptions", dependencies=[Depends(auth)])
def audio_exceptions_all():
    """Every accept-as-is exception with its show, scope and how many files it covers (`files`) and how many of
    those the last scan found with both original-language and English audio (`fixed`)."""
    return audio_request.list_all()


@app.post("/api/items/{item_id}/accept_show_audio", dependencies=[Depends(auth)])
def item_accept_show_audio(item_id: int, body: dict = Body(default={})):
    """Accept every season of a series as is (optional {"allow": "original_only" | "english_only"}; default is what
    its open warnings call for). Series only."""
    try:
        return audio_request.accept_show(item_id, body.get("allow") or None)
    except LookupError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/audio_exceptions/{exception_id}", dependencies=[Depends(auth)])
def audio_exception_delete(exception_id: int):
    """Remove an accept-as-is exception; that season's files are queued for a standard rescan."""
    r = audio_request.remove(exception_id)
    if r is None:
        raise HTTPException(404)
    return r


@app.get("/api/files", dependencies=[Depends(auth)])
def files(status: str = "", disk: str = "", item_id: int | None = None, search: str = "",
          page_no: int = Query(1, alias="page"), per_page: int = 50):
    where, args = ["f.missing=0"], []
    if status:
        where.append(f"f.status IN ({','.join('?' * len(status.split(',')))})")
        args += status.split(",")
    if disk:
        where.append("f.disk=?")
        args.append(disk)
    if item_id:
        where.append("f.item_id=?")
        args.append(item_id)
    if search:
        where.append("(i.title LIKE ? OR f.arr_path LIKE ?)")
        args += [f"%{search}%", f"%{search}%"]
    base = f"""SELECT f.id, f.item_id, i.title, i.kind, i.is_anime, f.label, f.disk, f.size, f.status, f.open_issues,
        f.last_scan_at, f.last_profile, f.release_group, f.quality, f.arr_path FROM files f JOIN items i ON i.id=f.item_id
        WHERE {' AND '.join(where)}"""
    return page(base, args, page_no, min(per_page, 500), "i.title COLLATE NOCASE, f.label")


@app.get("/api/files/{file_id}", dependencies=[Depends(auth)])
def file_detail(file_id: int):
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise HTTPException(404)
    f["episodes"] = db.unj(f["episodes"], [])
    f["item"] = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    f["item"].update(audio_request.describe(f["item"]))
    f["audio_exceptions"] = [e for e in audio_request.list_for_item(f["item_id"])
                             if e["season"] == audio_request.season_of(f)]
    last = db.q1("SELECT * FROM scans WHERE id=?", (f["last_scan_id"],)) if f["last_scan_id"] else None
    if last:
        last["result"] = db.unj(last["result"], {})
    f["last_scan"] = last
    f["scans"] = db.q("SELECT id, profile, source, started_at, finished_at, status, seed FROM scans WHERE file_id=? "
                      "ORDER BY id DESC LIMIT 20", (file_id,))
    f["issues"] = [dict(i, details=db.unj(i["details"], {})) for i in
                   db.q("SELECT * FROM issues WHERE file_id=? AND status!='resolved' ORDER BY id DESC", (file_id,))]
    f["reviews"] = [dict(r, timestamps=db.unj(r["timestamps"], [])) for r in
                    db.q("SELECT * FROM reviews WHERE file_id=? ORDER BY id DESC LIMIT 10", (file_id,))]
    f["actions"] = db.q("SELECT * FROM actions WHERE file_id=? ORDER BY id DESC LIMIT 10", (file_id,))
    ev = db.unj(f.pop("audio_evidence", None), {}) or {}
    f["audio_tracks"] = ev.get("tracks") or []  # every audio track with its real flags + the language verdict
    f["subtitle_tracks"] = ev.get("subs") or []
    try:
        f["facts"] = trackedit.file_facts(f)
    except Exception as e:  # noqa: BLE001
        f["facts"] = {"error": str(e)}
    f["whisper_blocker"] = actions.whisper_blocker(file_id)
    if subgen.configured():
        plan, why = subgen.explain(file_id, manual=True)
        f["subgen"] = {"plan": plan, "blocker": why or None}
    return f


MCP_BRIDGE_VERSION = 10  # bump when mcp/anotherr_mcp.py changes in a way users should re-download for
FRAME_LIMITER = anyio.CapacityLimiter(frames.MAX_LIVE)
FRAME_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


@app.get("/api/files/{file_id}/frame", dependencies=[Depends(auth)])
async def frame(file_id: int, t: float, w: int = 768):
    """Cached frames are served straight from disk. Uncached ones wait (without tying up the API's worker
    threads) for one of a few extraction slots, so a page full of frames can't start dozens of ffmpegs."""
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise HTTPException(404)
    hit = frames.cached(f, t, w)
    if hit:
        frames.touch(hit)
        return FileResponse(hit, media_type="image/jpeg", headers=FRAME_CACHE_HEADERS)
    path = await anyio.to_thread.run_sync(scanner.resolve_path, f)
    if not path:
        scanner.handle_missing(f)
        raise HTTPException(404, "file not found on disk - resyncing it from Sonarr/Radarr")
    try:
        data = await anyio.to_thread.run_sync(frames.extract, f, path, t, w, limiter=FRAME_LIMITER)
    except TimeoutError as e:
        raise HTTPException(503, "busy extracting frames, try again", headers={"Retry-After": "3"}) from e
    except Exception as e:  # noqa: BLE001 - a bad frame must never take the server down
        raise HTTPException(500, f"frame extraction failed: {e}") from e
    return Response(data, media_type="image/jpeg", headers=FRAME_CACHE_HEADERS)


@app.get("/api/files/{file_id}/streams", dependencies=[Depends(auth)])
def file_streams(file_id: int):
    """Live ffprobe of the file: every stream with codec, language, title, default/forced, duration."""
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise HTTPException(404)
    path = scanner.resolve_path(f)
    if not path:
        scanner.handle_missing(f)
        raise HTTPException(404, "file not found on disk - resyncing it from Sonarr/Radarr")
    info = media.summarize(media.ffprobe(path))
    info["subs"] += scanner.file_sidecars(f, path)
    return {"file_id": file_id, "path": path, **info}


# ====================================================================== scanning

@app.post("/api/scan", dependencies=[Depends(auth)])
def scan(body: dict = Body(...)):
    """scope: file | item | disk | library | random | problems | unscanned. full=true skips quick-rescan reuse."""
    scope = body.get("scope", "file")
    profile = body.get("profile") or settings.get()["scan"]["default_profile"]
    if profile not in settings.get()["scan"]["profiles"]:
        raise HTTPException(400, f"unknown profile {profile}")
    ids = [int(i) for i in body.get("ids") or []]
    count = int(body.get("count") or 0)
    where, args = ["f.missing=0", "i.excluded=0"], []
    if scope == "file":
        if not ids:
            raise HTTPException(400, "ids required")
        where.append(f"f.id IN ({','.join('?' * len(ids))})")
        args += ids
    elif scope == "item":
        if not ids:
            raise HTTPException(400, "ids required")
        where.append(f"f.item_id IN ({','.join('?' * len(ids))})")
        args += ids
    elif scope == "disk":
        where.append("f.disk=?")
        args.append(body.get("disk", ""))
    elif scope == "library":
        lib = body.get("library", "")
        if lib == "anime":
            where.append("i.is_anime=1")
        elif lib in ("sonarr", "tv"):
            where.append("i.source='sonarr'")
        elif lib in ("radarr", "movies"):
            where.append("i.source='radarr'")
        else:
            raise HTTPException(400, "library must be tv | movies | anime")
    elif scope == "problems":
        where.append("f.status IN ('warning','error','critical','failed')")
    elif scope == "unscanned":
        where.append("f.last_scan_at IS NULL")
    elif scope != "random":
        raise HTTPException(400, f"unknown scope {scope}")
    if body.get("disk") and scope != "disk":
        where.append("f.disk=?")
        args.append(body["disk"])
    rows = db.q(f"SELECT f.id FROM files f JOIN items i ON i.id=f.item_id WHERE {' AND '.join(where)}", args)
    fids = [r["id"] for r in rows]
    if scope == "random" or (count and len(fids) > count):
        random.shuffle(fids)
        fids = fids[: (count or 10)]
    full = bool(body.get("full"))
    n = scheduler.enqueue(fids, profile, "manual", full=full)
    db.log(f"Queued {n} file(s) for {'full ' if full else ''}{profile} scan ({scope})")
    return {"queued": n, "file_ids": fids[:200]}


@app.post("/api/scan/stop", dependencies=[Depends(auth)])
def scan_stop():
    scanner.CANCEL.set()
    return {"ok": True}


@app.get("/api/queue", dependencies=[Depends(auth)])
def queue_list(limit: int = 0):
    """Same rows scheduler.queue_count() counts (files still in the library). limit 0 = all."""
    return db.q("""SELECT q.*, i.title, f.label FROM queue q JOIN files f ON f.id=q.file_id JOIN items i ON i.id=f.item_id
        WHERE f.missing=0 ORDER BY CASE q.source WHEN 'manual' THEN 0 WHEN 'import' THEN 1 ELSE 2 END, q.created_at
        LIMIT ?""", (limit if limit > 0 else -1,))


@app.delete("/api/queue", dependencies=[Depends(auth)])
def queue_clear():
    return {"removed": db.n("DELETE FROM queue")}


@app.delete("/api/queue/{qid}", dependencies=[Depends(auth)])
def queue_remove(qid: int):
    return {"removed": db.n("DELETE FROM queue WHERE id=?", (qid,))}


@app.post("/api/control/pause", dependencies=[Depends(auth)])
def control_pause(body: dict = Body(...)):
    settings.state_set("paused", bool(body.get("paused")))
    db.log("Scanning paused" if body.get("paused") else "Scanning resumed")
    return {"paused": bool(body.get("paused"))}


@app.post("/api/queue/process", dependencies=[Depends(auth)])
def queue_process(body: dict = Body(default={})):
    """Work through everything in the queue now, ignoring pauses (parity, Plex streams, manual pause).
    Stops forcing once the queue is empty. stop=true cancels."""
    stop = bool(body.get("stop"))
    settings.state_set("force_queue", not stop)
    n = scheduler.queue_count()
    db.log("Stopped forcing the queue" if stop else f"Processing queue now ({n} file(s), ignoring pauses)")
    return {"forcing": not stop, "queued": n}


@app.post("/api/sync", dependencies=[Depends(auth)])
def sync_now():
    if sync.STATE["running"]:
        return {"ok": False, "error": "already running"}
    threading.Thread(target=sync.full_sync, daemon=True).start()
    return {"ok": True}


# ====================================================================== issues / reviews / actions

ISSUE_SQL = """SELECT x.*, i.title, f.label, f.disk, f.episodes AS file_episodes, i.id AS item_id FROM issues x JOIN files f ON f.id=x.file_id
    JOIN items i ON i.id=f.item_id WHERE f.missing=0"""


@app.get("/api/issues", dependencies=[Depends(auth)])
def issues(status: str = "open", severity: str = "", code: str = "", category: str = "", search: str = "",
           page_no: int = Query(1, alias="page"), per_page: int = 100, group: bool = False, dual: str = ""):
    """group=true folds issues of the same code for the same series + season into one row (paged by group).
    dual=only keeps the blue dual-audio issues (the Dual audio tab); dual=exclude drops them (the Issues tab)."""
    sql, args = ISSUE_SQL, []
    dual_sql = {"only": " AND x.severity='dual'", "exclude": " AND x.severity!='dual'"}.get(dual, "")
    sql += dual_sql
    for col, val in (("x.status", status), ("x.severity", severity), ("x.code", code), ("x.category", category)):
        if val:
            vals = val.split(",")
            sql += f" AND {col} IN ({','.join('?' * len(vals))})"
            args += vals
    if search:
        sql += " AND (i.title LIKE ? OR x.message LIKE ?)"
        args += [f"%{search}%"] * 2
    order = "CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 WHEN 'warning' THEN 2 WHEN 'dual' THEN 3 ELSE 4 END, id DESC"
    if group:
        res = _grouped_issues(db.q(f"{sql} ORDER BY {order}", args), page_no, min(per_page, 500))
    else:
        res = page(sql, args, page_no, min(per_page, 500), order)
    fixes = _proposed_track_fixes()
    for r in res["rows"]:
        r["details"] = db.unj(r.get("details"), {})
        r.pop("file_episodes", None)
        if r["code"] in TRACK_FIX_CODES:  # fixed by a track-edit proposal, not a resub
            files = [i["file_id"] for i in r["items"]] if r.get("group") else [r["file_id"]]
            hit = {fixes[(f, r["code"])] for f in files if (f, r["code"]) in fixes}
            r["fix_actions"] = sorted(hit)
            r["fix_missing"] = [f for f in files if (f, r["code"]) not in fixes]
    res["codes"] = db.q(f"SELECT x.code, x.severity, COUNT(*) n FROM issues x JOIN files f ON f.id=x.file_id "
                        f"WHERE x.status='open' AND f.missing=0{dual_sql} GROUP BY x.code, x.severity ORDER BY n DESC")
    return res


TRACK_FIX_CODES = {"default_audio_not_preferred": ("set_default_audio",),
                   "default_subs_foreign": ("set_default_audio", "set_default_subtitle"),
                   "default_subs_signs": ("set_default_audio", "set_default_subtitle"),  # combined proposals
                   "audio_untagged": ("retag_audio",), "audio_mislabeled": ("retag_audio",)}


def _proposed_track_fixes() -> dict[tuple[int, str], int]:
    """(file_id, issue code) -> the open track-edit proposal that fixes it."""
    out = {}
    for a in db.q("SELECT id, kind, payload FROM actions WHERE status='proposed' AND kind IN "
                  "('set_default_audio','set_default_subtitle','retag_audio')"):
        for e in (db.unj(a["payload"], {}) or {}).get("files", []):
            for code, kinds in TRACK_FIX_CODES.items():
                if a["kind"] not in kinds:
                    continue
                if code in ("default_subs_signs", "default_subs_foreign") and not e.get("sub"):
                    continue
                if code == "default_audio_not_preferred" and not e.get("audio"):
                    continue
                out[(e["file_id"], code)] = a["id"]
    return out


def _season_of(episodes_json) -> int | None:
    s = [e.get("season") for e in db.unj(episodes_json, []) or [] if e.get("season") is not None]
    return min(s) if s else None


def _grouped_issues(rows: list[dict], page_no: int, per: int) -> dict:
    groups: dict[tuple, dict] = {}
    out = []
    for r in rows:
        season = _season_of(r.get("file_episodes"))
        key = (r["item_id"], season, r["code"], r["severity"], r["status"]) if season is not None else ("id", r["id"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"group": True, "title": r["title"], "item_id": r["item_id"], "season": season,
                               "code": r["code"], "severity": r["severity"], "status": r["status"], "items": []}
            out.append(g)
        g["items"].append({k: r[k] for k in ("id", "file_id", "label", "disk", "message")})
    rows_out = []
    for g in out:
        if len(g["items"]) == 1:  # a lone issue stays a plain row
            one = g["items"][0]
            rows_out.append({**next(r for r in rows if r["id"] == one["id"])})
            continue
        g["items"].sort(key=lambda i: i["label"] or "")
        msgs = list(dict.fromkeys(i["message"] for i in g["items"]))
        g.update(ids=[i["id"] for i in g["items"]], message=msgs[0] if len(msgs) == 1 else None,
                 disks=sorted({i["disk"] for i in g["items"] if i["disk"]}))
        rows_out.append(g)
    total = len(rows_out)
    return {"total": total, "issues": len(rows), "page": page_no, "per_page": per,
            "rows": rows_out[(page_no - 1) * per: page_no * per]}


@app.post("/api/issues/bulk", dependencies=[Depends(auth)])
def issues_bulk(body: dict = Body(...)):
    """{"ids": [...], "op": "ignore" | "open" | "propose_sub_fix"}. propose_sub_fix proposes a Bazarr resub per file,
    or a Whisper subtitle where Bazarr's search already came up empty for that series (same rule as automatic
    proposals)."""
    ids, op = [int(i) for i in body.get("ids") or []], body.get("op")
    rows = db.q(f"SELECT x.id, x.file_id, x.code, x.message, f.item_id FROM issues x JOIN files f ON f.id=x.file_id "
                f"WHERE x.id IN ({','.join('?' * len(ids))})", ids) if ids else []
    if op in ("ignore", "open"):
        st = "ignored" if op == "ignore" else "open"
        for r in rows:
            db.x("UPDATE issues SET status=?, updated_at=? WHERE id=?", (st, time.time(), r["id"]))
        for fid in {r["file_id"] for r in rows}:
            _refresh_file_status(fid)
            actions.reconcile(fid)
        for iid in {r["item_id"] for r in rows}:
            retag.refresh_item(iid)
            defaults.refresh_item(iid)
            subgen.refresh_item(iid)
        return {"ok": True, "changed": len(rows)}
    if op == "propose_sub_fix":
        made: dict[str, int] = {}
        existing = 0
        sg = [r["file_id"] for r in rows if subgen.configured() and subgen.wanted(r["file_id"])]
        if sg:
            made["subgen_transcribe"] = len(subgen.request(sg)["proposed"])
        for r in rows:
            if r["file_id"] in sg:
                continue
            kind, reason = actions._sub_fix(r["file_id"], r["item_id"], [r["code"]])
            if kind == "skip":
                kind, reason = "resub", f"Manual, for {r['code']}"
            aid = actions.propose(r["file_id"], kind, reason.replace("Issues:", "Manual, for", 1), [r["code"]])
            if aid:
                made[kind] = made.get(kind, 0) + 1
            else:
                existing += 1
        return {"ok": True, "proposed": made, "already_proposed": existing}
    raise HTTPException(400, "op must be ignore | open | propose_sub_fix")


EVIDENCE_KEYS = {
    "video": ["decode", "freeze_check", "gpu_decode"],
    "integrity": ["decode", "full_decode", "runtime_estimate", "runtime_ratio", "expected_runtime_s", "segments"],
    "multi_episode": ["runtime_estimate", "runtime_ratio", "segment_starts", "segment_breaks", "episodes_mapped"],
    "language": ["audio_tracks", "expected_language", "original_audio_note"],
    "audio": ["audio_tracks"],
    "subtitles": ["subtitle_tracks", "subtitle_sync", "subtitle_content", "subtitle_mismatch_recheck",
                  "sync_audio_track", "sync_skipped", "transcript_sample", "audio_tracks"],
    "identity": ["title_cards_ocr", "runtime_estimate", "subtitle_sync", "subtitle_content",
                 "subtitle_mismatch_recheck", "audio_tracks"],
}


@app.get("/api/issues/{issue_id}", dependencies=[Depends(auth)])
def issue_detail(issue_id: int):
    """An issue with the evidence behind it: thresholds/scores from the scan and timestamps worth looking at."""
    i = db.q1(ISSUE_SQL + " AND x.id=?", (issue_id,))
    if not i:
        raise HTTPException(404)
    i["details"] = db.unj(i["details"], {})
    scan = db.q1("SELECT id, profile, seed, finished_at, result FROM scans WHERE id=?", (i["scan_id"],)) if i["scan_id"] else None
    res = db.unj(scan["result"], {}) if scan else {}
    i["scan"] = {k: scan[k] for k in ("id", "profile", "seed", "finished_at")} if scan else None
    i["evidence"] = {k: res[k] for k in EVIDENCE_KEYS.get(i["category"], []) if k in res}
    i["suppressed_by_this_scan"] = res.get("suppressed", [])
    ts = list(i["details"].get("evidence_ts") or [])
    if not ts and isinstance(i["details"].get("target"), (int, float)):
        ts = [round(i["details"]["target"] + o, 1) for o in (-20, 0, 20)]
    if not ts and i["details"].get("windows"):
        ts = [round(w[0] + w[1] / 2, 1) for w in i["details"]["windows"] if isinstance(w, (list, tuple))]
    i["evidence_ts"] = ts[:8]
    return i


@app.patch("/api/issues/{issue_id}", dependencies=[Depends(auth)])
def issue_update(issue_id: int, body: dict = Body(...)):
    st = body.get("status")
    if st not in ("open", "ignored", "resolved"):
        raise HTTPException(400, "status must be open | ignored | resolved")
    db.x("UPDATE issues SET status=?, updated_at=? WHERE id=?", (st, time.time(), issue_id))
    fid = db.scalar("SELECT file_id FROM issues WHERE id=?", (issue_id,))
    _refresh_file_status(fid)
    actions.reconcile(fid)
    item_id = db.scalar("SELECT item_id FROM files WHERE id=?", (fid,))
    if item_id:
        retag.refresh_item(item_id)  # ignoring a file's untagged/mislabeled issue takes it out of its retag batch
        defaults.refresh_item(item_id)
        subgen.refresh_item(item_id)
    return {"ok": True}


def _refresh_file_status(fid: int | None):
    if not fid:
        return
    f = db.q1("SELECT last_scan_at, status FROM files WHERE id=?", (fid,))
    if not f or f["last_scan_at"] is None:
        return
    sev = [r["severity"] for r in db.q("SELECT severity FROM issues WHERE file_id=? AND status='open'", (fid,))]
    db.x("UPDATE files SET status=?, open_issues=? WHERE id=?", (db.worst(sev) or "ok", len(sev), fid))


REVIEW_SQL = """SELECT r.*, i.title, i.kind, i.year, f.label FROM reviews r JOIN files f ON f.id=r.file_id
    JOIN items i ON i.id=f.item_id WHERE f.missing=0"""


@app.get("/api/reviews", dependencies=[Depends(auth)])
def reviews(status: str = "pending", kind: str = "", limit: int = 50, offset: int = 0):
    sql, args = REVIEW_SQL, []
    if status:
        sql += " AND r.status=?"
        args.append(status)
    if kind:
        sql += " AND r.kind=?"
        args.append(kind)
    rows = db.q(sql + " ORDER BY r.id LIMIT ? OFFSET ?", args + [min(limit, 500), max(offset, 0)])
    for r in rows:
        r["timestamps"] = db.unj(r["timestamps"], [])
        r["context"] = db.unj(r["context"], {})
    return rows


@app.get("/api/reviews/{review_id}", dependencies=[Depends(auth)])
def review(review_id: int):
    r = db.q1(REVIEW_SQL + " AND r.id=?", (review_id,))
    if not r:
        raise HTTPException(404)
    r["timestamps"] = db.unj(r["timestamps"], [])
    r["context"] = db.unj(r["context"], {})
    return r


@app.post("/api/reviews/{review_id}/verdict", dependencies=[Depends(auth)])
def review_verdict(review_id: int, body: dict = Body(...)):
    """verdict: ok | problem | dismiss. With problem, request_redownload=true proposes a redownload."""
    r = db.q1("SELECT * FROM reviews WHERE id=?", (review_id,))
    if not r:
        raise HTTPException(404)
    verdict = body.get("verdict")
    if verdict not in ("ok", "problem", "dismiss"):
        raise HTTPException(400, "verdict must be ok | problem | dismiss")
    notes = (body.get("notes") or "")[:4000]
    now = time.time()
    db.x("UPDATE reviews SET status=?, verdict=?, notes=?, updated_at=? WHERE id=?",
         ("dismissed" if verdict == "dismiss" else "done", verdict, notes, now, review_id))
    out: dict[str, Any] = {"ok": True}
    if verdict == "problem":
        sev = body.get("severity") or "error"
        db.x("INSERT INTO issues(file_id, scan_id, category, code, severity, message, details, status, created_at, "
             "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
             (r["file_id"], r["scan_id"], "visual", "visual_review_failed", sev,
              f"Visual review ({r['kind']}): {notes or 'problem found'}", db.j({"review_id": review_id}), "open", now, now))
        _refresh_file_status(r["file_id"])
        if body.get("request_redownload"):
            out["action_id"] = actions.propose(r["file_id"], "redownload", f"Visual review: {notes[:200]}")
            if out["action_id"]:
                db.x("UPDATE actions SET codes=? WHERE id=?", (db.j(["visual_review_failed"]), out["action_id"]))
                actions.maybe_auto([out["action_id"]])
    return out


ACTION_SQL = """SELECT a.*, i.title, f.label, f.item_id, f.episodes FROM actions a JOIN files f ON f.id=a.file_id JOIN items i ON i.id=f.item_id"""


def _action_rows(status: str, limit: int) -> list[dict]:
    sql, args = ACTION_SQL, []
    if status:
        vals = status.split(",")
        sql += f" WHERE a.status IN ({','.join('?' * len(vals))})"
        args += vals
    rows = db.q(sql + " ORDER BY a.id DESC LIMIT ?", args + [limit if limit > 0 else -1])
    for r in rows:
        r["codes"] = db.unj(r["codes"], [])
        r["payload"] = db.unj(r.get("payload"), None)
        eps = db.unj(r.pop("episodes", None), []) or []
        seasons = [e.get("season") for e in eps if e.get("season") is not None]
        r["season"] = min(seasons) if seasons else None
    actions.attach_choice_keys(rows)
    return rows


@app.get("/api/actions", dependencies=[Depends(auth)])
def actions_list(status: str = "proposed", limit: int = 0, dual: str = ""):
    """dual=only: the Dual actions tab; dual=exclude: the main Actions tab. limit 0 = all, so the tabs list every
    row their badge counts. The tab filter runs before any limit."""
    if dual not in ("only", "exclude"):
        return _action_rows(status, limit)
    rows = [r for r in _action_rows(status, 0) if actions.is_dual(r) == (dual == "only")]
    return rows[:limit] if limit > 0 else rows


@app.post("/api/actions/choose", dependencies=[Depends(auth)])
def actions_choose(body: dict = Body(...)):
    """{"choice_key": "...", "pick": "<kind>"}: approve the proposals of that kind in a choice (alternative fixes for
    the same files) and reject the others."""
    try:
        out = actions.choose(_action_rows("proposed", 0), str(body.get("choice_key") or ""), str(body.get("pick") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(409, "this choice no longer exists (the proposals were handled or withdrawn)")
    return out


@app.post("/api/actions/bulk", dependencies=[Depends(auth)])
def actions_bulk(body: dict = Body(...)):
    """{"ids": [...], "op": "approve" | "reject"}. Approve runs resub/redownload proposals one after another in the
    background (they show as 'queued' until their turn)."""
    ids = [int(i) for i in body.get("ids") or []]
    if body.get("op") == "reject":
        n = sum(db.n("UPDATE actions SET status='rejected', updated_at=? WHERE id=? AND status='proposed'",
                     (time.time(), i)) for i in ids)
        return {"ok": True, "rejected": n}
    if body.get("op") == "approve":
        return actions.approve_many(ids)
    raise HTTPException(400, "op must be approve | reject")


@app.get("/api/actions/bulk-preview", dependencies=[Depends(auth)])
def actions_bulk_preview():
    return {"english_retags": len(actions.english_retag_ids()), "out_of_sync_resubs": len(actions.subs_out_of_sync_ids()),
            "english_default_audio": len(actions.english_default_audio_ids()),
            "foreign_defaults": len(actions.foreign_default_ids()),
            "min_confidence": settings.get()["retag"].get("bulk_min_confidence", 0.95)}


@app.post("/api/actions/bulk-english-retag", dependencies=[Depends(auth)])
def actions_bulk_english():
    return actions.approve_english_retags()


@app.post("/api/actions/bulk-english-default-audio", dependencies=[Depends(auth)])
def actions_bulk_english_default_audio():
    return actions.approve_english_default_audio()


@app.post("/api/actions/bulk-foreign-defaults", dependencies=[Depends(auth)])
def actions_bulk_foreign_defaults():
    return actions.approve_foreign_defaults()


@app.post("/api/actions/bulk-out-of-sync", dependencies=[Depends(auth)])
def actions_bulk_out_of_sync():
    return actions.approve_many(actions.subs_out_of_sync_ids())


@app.post("/api/actions", dependencies=[Depends(auth)])
def action_create(body: dict = Body(...)):
    """Manual proposal: kind resub | redownload | whisper_subtitle, optionally for one issue (issue_id)."""
    kind = body.get("kind")
    if kind not in actions.MANUAL_KINDS:
        raise HTTPException(400, "kind must be " + " | ".join(actions.MANUAL_KINDS))
    file_id, reason, codes = int(body.get("file_id") or 0), body.get("reason") or "manual", []
    if body.get("issue_id"):
        i = db.q1("SELECT file_id, code, message FROM issues WHERE id=?", (int(body["issue_id"]),))
        if not i:
            raise HTTPException(404, "issue not found")
        file_id, codes = i["file_id"], [i["code"]]
        reason = f"Manual, for {i['code']}: {i['message'][:200]}"
    if kind == "accept_season_audio":
        try:
            aid = audio_request.propose_for_file(file_id, body.get("allow"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"action_id": aid, "note": None if aid else "no open proposal could be made for this season"}
    if kind == "whisper_subtitle" and subgen.configured():
        return _whisper_via_subgen(file_id)
    if kind == "whisper_subtitle":
        why = actions.whisper_blocker(file_id)
        if why:
            raise HTTPException(400, why)
        reason = reason.replace("Manual,", "Bazarr Whisper subtitle (translate),", 1) if codes else \
            "Bazarr Whisper subtitle (translate) - manual"
    aid = actions.propose(file_id, kind, reason, codes)
    return {"action_id": aid, "note": None if aid else "an open proposal of this kind already exists"}


def _whisper_via_subgen(file_id: int) -> dict:
    """A Whisper request goes to the subgen worker (Bazarr's Whisper provider is unreliable)."""
    r = subgen.request([file_id])
    if file_id not in r["proposed"]:
        raise HTTPException(400, r["not_possible"].get(file_id) or "not possible")
    for a in db.q("SELECT id, payload FROM actions WHERE kind=? AND status='proposed' ORDER BY id DESC", (subgen.KIND,)):
        if any(e.get("file_id") == file_id for e in (db.unj(a["payload"], {}) or {}).get("files", [])):
            return {"action_id": a["id"], "note": "sent to the subgen worker"}
    raise HTTPException(400, "subgen accepted the file but no proposal was made")


# ====================================================================== subgen

@app.get("/api/subgen/status", dependencies=[Depends(auth)])
def subgen_status():
    waiting = 0
    for a in db.q("SELECT payload FROM actions WHERE kind=? AND status IN ('queued','running')", (subgen.KIND,)):
        waiting += sum(e.get("status") == "waiting" for e in (db.unj(a["payload"], {}) or {}).get("files", []))
    return {"configured": subgen.configured(), "paused": subgen.paused(), "worker": subgen.STATUS,
            "waiting_files": waiting, "pilot_done": bool(settings.state_get("subgen_pilot_done", False)),
            "info": settings.state_get("subgen_info")}


@app.post("/api/subgen/pause", dependencies=[Depends(auth)])
def subgen_pause(body: dict = Body(...)):
    subgen.set_paused(bool(body.get("paused")))
    return {"ok": True, "paused": subgen.paused()}


@app.post("/api/subgen/request", dependencies=[Depends(auth)])
def subgen_request(body: dict = Body(...)):
    """A person asks for subgen subtitles: {"file_ids": [...]} or {"issue_ids": [...]}. Proposed per season."""
    ids = [int(i) for i in body.get("file_ids") or []]
    iss = [int(i) for i in body.get("issue_ids") or []]
    if iss:
        ids += [r["file_id"] for r in db.q(f"SELECT file_id FROM issues WHERE id IN ({','.join('?' * len(iss))})", iss)]
    if not subgen.configured():
        raise HTTPException(400, "subgen isn't set up (Settings -> Subgen -> url)")
    return subgen.request(sorted(set(ids)))


@app.post("/api/actions/{action_id}/control", dependencies=[Depends(auth)])
def action_control(action_id: int, body: dict = Body(...)):
    """subgen batches: {"op": "pause" | "resume" | "cancel"}."""
    return subgen.control(action_id, body.get("op", ""))


@app.post("/api/actions/{action_id}/approve", dependencies=[Depends(auth)])
def action_approve(action_id: int):
    return actions.execute(action_id)


@app.post("/api/actions/{action_id}/undo", dependencies=[Depends(auth)])
def action_undo(action_id: int):
    """Restore the track languages/default flags a track-edit action changed (from the rollback log)."""
    a = db.q1("SELECT kind, status FROM actions WHERE id=?", (action_id,))
    if not a or a["kind"] not in actions.TRACK_KINDS or a["status"] not in ("done", "failed"):
        raise HTTPException(400, "only finished track-edit actions (retag/default track) can be undone")
    return trackedit.undo(action_id)


@app.get("/api/actions/{action_id}", dependencies=[Depends(auth)])
def action_detail(action_id: int):
    a = db.q1(ACTION_SQL + " WHERE a.id=?", (action_id,))
    if not a:
        raise HTTPException(404)
    a["codes"] = db.unj(a["codes"], [])
    a["payload"] = db.unj(a.get("payload"), None)
    return a


@app.post("/api/actions/{action_id}/reject", dependencies=[Depends(auth)])
def action_reject(action_id: int):
    actions.reject(action_id)
    return {"ok": True}


# ====================================================================== settings

@app.get("/api/settings", dependencies=[Depends(auth)])
def settings_get():
    return {"settings": settings.masked(), "defaults": settings.DEFAULTS, "disks": scheduler.all_disks(),
            "resub_codes": settings.RESUB_CODES, "redownload_codes": settings.REDOWNLOAD_CODES}


@app.put("/api/settings", dependencies=[Depends(auth)])
def settings_put(body: dict = Body(...)):
    before = settings.get().get("path_mappings")
    saved = settings.save(body)
    if saved.get("path_mappings") != before:
        paths.reresolve_async()
    return {"settings": _mask(saved)}


@app.get("/api/paths/check", dependencies=[Depends(auth)])
def paths_check(limit: int = 25):
    return paths.check(min(limit, 100))


def _mask(s: dict) -> dict:
    for svc in ("sonarr", "radarr", "bazarr", "tautulli"):
        if s.get(svc, {}).get("api_key"):
            s[svc]["api_key"] = "********"
    return s


@app.post("/api/settings/test/{service}", dependencies=[Depends(auth)])
def settings_test(service: str, body: dict = Body(default={})):
    if service not in arr.CLIENTS:
        raise HTTPException(404)
    return arr.test(service, body.get("url"), body.get("api_key"))


@app.post("/api/whisper/test", dependencies=[Depends(auth)])
def whisper_test():
    import numpy as np

    try:
        t0 = time.time()
        stt.WHISPER.detect_language(np.zeros(16000, dtype=np.float32))
        return {"ok": True, "device": stt.WHISPER.device, "model": stt.WHISPER.model_name,
                "seconds": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ====================================================================== webhooks

@app.post("/api/webhook/{source}", dependencies=[Depends(auth)])
def webhook(source: str, payload: dict = Body(...)):
    if source not in ("sonarr", "radarr"):
        raise HTTPException(404)
    ev = payload.get("eventType", "")
    if ev == "Test":
        db.log(f"{source} webhook test received")
        return {"ok": True}
    if ev not in ("Download", "EpisodeFileDelete", "MovieFileDelete", "Rename"):
        return {"ok": True, "ignored": ev}
    try:
        if source == "sonarr":
            arr_id = payload["series"]["id"]
            sync.sync_one("sonarr", arr_id)
            file_arr_id = (payload.get("episodeFile") or {}).get("id")
        else:
            arr_id = payload["movie"]["id"]
            sync.sync_one("radarr", arr_id)
            file_arr_id = (payload.get("movieFile") or {}).get("id")
    except (KeyError, arr.ServiceError) as e:
        db.log(f"{source} webhook sync failed: {e}", "warning")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    queued = 0
    cfg = settings.get()["imports"]
    if ev == "Download" and file_arr_id and cfg.get("enabled"):
        f = db.q1("SELECT f.id FROM files f JOIN items i ON i.id=f.item_id WHERE i.source=? AND i.arr_id=? AND "
                  "f.arr_file_id=?", (source, arr_id, file_arr_id))
        if f:
            queued = scheduler.enqueue([f["id"]], cfg.get("profile", "quick"), "import")
            db.log(f"Import from {source}: queued file {f['id']} for {cfg.get('profile')} scan")
    return {"ok": True, "queued": queued}


# ====================================================================== MCP shim + UI

@app.get("/mcp/anotherr_mcp.py")
def mcp_download():
    return FileResponse(MCP_SCRIPT, media_type="text/x-python", filename="anotherr_mcp.py")


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@app.middleware("http")
async def ui_revalidate(request: Request, call_next):
    """Make browsers check for a newer UI after every update (cheap 304 when nothing changed)."""
    resp = await call_next(request)
    if not request.url.path.startswith(("/api/", "/mcp/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=os.path.join(HERE, "web"), html=True), name="web")
