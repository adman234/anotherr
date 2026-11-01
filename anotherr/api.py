"""FastAPI app: REST API, webhooks, web UI, MCP shim download."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, actions, arr, db, media, scanner, scheduler, settings, stt, sync, unraid

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


app = FastAPI(title="Anotherr", version=__version__, lifespan=lifespan)


def auth(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("x-api-key") or request.query_params.get("apikey")
    if key != API_KEY:
        raise HTTPException(401, "invalid or missing API key")


def _startup():
    db.init()
    db.x("UPDATE scans SET status='failed', error='interrupted by restart' WHERE status='running'")
    db.x("UPDATE actions SET status='failed', result='interrupted by restart' WHERE status='running'")
    if os.environ.get("ANOTHERR_NO_WORKER") != "1":
        scheduler.start()
    db.log(f"Anotherr {__version__} started")


def page(sql_base: str, args: list, page_no: int, per: int, order: str) -> dict:
    total = db.scalar(f"SELECT COUNT(*) FROM ({sql_base})", args)
    rows = db.q(f"{sql_base} ORDER BY {order} LIMIT ? OFFSET ?", args + [per, (page_no - 1) * per])
    return {"total": total, "page": page_no, "per_page": per, "rows": rows}


# ====================================================================== status

@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM files WHERE missing=0 GROUP BY status")}
    cfg = settings.get()
    return {
        "version": __version__,
        "current": dict(scanner.CURRENT),
        "scheduler": dict(scheduler.STATUS),
        "paused": bool(settings.state_get("paused", False)),
        "override_until": (lambda v: v if v > time.time() else 0)(float(settings.state_get("override_until", 0) or 0)),
        "sync": {**sync.STATE, "last": sync.STATE.get("last") or settings.state_get("last_sync")},
        "whisper": {"loaded": stt.WHISPER._model is not None, "device": stt.WHISPER.device,
                    "model": stt.WHISPER.model_name or cfg["whisper"]["model"], "error": stt.WHISPER.load_error},
        "files": {"total": sum(counts.values()), **counts},
        "queue": db.scalar("SELECT COUNT(*) FROM queue"),
        "open_issues": db.scalar("SELECT COUNT(*) FROM issues WHERE status='open'"),
        "pending_reviews": db.scalar("SELECT COUNT(*) FROM reviews WHERE status='pending'"),
        "proposed_actions": db.scalar("SELECT COUNT(*) FROM actions WHERE status='proposed'"),
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
            SUM(f.status IN ('error','critical','failed')) bad
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
        SUM(f.status IN ('error','critical','failed')) bad, SUM(f.status IN ('warning')) warn
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
    return it


@app.patch("/api/items/{item_id}", dependencies=[Depends(auth)])
def item_update(item_id: int, body: dict = Body(...)):
    allowed = {"language_override", "multi_segment", "excluded", "is_anime"}
    sets = {k: v for k, v in body.items() if k in allowed}
    if "language_override" in sets:
        from . import langs

        v = sets["language_override"] or ""
        sets["language_override"] = langs.norm(v) if v else ""
        if v and not sets["language_override"]:
            raise HTTPException(400, f"unknown language '{v}'")
    for k, v in sets.items():
        db.x(f"UPDATE items SET {k}=? WHERE id=?", (v, item_id))
    return item(item_id)


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
    return f


@app.get("/api/files/{file_id}/frame", dependencies=[Depends(auth)])
def frame(file_id: int, t: float, w: int = 768):
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise HTTPException(404)
    path = scanner.resolve_path(f)
    if not path:
        raise HTTPException(404, "file not found on disk")
    try:
        return Response(media.extract_frame(path, t, max(160, min(w, 1920))), media_type="image/jpeg")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


# ====================================================================== scanning

@app.post("/api/scan", dependencies=[Depends(auth)])
def scan(body: dict = Body(...)):
    """scope: file | item | disk | library | random | problems | unscanned"""
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
    n = scheduler.enqueue(fids, profile, "manual")
    db.log(f"Queued {n} file(s) for {profile} scan ({scope})")
    return {"queued": n, "file_ids": fids[:200]}


@app.post("/api/scan/stop", dependencies=[Depends(auth)])
def scan_stop():
    scanner.CANCEL.set()
    return {"ok": True}


@app.get("/api/queue", dependencies=[Depends(auth)])
def queue_list(limit: int = 200):
    return db.q("""SELECT q.*, i.title, f.label FROM queue q JOIN files f ON f.id=q.file_id JOIN items i ON i.id=f.item_id
        ORDER BY CASE q.source WHEN 'manual' THEN 0 WHEN 'import' THEN 1 ELSE 2 END, q.created_at LIMIT ?""", (limit,))


@app.delete("/api/queue", dependencies=[Depends(auth)])
def queue_clear():
    return {"removed": db.x("DELETE FROM queue")}


@app.delete("/api/queue/{qid}", dependencies=[Depends(auth)])
def queue_remove(qid: int):
    return {"removed": db.x("DELETE FROM queue WHERE id=?", (qid,))}


@app.post("/api/control/pause", dependencies=[Depends(auth)])
def control_pause(body: dict = Body(...)):
    settings.state_set("paused", bool(body.get("paused")))
    db.log("Scanning paused" if body.get("paused") else "Scanning resumed")
    return {"paused": bool(body.get("paused"))}


@app.post("/api/control/run-now", dependencies=[Depends(auth)])
def control_run_now(body: dict = Body(default={})):
    """Run scheduled (priority) scanning outside the scan windows for N hours. hours=0 cancels."""
    hours = float(body.get("hours", 2))
    until = time.time() + hours * 3600 if hours > 0 else 0
    settings.state_set("override_until", until)
    if until and settings.state_get("paused", False):
        settings.state_set("paused", False)
    db.log(f"Run-now override until {time.strftime('%H:%M', time.localtime(until))}" if until else "Run-now override cancelled")
    return {"override_until": until}


@app.post("/api/sync", dependencies=[Depends(auth)])
def sync_now():
    if sync.STATE["running"]:
        return {"ok": False, "error": "already running"}
    threading.Thread(target=sync.full_sync, daemon=True).start()
    return {"ok": True}


# ====================================================================== issues / reviews / actions

ISSUE_SQL = """SELECT x.*, i.title, f.label, f.disk, i.id AS item_id FROM issues x JOIN files f ON f.id=x.file_id
    JOIN items i ON i.id=f.item_id WHERE f.missing=0"""


@app.get("/api/issues", dependencies=[Depends(auth)])
def issues(status: str = "open", severity: str = "", code: str = "", category: str = "", search: str = "",
           page_no: int = Query(1, alias="page"), per_page: int = 100):
    sql, args = ISSUE_SQL, []
    for col, val in (("x.status", status), ("x.severity", severity), ("x.code", code), ("x.category", category)):
        if val:
            vals = val.split(",")
            sql += f" AND {col} IN ({','.join('?' * len(vals))})"
            args += vals
    if search:
        sql += " AND (i.title LIKE ? OR x.message LIKE ?)"
        args += [f"%{search}%"] * 2
    res = page(sql, args, page_no, min(per_page, 500),
               "CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, id DESC")
    for r in res["rows"]:
        r["details"] = db.unj(r["details"], {})
    res["codes"] = db.q("SELECT code, severity, COUNT(*) n FROM issues WHERE status='open' GROUP BY code, severity "
                        "ORDER BY n DESC")
    return res


@app.patch("/api/issues/{issue_id}", dependencies=[Depends(auth)])
def issue_update(issue_id: int, body: dict = Body(...)):
    st = body.get("status")
    if st not in ("open", "ignored", "resolved"):
        raise HTTPException(400, "status must be open | ignored | resolved")
    db.x("UPDATE issues SET status=?, updated_at=? WHERE id=?", (st, time.time(), issue_id))
    fid = db.scalar("SELECT file_id FROM issues WHERE id=?", (issue_id,))
    _refresh_file_status(fid)
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
def reviews(status: str = "pending", kind: str = "", limit: int = 50):
    sql, args = REVIEW_SQL, []
    if status:
        sql += " AND r.status=?"
        args.append(status)
    if kind:
        sql += " AND r.kind=?"
        args.append(kind)
    rows = db.q(sql + " ORDER BY r.id LIMIT ?", args + [min(limit, 500)])
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


ACTION_SQL = """SELECT a.*, i.title, f.label FROM actions a JOIN files f ON f.id=a.file_id JOIN items i ON i.id=f.item_id"""


@app.get("/api/actions", dependencies=[Depends(auth)])
def actions_list(status: str = "proposed", limit: int = 200):
    sql, args = ACTION_SQL, []
    if status:
        vals = status.split(",")
        sql += f" WHERE a.status IN ({','.join('?' * len(vals))})"
        args += vals
    rows = db.q(sql + " ORDER BY a.id DESC LIMIT ?", args + [min(limit, 1000)])
    for r in rows:
        r["codes"] = db.unj(r["codes"], [])
    return rows


@app.post("/api/actions", dependencies=[Depends(auth)])
def action_create(body: dict = Body(...)):
    kind = body.get("kind")
    if kind not in ("resub", "redownload"):
        raise HTTPException(400, "kind must be resub | redownload")
    aid = actions.propose(int(body["file_id"]), kind, body.get("reason") or "manual")
    return {"action_id": aid, "note": None if aid else "an open proposal of this kind already exists"}


@app.post("/api/actions/{action_id}/approve", dependencies=[Depends(auth)])
def action_approve(action_id: int):
    return actions.execute(action_id)


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
    return {"settings": _mask(settings.save(body))}


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


app.mount("/", StaticFiles(directory=os.path.join(HERE, "web"), html=True), name="web")
