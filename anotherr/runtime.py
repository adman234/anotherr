"""Expected per-episode runtime, learned from the series' own files.

Sonarr's runtime is per *numbered* episode. Shows numbered by 11-13 minute segments (Daniel Tiger,
SpongeBob, Courage) often carry a ~30 minute runtime, which made every correct two-segment file
look half as long as it should. The series' files are a better reference: the median of
duration / mapped episodes across them.
"""
from __future__ import annotations

import os
import statistics
import subprocess

from . import db, media

MIN_SAMPLES = 5
PROBE_BUDGET = 8      # extra ffprobe calls per scan to learn a new series
AGREE = 0.25          # metadata is trusted when it is within 25% of the files


def probe_duration(path: str) -> float:
    try:
        p = media.run([media.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                      timeout=30)
        return float((p.stdout or "0").strip() or 0)
    except (subprocess.TimeoutExpired, ValueError):
        return 0.0


def _samples(item_id: int, exclude_id: int) -> list[float]:
    rows = db.q("SELECT duration, episodes FROM files WHERE item_id=? AND id!=? AND missing=0 AND duration > 0",
                (item_id, exclude_id))
    out = []
    for r in rows:
        n = len(db.unj(r["episodes"], []) or [])
        if n:
            out.append(r["duration"] / 60.0 / n)
    return out


def _learn(item_id: int, exclude_id: int, need: int) -> int:
    """Probe a few unprobed sibling files (header read only) so a new series has a reference."""
    from .scanner import resolve_path

    rows = db.q("SELECT * FROM files WHERE item_id=? AND id!=? AND missing=0 AND duration IS NULL "
                "ORDER BY label LIMIT ?", (item_id, exclude_id, min(need, PROBE_BUDGET)))
    n = 0
    for f in rows:
        path = resolve_path(f)
        if path and os.path.exists(path):
            d = probe_duration(path)
            if d > 0:
                db.x("UPDATE files SET duration=? WHERE id=?", (d, f["id"]))
                n += 1
    return n


def metadata_minutes(item: dict, episodes: list[dict]) -> float:
    per = [e.get("runtime") for e in episodes if e.get("runtime")]
    if per:
        return float(statistics.mean(per))
    return float(item.get("runtime") or 0)


def estimate(item: dict, file: dict, episodes: list[dict], learn: bool = True) -> dict:
    """Per-episode runtime (minutes) for this file's series, with where it came from."""
    meta = metadata_minutes(item, episodes)
    samples = _samples(item["id"], file["id"])
    if len(samples) < MIN_SAMPLES and learn:
        if _learn(item["id"], file["id"], MIN_SAMPLES - len(samples) + 3):
            samples = _samples(item["id"], file["id"])
    out = {"metadata_min": round(meta, 2), "samples": len(samples), "per_episode_min": meta or None,
           "source": "metadata" if meta else "none"}
    if len(samples) >= MIN_SAMPLES:
        med = statistics.median(samples)
        out["files_median_min"] = round(med, 2)
        if meta and abs(med / meta - 1) <= AGREE:
            out.update(per_episode_min=meta, source="metadata (matches series files)")
        else:
            out.update(per_episode_min=med, source="series files")
    return out
