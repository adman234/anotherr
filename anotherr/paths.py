"""Path-mapping diagnostics: test a sample of files and suggest mappings by finding where they really live."""
from __future__ import annotations

import os
import threading
import time

from . import db, settings
from .sync import map_path
from .unraid import MNT, locate

_bases_cache: dict = {"t": 0.0, "bases": []}


def _bases() -> list[str]:
    """Candidate host folders: /mnt/user, its shares, and one level below each share."""
    if time.time() - _bases_cache["t"] < 600:
        return _bases_cache["bases"]
    user = os.path.join(MNT, "user")
    bases = [user] if os.path.isdir(user) else []
    try:
        for share in sorted(os.listdir(user)):
            sp = os.path.join(user, share)
            if not os.path.isdir(sp) or share.startswith("."):
                continue
            bases.append(sp)
            try:
                bases += [os.path.join(sp, d) for d in sorted(os.listdir(sp))
                          if not d.startswith(".") and os.path.isdir(os.path.join(sp, d))]
            except OSError:
                pass
    except OSError:
        pass
    _bases_cache.update(t=time.time(), bases=bases)
    return bases


def suggest(arr_path: str) -> dict | None:
    """Find a host folder that contains the tail of arr_path; return the prefix mapping that would fix it."""
    parts = [p for p in arr_path.split("/") if p]
    bases = _bases()
    for i in range(1, len(parts) - 1):  # drop the first i components, keep the longest tail first
        tail = "/".join(parts[i:])
        for base in bases:
            if os.path.exists(os.path.join(base, tail)):
                return {"from": "/" + "/".join(parts[:i]), "to": base}
    return None


def check(limit: int = 25) -> dict:
    rows = db.q("""SELECT f.id, f.arr_path, i.title, i.source FROM files f JOIN items i ON i.id=f.item_id
                   WHERE f.missing=0 GROUP BY f.item_id ORDER BY RANDOM() LIMIT ?""", (limit,))
    samples, sugg = [], {}
    for r in rows:
        mapped = map_path(r["arr_path"])
        disk, read = locate(mapped)
        ok = os.path.exists(read)
        s = None
        if not ok:
            # an earlier suggestion may already explain this file
            for key, v in sugg.items():
                if r["arr_path"].startswith(v["from"] + "/") and \
                        os.path.exists(v["to"] + r["arr_path"][len(v["from"]):]):
                    s = v
                    break
            s = s or suggest(r["arr_path"])
            if s:
                key = (s["from"], s["to"])
                sugg.setdefault(key, {**s, "files": 0})
                sugg[key]["files"] += 1
        samples.append({"title": r["title"], "source": r["source"], "arr_path": r["arr_path"], "mapped": mapped,
                        "read_path": read, "disk": disk, "found": ok, "suggestion": s})
    found = sum(1 for s in samples if s["found"])
    return {"checked": len(samples), "found": found, "mnt_visible": os.path.isdir(os.path.join(MNT, "user")),
            "suggestions": sorted(sugg.values(), key=lambda v: -v["files"]), "samples": samples}


def reresolve() -> dict:
    """After mappings change: recompute paths/disks, and un-fail files that can now be found."""
    from .unraid import _dir_cache

    _dir_cache.clear()
    settings.state_set("paths_changed_at", time.time())
    fixed = moved = 0
    for f in db.q("SELECT id, arr_path, local_path, disk FROM files WHERE missing=0"):
        disk, read = locate(map_path(f["arr_path"]))
        if not os.path.exists(read):
            continue
        if read != f["local_path"] or disk != f["disk"]:
            db.x("UPDATE files SET local_path=?, disk=? WHERE id=?", (read, disk, f["id"]))
            moved += 1
        if db.q1("SELECT id FROM issues WHERE file_id=? AND code='file_not_found' AND status='open'", (f["id"],)):
            db.x("UPDATE issues SET status='resolved', updated_at=? WHERE file_id=? AND code='file_not_found' "
                 "AND status='open'", (time.time(), f["id"]))
            db.x("UPDATE files SET status='unscanned', last_scan_at=NULL, open_issues="
                 "(SELECT COUNT(*) FROM issues WHERE file_id=? AND status='open') WHERE id=?", (f["id"], f["id"]))
            fixed += 1
    db.log(f"Path mappings updated: {moved} file path(s) updated, {fixed} previously missing file(s) found")
    return {"updated": moved, "found_again": fixed}


def reresolve_async():
    threading.Thread(target=reresolve, daemon=True, name="anotherr-reresolve").start()


def mappings_look_wrong() -> bool:
    """True when the last 5 scans since the mappings last changed all failed with file_not_found."""
    since = float(settings.state_get("paths_changed_at", 0) or 0)
    rows = db.q("""SELECT s.id, EXISTS(SELECT 1 FROM issues x WHERE x.scan_id=s.id AND x.code='file_not_found') nf
                   FROM scans s WHERE s.status='done' AND s.started_at > ? ORDER BY s.id DESC LIMIT 5""", (since,))
    return len(rows) == 5 and all(r["nf"] for r in rows)
