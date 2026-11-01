"""Mirror Sonarr/Radarr libraries into the local DB."""
from __future__ import annotations

import logging
import threading
import time

from . import arr, db, langs, settings
from .unraid import locate

log = logging.getLogger("anotherr.sync")
_lock = threading.Lock()
STATE: dict = {"running": False, "last": None, "last_error": "", "progress": ""}


def map_path(p: str) -> str:
    best = None
    for m in settings.get().get("path_mappings", []):
        src = (m.get("from") or "").rstrip("/")
        if src and (p == src or p.startswith(src + "/")):
            if best is None or len(src) > len(best[0]):
                best = (src, (m.get("to") or "").rstrip("/"))
    return best[1] + p[len(best[0]):] if best else p


def _quality(q) -> str:
    try:
        return q["quality"]["name"]
    except (TypeError, KeyError):
        return ""


def _upsert_item(source, arr_id, title, year, kind, is_anime, olang, runtime, monitored, path, extra) -> int:
    now = time.time()
    code = langs.norm((olang or {}).get("name", "")) if isinstance(olang, dict) else langs.norm(olang)
    oname = (olang or {}).get("name", "") if isinstance(olang, dict) else (olang or "")
    if oname.lower() in ("any", "unknown", "original"):
        code = ""
    db.x("""INSERT INTO items(source, arr_id, title, year, kind, is_anime, original_language, original_language_name,
            runtime, monitored, path, extra, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, arr_id) DO UPDATE SET title=excluded.title, year=excluded.year,
            is_anime=excluded.is_anime, original_language=excluded.original_language,
            original_language_name=excluded.original_language_name, runtime=excluded.runtime,
            monitored=excluded.monitored, path=excluded.path, extra=excluded.extra, updated_at=excluded.updated_at""",
         (source, arr_id, title, year, kind, int(is_anime), code, oname, runtime or 0, int(monitored), path,
          db.j(extra), now))
    return db.scalar("SELECT id FROM items WHERE source=? AND arr_id=?", (source, arr_id))


