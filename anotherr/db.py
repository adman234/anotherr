"""SQLite storage. One connection per thread, WAL mode, autocommit."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

CONFIG_DIR = os.environ.get("ANOTHERR_CONFIG", "/config")
DB_PATH = os.environ.get("ANOTHERR_DB", os.path.join(CONFIG_DIR, "anotherr.db"))

_local = threading.local()
_write_lock = threading.RLock()

SEVERITIES = ["info", "warning", "error", "critical"]
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,              -- sonarr | radarr
    arr_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    kind TEXT NOT NULL,                -- series | movie
    is_anime INTEGER DEFAULT 0,
    original_language TEXT DEFAULT '', -- ISO 639-2 (e.g. jpn)
    original_language_name TEXT DEFAULT '',
    runtime INTEGER DEFAULT 0,         -- minutes (per episode for series)
    language_override TEXT DEFAULT '', -- user override of preferred audio language
    multi_segment INTEGER DEFAULT 0,   -- user flag: files usually hold several segments
    excluded INTEGER DEFAULT 0,
    monitored INTEGER DEFAULT 1,
    path TEXT DEFAULT '',
    extra TEXT DEFAULT '{}',
    updated_at REAL,
    UNIQUE(source, arr_id)
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    arr_file_id INTEGER NOT NULL,
    arr_path TEXT NOT NULL,
    local_path TEXT DEFAULT '',
    disk TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    episodes TEXT DEFAULT '[]',
    label TEXT DEFAULT '',
    release_group TEXT DEFAULT '',
    quality TEXT DEFAULT '',
    date_added TEXT DEFAULT '',
    first_seen REAL,
    last_scan_at REAL,
    last_scan_id INTEGER,
    last_profile TEXT DEFAULT '',
    status TEXT DEFAULT 'unscanned',   -- unscanned | ok | info | warning | error | critical | failed
    open_issues INTEGER DEFAULT 0,
    missing INTEGER DEFAULT 0,
    UNIQUE(item_id, arr_file_id)
);
CREATE INDEX IF NOT EXISTS files_item ON files(item_id);
CREATE INDEX IF NOT EXISTS files_disk ON files(disk);
CREATE INDEX IF NOT EXISTS files_scan ON files(last_scan_at);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    profile TEXT, seed INTEGER, source TEXT,
    started_at REAL, finished_at REAL,
    status TEXT,                       -- running | done | failed | cancelled
    result TEXT DEFAULT '{}',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS scans_file ON scans(file_id);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    scan_id INTEGER,
    category TEXT, code TEXT, severity TEXT, message TEXT,
    details TEXT DEFAULT '{}',
    status TEXT DEFAULT 'open',        -- open | resolved | ignored
    created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS issues_file ON issues(file_id);
CREATE INDEX IF NOT EXISTS issues_status ON issues(status);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    scan_id INTEGER,
    kind TEXT, question TEXT,
    timestamps TEXT DEFAULT '[]',
    context TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',     -- pending | done | dismissed
    verdict TEXT DEFAULT '', notes TEXT DEFAULT '',
    created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS reviews_status ON reviews(status);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT,                         -- resub | redownload
    reason TEXT, codes TEXT DEFAULT '[]',
    status TEXT DEFAULT 'proposed',    -- proposed | running | done | failed | rejected
    auto INTEGER DEFAULT 0,
    result TEXT DEFAULT '',
    created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS actions_status ON actions(status);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    profile TEXT, source TEXT,
    created_at REAL,
    UNIQUE(file_id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY, ts REAL, level TEXT, message TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


# Columns added after the first release (CREATE TABLE IF NOT EXISTS won't add them to old DBs)
_ADDED_COLUMNS = [
    ("files", "duration", "REAL"),  # seconds, from ffprobe; feeds the per-series runtime estimate
]


def init() -> None:
    with _write_lock:
        c = conn()
        c.executescript(SCHEMA)
        for table, col, typ in _ADDED_COLUMNS:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def q(sql: str, args: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple | list = ()) -> dict | None:
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def scalar(sql: str, args: tuple | list = ()) -> Any:
    r = conn().execute(sql, args).fetchone()
    return r[0] if r else None


def x(sql: str, args: tuple | list = ()) -> int:
    with _write_lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid or cur.rowcount


@contextmanager
def tx():
    with _write_lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def j(v: Any) -> str:
    return json.dumps(v, default=str)


def unj(v: str | None, default: Any = None) -> Any:
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def log(message: str, level: str = "info") -> None:
    import logging

    from .redact import redact

    message = redact(message)

    logging.getLogger("anotherr").log(
        {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO), message
    )
    try:
        x("INSERT INTO activity(ts, level, message) VALUES (?,?,?)", (time.time(), level, message))
        x("DELETE FROM activity WHERE id < (SELECT MAX(id) - 2000 FROM activity)")
    except Exception:
        pass


def worst(sevs) -> str | None:
    sevs = [s for s in sevs if s in SEV_RANK]
    return max(sevs, key=lambda s: SEV_RANK[s]) if sevs else None
