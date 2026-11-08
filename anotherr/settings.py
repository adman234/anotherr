"""Settings live in the DB (editable from the UI); defaults are merged in on read."""
from __future__ import annotations

import copy
import threading

from . import db

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]  # Monday = 0

# Issue codes that may lead to an action proposal.
RESUB_CODES = ["subs_missing", "subs_signs_only", "subs_out_of_sync", "subs_mismatch", "subs_text_mismatch", "dubtitles"]
REDOWNLOAD_CODES = [
    "unreadable", "no_video", "no_audio", "truncated", "decode_errors_severe", "decode_stopped_early",
    "duration_short", "wrong_content",
    "original_audio_missing", "silent_audio", "visual_review_failed", "full_decode_errors",
]

DEFAULTS: dict = {
    "sonarr": {"url": "", "api_key": ""},
    "radarr": {"url": "", "api_key": ""},
    "bazarr": {"url": "", "api_key": ""},
    "tautulli": {"url": "", "api_key": ""},
    # Map paths as Sonarr/Radarr see them to paths inside this container.
    "path_mappings": [{"from": "/tv", "to": "/mnt/user/media/tv"}, {"from": "/movies", "to": "/mnt/user/media/movies"}],
    "languages": {
        "subtitle_language": "eng",            # your reading language
        "require_subs_for_native_audio": False,  # require subs even when audio is already in subtitle_language
        "min_full_sub_cues_per_min": 3.0,      # below this a track is treated as signs/songs only
    },
    "anime": {
        "prefer_dual_audio": True,
        "dub_language": "eng",
        "detect_dubtitles": True,
    },
    "scan": {
        "default_profile": "standard",
        "hw_decode": "auto",     # auto = NVIDIA GPU (NVDEC) when available, off = CPU only
        "ffmpeg_threads": 0,     # 0 = let ffmpeg decide
        "max_audio_tracks": 4,
        # optional checks (see Dashboard -> Where scan time goes): turn off the ones that cost more than they find
        "checks": {
            "interlace": True,               # idet sample for interlaced video
            "extra_language_samples": True,  # more audio windows before calling the expected language missing
            "hardsubs": True,                # OCR frames for burned-in subtitles
            "subtitle_sync": True,           # compare subtitle timing with speech
            "subtitle_content": True,        # transcribe speech and compare it with the subtitle text
        },
        "profiles": {
            "quick": {"segments": 3, "seconds": 30, "subtitles": False, "full_decode": False},
            "standard": {"segments": 5, "seconds": 90, "subtitles": True, "full_decode": False},
            "deep": {"segments": 8, "seconds": 180, "subtitles": True, "full_decode": True},
        },
    },
    "review": {
        "title_cards": "multi_episode",  # never | multi_episode | flagged | always
        "ocr_title_cards": True,         # read title cards with OCR; matches auto-pass, other episodes flag wrong content
        "visual_quality_on_deep": True,
    },
    "schedule": {
        "enabled": True,              # scheduled (priority) scanning; queued scans always run
        "profile": "standard",
        "max_disks": 1,               # hard drives scanned at the same time
        "cache_slot": True,           # plus one extra slot for cache/SSD pools (no spin-up, no contention)
        "spinup": "anytime",          # waking idle disks for scheduled scans: anytime | windows | never
        "windows": [{"days": ALL_DAYS, "start": "01:00", "end": "07:00"}],  # used when spinup = windows
        "quiet_windows": [],          # no new scans at all, e.g. while scheduled updates/backups run
        "disks": [],                  # optional: only scan these disks (empty = all)
        "rescan_after_days": 180,
        "pause_on_parity": True,
        "pause_on_mover": True,
        "pause_on_streams": True,
        "stream_pause": "off",        # off | same_disk = skip the disk Plex reads | any = pause during streams
        "max_streams": 0,             # 'any' mode: pause when Plex has more than this many active streams
        "manual_respects_pause": True,
    },
    "imports": {"enabled": True, "profile": "quick"},
    "sync": {"interval_minutes": 360},
    "whisper": {
        "model": "large-v3-turbo",
        "device": "auto",          # auto | cuda | cpu
        "compute_type": "auto",    # auto | float16 | int8_float16 | int8
        "cpu_fallback_model": "small",
        "idle_unload_minutes": 10,
    },
    "retag": {
        "enabled": False,            # writing language tags into media files; needs /mnt mounted read/write
        "allowed_paths": [],         # only files under these folders may be edited, e.g. /mnt/user/media/tv
        "break_hardlinks": False,    # hardlinked files (seeding copies): skip (default) or write a new copy
    },
    "subgen": {
        "url": "",                   # e.g. http://192.168.1.10:9006 - empty = the subgen route is off
        "path_map": [],              # [{"from": "/mnt/user/data/media", "to": "/media"}]: library path -> subgen's view
        "timeout_factor": 4,         # per-file budget: running time x this (grows with subgen's measured pace)
        "timeout_min_s": 600,
        "poll_s": 15,
        "max_consecutive_failures": 3,
        "refresh_every": 10,         # Sonarr/Radarr + Bazarr rescan after this many files (and at the end)
        "new_episode_days": 7,       # newer episodes get a Bazarr resub first
        "gpu": False,                # subgen shares the GPU: pause Anotherr's scans while a subgen job runs
        "min_cues_per_min": 3,
        "min_english_score": 0.2,
    },
    "automation": {
        "enabled": False,
        "windows": [{"days": ALL_DAYS, "start": "02:00", "end": "06:00"}],
        "max_actions_per_day": 10,
        "auto_resub": True,
        "auto_redownload": False,
        "redownload_codes": ["unreadable", "no_video", "no_audio", "truncated", "decode_errors_severe", "original_audio_missing"],
        "redownload_delete_file": True,
    },
}