def _upsert_file(item_id, arr_file_id, arr_path, size, episodes, label, group, quality, date_added) -> int:
    now = time.time()
    existing = db.q1("SELECT id, size, arr_path FROM files WHERE item_id=? AND arr_file_id=?", (item_id, arr_file_id))
    local = map_path(arr_path)
    if existing and existing["size"] == size and existing["arr_path"] == arr_path:
        db.x("UPDATE files SET episodes=?, label=?, release_group=?, quality=?, missing=0 WHERE id=?",
             (db.j(episodes), label, group, quality, existing["id"]))
        return existing["id"]
    disk, read_path = locate(local)
    if existing:  # file was replaced in place: reset scan state
        db.x("""UPDATE files SET arr_path=?, local_path=?, disk=?, size=?, episodes=?, label=?, release_group=?,
                quality=?, date_added=?, missing=0, status='unscanned', last_scan_at=NULL, open_issues=0 WHERE id=?""",
             (arr_path, read_path, disk, size, db.j(episodes), label, group, quality, date_added, existing["id"]))
        db.x("UPDATE issues SET status='resolved', updated_at=? WHERE file_id=? AND status='open'", (now, existing["id"]))
        return existing["id"]
    return db.x("""INSERT INTO files(item_id, arr_file_id, arr_path, local_path, disk, size, episodes, label,
                   release_group, quality, date_added, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, arr_file_id, arr_path, read_path, disk, size, db.j(episodes), label, group, quality,
                 date_added, now))


def _mark_missing(item_id: int, seen: set[int]):
    rows = db.q("SELECT id, arr_file_id FROM files WHERE item_id=? AND missing=0", (item_id,))
    for r in rows:
        if r["arr_file_id"] not in seen:
            db.x("UPDATE files SET missing=1 WHERE id=?", (r["id"],))
            db.x("UPDATE issues SET status='resolved', updated_at=? WHERE file_id=? AND status='open'",
                 (time.time(), r["id"]))
            db.x("UPDATE reviews SET status='dismissed' WHERE file_id=? AND status='pending'", (r["id"],))
            db.x("DELETE FROM queue WHERE file_id=?", (r["id"],))


def _ep_label(eps: list[dict]) -> str:
    if not eps:
        return ""
    eps = sorted(eps, key=lambda e: (e["season"], e["episode"]))
    first, last = eps[0], eps[-1]
    lab = f"S{first['season']:02d}E{first['episode']:02d}"
    if len(eps) > 1:
        lab += f"-E{last['episode']:02d}" if last["season"] == first["season"] else f"-S{last['season']:02d}E{last['episode']:02d}"
    return lab


def sync_series(s: arr.Sonarr, series: dict) -> int:
    item_id = _upsert_item(
        "sonarr", series["id"], series.get("title", "?"), series.get("year"), "series",
        series.get("seriesType") == "anime", series.get("originalLanguage"), series.get("runtime"),
        series.get("monitored", True), series.get("path", ""),
        {"tvdbId": series.get("tvdbId"), "imdbId": series.get("imdbId"), "network": series.get("network"),
         "genres": series.get("genres", [])})
    if not (series.get("statistics") or {}).get("episodeFileCount", 1):
        _mark_missing(item_id, set())
        return 0
    eps = s.episodes(series["id"]) or []
    files = s.episode_files(series["id"]) or []
    by_file: dict[int, list[dict]] = {}
    for e in eps:
        if e.get("episodeFileId"):
            by_file.setdefault(e["episodeFileId"], []).append({
                "id": e["id"], "season": e.get("seasonNumber", 0), "episode": e.get("episodeNumber", 0),
                "abs": e.get("absoluteEpisodeNumber"), "title": e.get("title", ""), "runtime": e.get("runtime") or 0})
    seen = set()
    for f in files:
        seen.add(f["id"])
        episodes = sorted(by_file.get(f["id"], []), key=lambda e: (e["season"], e["episode"]))
        _upsert_file(item_id, f["id"], f.get("path", ""), f.get("size", 0), episodes, _ep_label(episodes),
                     f.get("releaseGroup") or "", _quality(f.get("quality")), f.get("dateAdded", ""))
    _mark_missing(item_id, seen)
    return len(files)


def sync_movie(m: dict) -> int:
    genres = m.get("genres") or []
    olang = m.get("originalLanguage") or {}
    is_anime = "Animation" in genres and langs.norm(olang.get("name", "")) == "jpn"
    item_id = _upsert_item("radarr", m["id"], m.get("title", "?"), m.get("year"), "movie", is_anime, olang,
                           m.get("runtime"), m.get("monitored", True), m.get("path", ""),
                           {"tmdbId": m.get("tmdbId"), "imdbId": m.get("imdbId"), "genres": genres})
    mf = m.get("movieFile")
    if not mf:
        _mark_missing(item_id, set())
        return 0
    _upsert_file(item_id, mf["id"], mf.get("path", ""), mf.get("size", 0), [], "", mf.get("releaseGroup") or "",
                 _quality(mf.get("quality")), mf.get("dateAdded", ""))
    _mark_missing(item_id, {mf["id"]})
    return 1


def full_sync() -> dict:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "sync already running"}
    STATE.update(running=True, last_error="", progress="starting")
    counts = {"series": 0, "episode_files": 0, "movies": 0, "errors": []}
    try:
        s = arr.Sonarr()
        if s.configured:
            try:
                all_series = s.series() or []
                live = set()
                for i, ser in enumerate(all_series):
                    STATE["progress"] = f"Sonarr {i + 1}/{len(all_series)}: {ser.get('title')}"
                    live.add(ser["id"])
                    try:
                        counts["episode_files"] += sync_series(s, ser)
                        counts["series"] += 1
                    except arr.ServiceError as e:
                        counts["errors"].append(str(e))
                _drop_items("sonarr", live)
            except arr.ServiceError as e:
                counts["errors"].append(str(e))
        r = arr.Radarr()
        if r.configured:
            try:
                movies = r.movies() or []
                STATE["progress"] = f"Radarr: {len(movies)} movies"
                for mv in movies:
                    counts["movies"] += sync_movie(mv)
                _drop_items("radarr", {mv["id"] for mv in movies})
            except arr.ServiceError as e:
                counts["errors"].append(str(e))
        STATE["last"] = time.time()
        settings.state_set("last_sync", STATE["last"])
        db.log(f"Sync done: {counts['series']} series / {counts['episode_files']} episode files, "
               f"{counts['movies']} movies" + (f", {len(counts['errors'])} errors" if counts["errors"] else ""),
               "warning" if counts["errors"] else "info")
        if counts["errors"]:
            STATE["last_error"] = counts["errors"][0]
        return {"ok": True, **counts}
    finally:
        STATE.update(running=False, progress="")
        _lock.release()


def _drop_items(source: str, live: set[int]):
    for row in db.q("SELECT id, arr_id FROM items WHERE source=?", (source,)):
        if row["arr_id"] not in live:
            db.x("DELETE FROM items WHERE id=?", (row["id"],))


def sync_one(source: str, arr_id: int) -> None:
    if source == "sonarr":
        s = arr.Sonarr()
        sync_series(s, s.series(arr_id))
    else:
        sync_movie(arr.Radarr().movies(arr_id))
