"""Frame images for reviews, evidence and the MCP bridge - cached on disk, extracted with bounded concurrency.

Every uncached frame is an ffmpeg process that decodes from the previous keyframe: ~250-450 MB for a 4K
HEVC file, more on CPUs with many cores. The Reviews page and MCP clients can ask for dozens at once,
which used to start dozens of ffmpeg processes and get the container OOM-killed. Now:
  * frames for reviews/evidence are rendered during the scan (the file is open, the disk is spinning)
  * served frames come from the cache whenever possible
  * at most MAX_LIVE extractions run at a time, each with a small thread count
"""
from __future__ import annotations

import os
import threading
import time

from . import db, media

CACHE_DIR = os.path.join(db.CONFIG_DIR, "cache", "frames")
MAX_LIVE = 2
THREADS = 2
CAP_BYTES = 1024 * 1024 * 1024
BASE_WIDTH = 768
_sem = threading.BoundedSemaphore(MAX_LIVE)


def _width(w: int) -> int:
    w = int(w or BASE_WIDTH)
    return BASE_WIDTH if w <= BASE_WIDTH else min(w, 1920)  # small requests reuse the standard size


def cache_path(f: dict, t: float, w: int) -> str:
    return os.path.join(CACHE_DIR, f"{f['id']}-{f.get('size') or 0}", f"{int(round(t * 10))}-{_width(w)}.jpg")


def cached(f: dict, t: float, w: int = BASE_WIDTH) -> str | None:
    p = cache_path(f, t, w)
    return p if os.path.exists(p) else None


def extract(f: dict, path: str, t: float, w: int = BASE_WIDTH, wait: float = 120) -> bytes:
    """Cached frame, or extract one (waiting for a free extraction slot) and cache it."""
    p = cache_path(f, t, w)
    if os.path.exists(p):
        with open(p, "rb") as fh:
            return fh.read()
    if not _sem.acquire(timeout=wait):
        raise TimeoutError("frame extraction busy")
    try:
        if os.path.exists(p):  # another request made it while we waited
            with open(p, "rb") as fh:
                return fh.read()
        data = media.extract_frame(path, t, _width(w), threads=THREADS)
    finally:
        _sem.release()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, p)
    return data


def prerender(f: dict, path: str, timestamps: list[float]) -> int:
    n = 0
    for t in timestamps:
        try:
            if not cached(f, t):
                extract(f, path, t)
                n += 1
        except Exception:  # noqa: BLE001 - a missing frame is shown as an error later, never fatal
            pass
    return n


def prune(cap: int = CAP_BYTES) -> int:
    """Delete the least recently used frames once the cache is over its cap."""
    files = []
    for root, _, names in os.walk(CACHE_DIR):
        for n in names:
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((max(st.st_atime, st.st_mtime), st.st_size, p))
    total = sum(s for _, s, _ in files)
    removed = 0
    if total <= cap:
        return 0
    for _, size, p in sorted(files):
        if total <= cap * 0.8:
            break
        try:
            os.remove(p)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def drop_file(file_id: int):
    """Remove cached frames for a file (e.g. it was replaced)."""
    import shutil

    if not os.path.isdir(CACHE_DIR):
        return
    for d in os.listdir(CACHE_DIR):
        if d.split("-")[0] == str(file_id):
            shutil.rmtree(os.path.join(CACHE_DIR, d), ignore_errors=True)


_touch_at: dict[str, float] = {}


def touch(p: str):
    """Mark a cached frame as used (atime is often disabled on Unraid mounts)."""
    if time.time() - _touch_at.get(p, 0) > 3600:
        _touch_at[p] = time.time()
        try:
            os.utime(p)
        except OSError:
            pass
