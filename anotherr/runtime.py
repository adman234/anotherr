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


def _season(episodes: list[dict]) -> int | None:
    seasons = [e.get("season") for e in episodes if e.get("season") is not None]
    return min(seasons) if seasons else None


def _samples(item_id: int, exclude_id: int) -> list[tuple[int | None, float]]:
    """(season, minutes per mapped episode) for the series' other files with a known length."""
    rows = db.q("SELECT duration, episodes FROM files WHERE item_id=? AND id!=? AND missing=0 AND duration > 0",
                (item_id, exclude_id))
    out = []
    for r in rows:
        eps = db.unj(r["episodes"], []) or []
        if eps:
            out.append((_season(eps), r["duration"] / 60.0 / len(eps)))
    return out


def _learn(item_id: int, exclude_id: int, need: int, season: int | None = None) -> int:
    """Probe a few unprobed sibling files (header read only), same season first, so there's a reference."""
    from .scanner import resolve_path

    prefix = f"S{season:02d}E%" if season is not None else "-"
    rows = db.q("SELECT * FROM files WHERE item_id=? AND id!=? AND missing=0 AND duration IS NULL "
                "ORDER BY (label LIKE ?) DESC, label LIMIT ?", (item_id, exclude_id, prefix, min(need, PROBE_BUDGET)))
    n = 0
    for f in rows:
        path = resolve_path(f)
        if path and os.path.exists(path):
            d = probe_duration(path)
            if d > 0:
                db.x("UPDATE files SET duration=? WHERE id=?", (d, f["id"]))
                n += 1
    return n


def metadata(item: dict, episodes: list[dict]) -> tuple[float, str]:
    """Per-episode runtime from Sonarr/Radarr: the episodes' own runtimes (TVDB) when known, else the series'."""
    per = [e.get("runtime") for e in episodes if e.get("runtime")]
    if per:
        return float(statistics.mean(per)), "episode metadata"
    return float(item.get("runtime") or 0), "series metadata"


def estimate(item: dict, file: dict, episodes: list[dict], learn: bool = True) -> dict:
    """Two opinions on the per-episode runtime: this season's (or series') files, and the metadata.

    Episode length often changes between seasons (Run BTS! S1 ran 3-15 min, later seasons 30-40), so the
    file baseline comes from the same season when it has enough known files.
    """
    meta, meta_src = metadata(item, episodes)
    season = _season(episodes)
    samples = _samples(item["id"], file["id"])
    in_season = [m for s, m in samples if s == season and season is not None]
    if len(in_season) < MIN_SAMPLES and learn:
        if _learn(item["id"], file["id"], MIN_SAMPLES - len(in_season) + 3, season):
            samples = _samples(item["id"], file["id"])
            in_season = [m for s, m in samples if s == season and season is not None]
    out = {"metadata_min": round(meta, 2) or None, "metadata_source": meta_src if meta else None,
           "samples": len(samples), "season_samples": len(in_season), "files_min": None, "files_source": None}
    if len(in_season) >= MIN_SAMPLES:
        out.update(files_min=round(statistics.median(in_season), 2), files_source=f"season {season} files")
    elif len(samples) >= MIN_SAMPLES:
        out.update(files_min=round(statistics.median(m for _, m in samples), 2), files_source="series files")
    return out
