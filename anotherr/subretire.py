"""Retire a subtitle sidecar that doesn't match the dialogue while another full track in the same language does.

subs_mismatch_partial means the file already has a good subtitle and one (or more) bad ones next to it: players
may pick the bad one, and Bazarr counts the language as covered. The fix is moving the bad sidecar out of the way
(renamed with the .anotherr-bak suffix, or .anotherr-retired when a shift backup already exists, so the scanner and Plex no longer see it and it can be put back).
Embedded tracks are left alone: removing one needs a remux, and the good track still plays."""
from __future__ import annotations

import os

from . import db
from .subshift import BACKUP_SUFFIX

KIND = "retire_subtitle"
GOOD = ("ok", "offset")
RETIRED_SUFFIX = ".anotherr-retired"


def plan(file_id: int) -> dict:
    """{"retire": [{name, path}], "keep": [names], "embedded": [names]} from the file's latest finished scan."""
    row = db.q1("SELECT result FROM scans WHERE file_id=? AND status='done' AND result LIKE '%subtitle_sync%' "
                "ORDER BY id DESC LIMIT 1", (file_id,))
    sync = (db.unj(row["result"], {}) if row else {}).get("subtitle_sync") or []
    keep = [s.get("track") for s in sync if s.get("verdict_final") in GOOD]
    bad = [s for s in sync if s.get("verdict_final") == "mismatch"]
    if not keep:  # nothing good to fall back on: that's subs_mismatch, a new subtitle is needed instead
        return {"retire": [], "keep": [], "embedded": [s.get("track") for s in bad if not s.get("external")]}
    return {"retire": [{"name": s.get("track"), "path": s["path"]} for s in bad if s.get("external") and s.get("path")],
            "keep": keep, "embedded": [s.get("track") for s in bad if not s.get("external")]}


def run(f: dict) -> str:
    p = plan(f["id"])
    if not p["retire"]:
        why = ("only embedded tracks are bad; they stay (the good track still plays)" if p["embedded"]
               else "no bad subtitle sidecar next to a good track in the last scan (rescan the file first)")
        raise RuntimeError(why)
    done, errors = [], []
    for r in p["retire"]:
        path = r["path"]
        try:
            if not os.path.exists(path):
                raise RuntimeError(f"{os.path.basename(path)}: already gone")
            bak = path + BACKUP_SUFFIX
            if os.path.exists(bak):  # a shift's backup of the original: keep it, park this copy next to it
                bak = path + RETIRED_SUFFIX
            os.replace(path, bak)
            done.append(f"{os.path.basename(path)} moved aside as {os.path.basename(bak)}")
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    if not done:
        raise RuntimeError("; ".join(errors))
    extra = [f"skipped: {e}" for e in errors]
    if p["embedded"]:
        extra.append("embedded " + ", ".join(p["embedded"]) + " left in place")
    return "; ".join(done + extra) + f" (kept: {', '.join(p['keep'])})"
