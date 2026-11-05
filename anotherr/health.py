"""Crash diagnostics: memory readings, a heartbeat file, and a note at startup if the last run died.

An out-of-memory kill is a SIGKILL - the process can't log anything on the way out. So every minute the
worker writes a heartbeat with current memory use and the cgroup's oom_kill counter; a clean shutdown
marks it. At startup an unmarked heartbeat means the previous run ended abruptly, and a higher
oom_kill count than the heartbeat recorded means the kernel's OOM killer did it.
"""
from __future__ import annotations

import json
import os
import time

from . import db

HEARTBEAT = os.path.join(db.CONFIG_DIR, "heartbeat.json")
_CGROUP_EVENTS = ("/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory/memory.oom_control")


def _rss_kb(pid: int | str) -> int:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def children() -> list[dict]:
    """Our child processes (ffmpeg/ffprobe/tesseract) with their memory."""
    me = os.getpid()
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                parts = fh.read().rsplit(")", 1)[1].split()
            if int(parts[1]) != me:
                continue
            with open(f"/proc/{d}/comm") as fh:
                name = fh.read().strip()
        except (OSError, IndexError, ValueError):
            continue
        out.append({"pid": int(d), "name": name, "rss_mb": _rss_kb(d) // 1024})
    return out


def oom_kills() -> int | None:
    for p in _CGROUP_EVENTS:
        try:
            with open(p) as fh:
                for line in fh:
                    k, _, v = line.partition(" ")
                    if k == "oom_kill":
                        return int(v)
        except (OSError, ValueError):
            continue
    return None


def _read_kv(path: str) -> dict[str, int]:
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                k, _, v = line.partition(" ")
                if v.strip().isdigit():
                    out[k] = int(v)
    except OSError:
        pass
    return out


def container_memory() -> dict:
    """The container's memory as Docker sees it, split into what processes use and reclaimable file cache.

    Docker counts the page cache of files the container read (minus the 'inactive' part). Scanning reads a
    lot of video, so that number grows toward free RAM - the kernel gives it back on demand, it isn't a leak.
    """
    mb = 1024 * 1024
    st = _read_kv("/sys/fs/cgroup/memory.stat")  # cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.current") as fh:
            cur = int(fh.read().strip())
    except (OSError, ValueError):
        cur = None
    if st and cur is not None:
        docker = cur - st.get("inactive_file", 0)
        return {"container_mb": cur // mb, "docker_shows_mb": docker // mb, "processes_mb": st.get("anon", 0) // mb,
                "file_cache_mb": st.get("file", 0) // mb, "active_file_cache_mb": st.get("active_file", 0) // mb}
    st = _read_kv("/sys/fs/cgroup/memory/memory.stat")  # cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as fh:
            cur = int(fh.read().strip())
    except (OSError, ValueError):
        return {}
    docker = cur - st.get("total_inactive_file", 0)
    return {"container_mb": cur // mb, "docker_shows_mb": docker // mb, "processes_mb": st.get("total_rss", 0) // mb,
            "file_cache_mb": st.get("total_cache", 0) // mb, "active_file_cache_mb": st.get("total_active_file", 0) // mb}


def memory() -> dict:
    kids = children()
    return {"anotherr_mb": _rss_kb("self") // 1024, "children": len(kids),
            "children_mb": sum(k["rss_mb"] for k in kids),
            "ffmpeg": sum(1 for k in kids if k["name"].startswith(("ffmpeg", "ffprobe"))),
            "oom_kills": oom_kills(), **container_memory()}


def beat(extra: dict | None = None):
    rec = {"ts": time.time(), "clean": False, **memory(), **(extra or {})}
    tmp = HEARTBEAT + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def mark_clean_shutdown():
    try:
        with open(HEARTBEAT) as fh:
            rec = json.load(fh)
        rec["clean"] = True
        with open(HEARTBEAT, "w") as fh:
            json.dump(rec, fh)
    except (OSError, ValueError):
        pass


def check_previous_run() -> str | None:
    """Explain how the previous run ended, if it didn't shut down cleanly."""
    try:
        with open(HEARTBEAT) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    if rec.get("clean"):
        return None
    now_kills, then_kills = oom_kills(), rec.get("oom_kills")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("ts", 0)))
    mem = (f"last reading at {when}: Anotherr {rec.get('anotherr_mb')} MB, {rec.get('ffmpeg')} ffmpeg process(es) "
           f"using {rec.get('children_mb')} MB")
    if now_kills is not None and then_kills is not None and now_kills > then_kills:
        return f"Previous run was killed by the out-of-memory killer ({now_kills - then_kills} kill(s)); {mem}"
    return f"Previous run ended unexpectedly (crash, OOM kill or forced container stop); {mem}"