SECRET_KEYS = {"api_key"}

_lock = threading.RLock()
_cache: dict | None = None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _prune(base: dict, cur: dict) -> dict:
    """Drop settings that no longer exist (e.g. retired options still in an old saved config)."""
    out = {}
    for k, v in cur.items():
        if k not in base:
            continue
        if isinstance(v, dict) and isinstance(base[k], dict) and base[k] and k != "profiles":
            out[k] = _prune(base[k], v)
        else:
            out[k] = v
    return out


def _diff(base: dict, cur: dict) -> dict:
    """Only the values that differ from the defaults (so improved defaults reach existing installs)."""
    out = {}
    for k, v in cur.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            d = _diff(base[k], v)
            if d:
                out[k] = d
        elif k not in base or base[k] != v:
            out[k] = v
    return out


# Values that older versions saved as plain defaults; treat them as "not customised".
_OLD_DEFAULTS = [
    (("scan", "ffmpeg_threads"), 2),
    (("scan", "profiles", "standard", "seconds"), 120),
]


def _migrate(stored: dict) -> dict:
    for path, old in _OLD_DEFAULTS:
        node = stored
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and node.get(path[-1]) == old:
            del node[path[-1]]
    return _diff(DEFAULTS, _merge(DEFAULTS, stored))


def get() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            stored = db.unj(db.scalar("SELECT value FROM settings WHERE key='config'"), {})
            if stored and not stored.get("_v"):
                stored = _migrate(stored)
                stored["_v"] = 2
                db.x("INSERT INTO settings(key, value) VALUES('config', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (db.j(stored),))
            stored.pop("_v", None)
            _cache = _prune(DEFAULTS, _merge(DEFAULTS, stored))
        return copy.deepcopy(_cache)


def save(new: dict) -> dict:
    global _cache
    with _lock:
        current = get()
        # Keep existing secrets when the UI sends back a masked value.
        for svc in ("sonarr", "radarr", "bazarr", "tautulli"):
            if svc in new and isinstance(new[svc], dict) and new[svc].get("api_key") in ("********", None):
                new[svc]["api_key"] = current.get(svc, {}).get("api_key", "")
        new.pop("_v", None)
        merged = _prune(DEFAULTS, _merge(current, new))
        db.x("INSERT INTO settings(key, value) VALUES('config', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (db.j({**_diff(DEFAULTS, merged), "_v": 2}),))
        _cache = merged
        return copy.deepcopy(merged)


def masked() -> dict:
    s = get()
    for svc in ("sonarr", "radarr", "bazarr", "tautulli"):
        if s[svc].get("api_key"):
            s[svc]["api_key"] = "********"
    return s


def state_get(key: str, default=None):
    return db.unj(db.scalar("SELECT value FROM settings WHERE key=?", ("state." + key,)), default)


def state_set(key: str, value) -> None:
    db.x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         ("state." + key, db.j(value)))
