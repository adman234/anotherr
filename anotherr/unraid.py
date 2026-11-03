"""Unraid awareness: physical disk lookup, spin state, parity checks.

Expects the host's /mnt mounted at /mnt (read-only, slave) and, optionally,
/var/local/emhttp mounted at /unraid/emhttp (read-only).
"""
from __future__ import annotations

import os
import re
import time
from functools import lru_cache

MNT = os.environ.get("ANOTHERR_MNT", "/mnt")
EMHTTP = os.environ.get("ANOTHERR_EMHTTP", "/unraid/emhttp")
_SKIP = {"user", "user0", "disks", "remotes", "addons", "rootshare", "RecycleBin"}


def _ini(path: str) -> dict[str, dict[str, str]]:
    """Parse Unraid's emhttp ini files (quoted section names and values)."""
    out: dict[str, dict[str, str]] = {}
    sec = out.setdefault("", {})
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(";"):
                    continue
                m = re.match(r'^\["?([^"\]]+)"?\]$', line)
                if m:
                    sec = out.setdefault(m.group(1), {})
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    sec[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def disk_roots() -> list[str]:
    """Physical array disks and pools (e.g. /mnt/disk1, /mnt/cache)."""
    try:
        names = sorted(os.listdir(MNT))
    except OSError:
        return []
    roots = []
    for n in names:
        if n in _SKIP or n.startswith("."):
            continue
        p = os.path.join(MNT, n)
        if os.path.isdir(p):
            roots.append(p)

    def key(p: str):
        b = os.path.basename(p)
        m = re.match(r"disk(\d+)$", b)
        return (0, int(m.group(1)), b) if m else (1, 0, b)

    return sorted(roots, key=key)


_dir_cache: dict[str, tuple[float, list[str]]] = {}


def _disks_with_dir(rel_dir: str) -> list[str]:
    hit = _dir_cache.get(rel_dir)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    found = [r for r in disk_roots() if os.path.isdir(os.path.join(r, rel_dir))]
    _dir_cache[rel_dir] = (time.time(), found)
    return found


def locate(path: str) -> tuple[str, str]:
    """Return (disk_name, best_read_path) for a /mnt/user path.

    Reads go straight to /mnt/diskN when possible (bypasses shfs overhead).
    The parent directory is checked first so we rarely stat every disk.
    """
    for share in ("/user/", "/user0/"):
        prefix = MNT.rstrip("/") + share
        if path.startswith(prefix):
            rel = path[len(prefix):]
            candidates = _disks_with_dir(os.path.dirname(rel))
            if len(candidates) == 1 and os.path.exists(os.path.join(candidates[0], rel)):
                return os.path.basename(candidates[0]), os.path.join(candidates[0], rel)
            for root in candidates:
                p = os.path.join(root, rel)
                if os.path.exists(p):
                    return os.path.basename(root), p
            return "", path
    m = re.match(re.escape(MNT.rstrip("/")) + r"/([^/]+)/", path)
    if m and m.group(1) not in _SKIP:
        return m.group(1), path
    return "", path


def disks_state() -> dict[str, dict]:
    """Spin/temperature info per disk from emhttp's disks.ini (empty when unavailable)."""
    data = _ini(os.path.join(EMHTTP, "disks.ini"))
    out = {}
    for sec, vals in data.items():
        if not sec or vals.get("status", "DISK_OK") in ("DISK_NP", "DISK_NP_DSBL"):
            continue
        name = vals.get("name", sec)
        out[name] = {
            "spundown": vals.get("spundown", "0") == "1",
            "temp": vals.get("temp", ""),
            "type": vals.get("type", ""),
            "rotational": vals.get("rotational", "1") == "1",
        }
    return out


def spun_up_disks() -> set[str] | None:
    st = disks_state()
    if not st:
        return None
    return {n for n, v in st.items() if not v["spundown"] or not v["rotational"]}


def parity_running() -> bool:
    var = _ini(os.path.join(EMHTTP, "var.ini")).get("", {})
    if var:
        try:
            return int(var.get("mdResyncPos", "0") or 0) > 0
        except ValueError:
            pass
    try:
        with open("/proc/mdstat") as fh:
            for line in fh:
                if line.startswith("mdResyncPos="):
                    return int(line.split("=", 1)[1].strip() or 0) > 0
    except (OSError, ValueError):
        pass
    return False


def mover_running() -> bool:
    """Unraid's mover is moving files between cache and array (emhttp var.ini: shareMoverActive)."""
    var = _ini(os.path.join(EMHTTP, "var.ini")).get("", {})
    return str(var.get("shareMoverActive", "")).lower() in ("yes", "true", "1")


def is_fast_disk(name: str, state: dict | None = None) -> bool:
    """Cache/pool or SSD: scanning it never spins anything up or competes with playback."""
    if not name:
        return False
    st = (state if state is not None else disks_state()).get(name)
    if st is not None:
        return not st["rotational"] or (not re.match(r"disk\d+$", name) and st.get("type", "").lower() == "cache")
    return not re.match(r"(disk\d+|parity\d*)$", name)  # pools are named, array disks are diskN


@lru_cache(maxsize=1)
def is_unraid() -> bool:
    return os.path.isdir(os.path.join(MNT, "user")) or os.path.exists(os.path.join(EMHTTP, "var.ini"))
