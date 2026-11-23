"""Keeps the scans table small.

The reuse cache (expensive measurements a quick rescan can reuse) lives in its own table, one zlib-compressed row
per file, instead of inside the newest scan's result JSON. Old scan results nothing reads any more are emptied
after every scan; the scan rows themselves stay, so the per-file scan list is unchanged.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zlib

from . import db

MIGRATION_KEY = "migrated:scan_store_v1"

# A scan's result is still read when it is the file's last scan, an issue or review points at it, or it is the
# file's newest finished scan with subtitle_sync (subgen / subretire / subshift look that one up).
_KEEP = """
    id NOT IN (SELECT COALESCE(last_scan_id, -1) FROM files WHERE {f})
    AND id NOT IN (SELECT scan_id FROM issues WHERE scan_id IS NOT NULL AND {f})
    AND id NOT IN (SELECT scan_id FROM reviews WHERE scan_id IS NOT NULL AND {f})
    AND id NOT IN (SELECT MAX(id) FROM scans WHERE status='done' AND result LIKE '%subtitle_sync%' AND {f}
                   GROUP BY file_id)
"""


def _pack(payload: dict) -> bytes:
    return zlib.compress(json.dumps(payload, default=str, separators=(",", ":")).encode(), 6)


def save_cache(c: sqlite3.Connection, file_id: int, scan_id: int, payload: dict) -> None:
    """payload: {"cache", "fingerprint", "sampled", "segments"} of the scan that made it."""
    c.execute("INSERT OR REPLACE INTO scan_cache(file_id, scan_id, made_at, data) VALUES (?,?,?,?)",
              (file_id, scan_id, (payload.get("cache") or {}).get("made_at"), _pack(payload)))


def load_cache(file_id: int) -> dict | None:
    row = db.q1("SELECT scan_id, data FROM scan_cache WHERE file_id=?", (file_id,))
    if not row:
        return None
    try:
        payload = json.loads(zlib.decompress(row["data"]))
    except Exception:  # noqa: BLE001  - a damaged row just means a full scan
        return None
    payload["scan_id"] = row["scan_id"]
    return payload


def prune(c: sqlite3.Connection, file_id: int | None = None) -> int:
    """Empty the results nothing reads and drop ffprobe streams from all but the last scan. Returns rows emptied."""
    f, args = ("file_id=?", (file_id,)) if file_id is not None else ("1=1", ())
    keep = _KEEP.format(f=f)
    emptied = c.execute(f"UPDATE scans SET result='{{}}' WHERE {f} AND status<>'running' AND result<>'{{}}' AND {keep}",
                        args * 5).rowcount
    c.execute(f"UPDATE scans SET result=json_remove(result, '$.streams') WHERE {f} AND result LIKE '%\"streams\"%' "
              f"AND id NOT IN (SELECT COALESCE(last_scan_id, -1) FROM files WHERE {f})", args * 2)
    return emptied


def _size_mb() -> float:
    try:
        return sum(os.path.getsize(db.DB_PATH + s) for s in ("", "-wal") if os.path.exists(db.DB_PATH + s)) / 1048576
    except OSError:
        return 0.0


def migrate(vacuum: bool = True) -> dict | None:
    """One-time: move caches out of scan results, prune old results, then VACUUM. Runs before any scan starts."""
    if db.scalar("SELECT value FROM settings WHERE key=?", (MIGRATION_KEY,)):
        return None
    t0, before = time.time(), _size_mb()
    moved = 0
    ids = [r["id"] for r in db.q("SELECT id FROM scans WHERE result LIKE '%\"cache\"%' ORDER BY id")]
    for start in range(0, len(ids), 200):
        with db.tx() as c:
            for sid in ids[start:start + 200]:
                row = c.execute("SELECT file_id, status, result FROM scans WHERE id=?", (sid,)).fetchone()
                res = db.unj(row["result"], {}) or {}
                cache = res.get("cache")
                if row["status"] == "done" and cache:  # ids ascend, so a file's newest cache wins
                    save_cache(c, row["file_id"], sid, {"cache": cache, "fingerprint": res.get("fingerprint"),
                                                        "sampled": res.get("sampled"), "segments": res.get("segments")})
                    moved += 1
                c.execute("UPDATE scans SET result=json_remove(result, '$.cache') WHERE id=?", (sid,))
    with db.tx() as c:
        emptied = prune(c)
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (MIGRATION_KEY, str(time.time())))
    if vacuum:
        c = db.conn()
        with db._write_lock:
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out = {"caches_moved": moved, "results_emptied": emptied, "before_mb": round(before), "after_mb": round(_size_mb()),
           "seconds": round(time.time() - t0)}
    db.log(f"Compacted the database: {out['before_mb']} MB -> {out['after_mb']} MB ({moved} scan caches moved to "
           f"their own table, {emptied} old scan results emptied, {out['seconds']}s)")
    return out
