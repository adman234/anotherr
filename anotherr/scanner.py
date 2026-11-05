"""The scan pipeline: probe -> integrity -> audio/language -> subtitles -> multi-episode -> reviews."""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
import traceback
from dataclasses import dataclass, field

import numpy as np

from . import analysis, db, frames, langs, media, ocr, runtime, settings, stt

log = logging.getLogger("anotherr.scan")

# Scans can run in parallel (one per slot); CURRENT maps slot name -> live status of its scan.
CURRENT: dict[str, dict] = {}
CANCEL = threading.Event()   # set to stop every running scan; the scheduler clears it once they've stopped
_active_files: set[int] = set()
_active_lock = threading.Lock()


def current_scans() -> list[dict]:
    return [dict(v, slot=k) for k, v in list(CURRENT.items())]

FAMILIES = [{"chi", "yue"}, {"nor", "dan", "swe"}, {"srp", "hrv", "bos"}, {"may", "ind"}]


class Cancelled(Exception):
    pass


def lang_match(a: str, b: str) -> bool:
    a, b = langs.norm(a), langs.norm(b)
    if not a or not b:
        return False
    return a == b or any(a in f and b in f for f in FAMILIES)


@dataclass
class Ctx:
    file: dict
    item: dict
    cfg: dict
    profile: str
    pcfg: dict
    seed: int
    path: str = ""
    info: dict = field(default_factory=dict)
    segments: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    result: dict = field(default_factory=dict)
    audio: dict = field(default_factory=dict)   # (a_index, seg_idx) -> np.ndarray
    vad: dict = field(default_factory=dict)     # (a_index, seg_idx) -> [(abs_start, abs_end)]
    tracks: dict = field(default_factory=dict)  # a_index -> analysis
    expected: str = ""
    orig_track: int | None = None
    live: dict = field(default_factory=dict)  # this scan's entry in CURRENT

    def issue(self, category: str, code: str, severity: str, message: str, **details):
        self.issues.append({"category": category, "code": code, "severity": severity, "message": message,
                            "details": details})

    def step(self, name: str):
        if CANCEL.is_set():
            raise Cancelled()
        now = time.time()
        prev, prev_at = self.live.get("step"), self.live.get("step_at")
        if prev and prev_at:
            group = prev.split(":")[0].split(" segment")[0].split(" track")[0].strip()
            t = self.result.setdefault("timings", {})
            t[group] = round(t.get(group, 0) + now - prev_at, 1)
        self.live["step"] = name
        self.live["step_at"] = now

    @property
    def episodes(self) -> list[dict]:
        return db.unj(self.file.get("episodes"), []) or []


# ====================================================================== entry

def resolve_path(f: dict) -> str:
    from .sync import map_path
    from .unraid import locate

    if f.get("local_path") and os.path.exists(f["local_path"]):
        return f["local_path"]
    mapped = map_path(f["arr_path"])
    disk, read_path = locate(mapped)
    if os.path.exists(read_path):
        db.x("UPDATE files SET local_path=?, disk=? WHERE id=?", (read_path, disk, f["id"]))
        return read_path
    return ""


def _release_cache(path: str):
    """Drop the scanned file from the page cache - unless Plex is playing it right now."""
    if not path:
        return
    try:
        from . import scheduler

        playing = {os.path.basename((x.get("file") or "").replace("\\", "/"))
                   for x in scheduler._streams_cache.get("sessions") or []}
        if os.path.basename(path) in playing:
            return
    except Exception:  # noqa: BLE001
        pass
    media.drop_cache(path)
    for sd in media.sidecar_subs(path):
        media.drop_cache(sd["path"])


_resync_at: dict[int, float] = {}


def handle_missing(f: dict, item: dict | None = None, wait: bool = False):
    """A file can't be read: resync its series/movie (it was probably replaced or renamed). If Sonarr/Radarr
    still point at the unreadable path, keep a file_not_found issue open and park the file's reviews."""
    item = item or db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    if not item or time.time() - _resync_at.get(item["id"], 0) < 600:
        return
    _resync_at[item["id"]] = time.time()
    t = threading.Thread(target=_resync_missing, args=(f["id"], item), daemon=True, name="anotherr-resync")
    t.start()
    if wait:
        t.join(60)


def _resync_missing(fid: int, item: dict):
    from . import sync

    try:
        sync.sync_one(item["source"], item["arr_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("resync after missing file failed: %s", e)
    f = db.q1("SELECT * FROM files WHERE id=?", (fid,))
    if not f or f["missing"] or resolve_path(f):
        if f and f["missing"]:
            db.log(f"{item['title']} {f.get('label', '')}: file was replaced or removed - old record retired")
        return
    now = time.time()
    if not db.q1("SELECT id FROM issues WHERE file_id=? AND code='file_not_found' AND status='open'", (fid,)):
        db.x("INSERT INTO issues(file_id, category, code, severity, message, details, status, created_at, updated_at) "
             "VALUES (?, 'config', 'file_not_found', 'error', ?, ?, 'open', ?, ?)",
             (fid, f"File can't be read at {f['arr_path']} and {item['source'].title()} still lists it there.",
              db.j({"arr_path": f["arr_path"]}), now, now))
    db.x("UPDATE reviews SET status='dismissed', notes='parked: file not readable', updated_at=? "
         "WHERE file_id=? AND status='pending'", (now, fid))
    sev = [r["severity"] for r in db.q("SELECT severity FROM issues WHERE file_id=? AND status='open'", (fid,))]
    db.x("UPDATE files SET status=?, open_issues=? WHERE id=?", (db.worst(sev) or "ok", len(sev), fid))


def scan_file(file_id: int, profile: str | None = None, source: str = "manual", seed: int | None = None,
              slot: str = "main", why: str = "") -> dict:
    with _active_lock:
        if file_id in _active_files:
            return {"status": "skipped", "error": "already being scanned"}
        _active_files.add(file_id)
    try:
        return _scan_file(file_id, profile, source, seed, slot, why)
    finally:
        with _active_lock:
            _active_files.discard(file_id)


def _scan_file(file_id: int, profile: str | None, source: str, seed: int | None, slot: str, why: str) -> dict:
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise ValueError(f"file {file_id} not found")
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    cfg = settings.get()
    profile = profile or cfg["scan"]["default_profile"]
    pcfg = cfg["scan"]["profiles"].get(profile) or cfg["scan"]["profiles"]["standard"]
    seed = seed if seed is not None else random.randrange(1 << 31)
    started = time.time()
    scan_id = db.x("INSERT INTO scans(file_id, profile, seed, source, started_at, status) VALUES (?,?,?,?,?,?)",
                   (file_id, profile, seed, source, started, "running"))
    live = {"file_id": file_id, "scan_id": scan_id, "title": item["title"], "label": f.get("label", ""),
            "disk": f.get("disk", ""), "why": why, "profile": profile, "started_at": started, "step": "starting"}
    CURRENT[slot] = live
    ctx = Ctx(file=f, item=item, cfg=cfg, profile=profile, pcfg=pcfg, seed=seed, live=live)
    status, error = "done", ""
    try:
        run_pipeline(ctx)
    except Cancelled:
        status = "cancelled"
    except Exception as e:  # noqa: BLE001
        status, error = "failed", f"{e}\n{traceback.format_exc()[-1500:]}"
        log.exception("scan failed for file %s", file_id)
    finally:
        CURRENT.pop(slot, None)
        _release_cache(ctx.path)
    return persist(ctx, scan_id, status, error, time.time() - started)


def run_pipeline(ctx: Ctx):
    rng = random.Random(ctx.seed)
    ctx.step("locating file")
    ctx.path = resolve_path(ctx.file)
    if not ctx.path:
        from .sync import map_path

        tried = map_path(ctx.file["arr_path"])
        ctx.issue("config", "file_not_found", "error",
                  f"File not found. {ctx.item['source'].title()} path: {ctx.file['arr_path']} -> looked for: {tried}. "
                  "Fix Settings -> Path mappings (use 'Check paths'), or resync if the file was replaced.",
                  arr_path=ctx.file["arr_path"], tried=tried)
        handle_missing(ctx.file, ctx.item)
        return
    ctx.result["path"] = ctx.path

    ctx.step("probing")
    try:
        probe = media.ffprobe(ctx.path)
    except Exception as e:  # noqa: BLE001
        ctx.issue("integrity", "unreadable", "critical", f"ffprobe could not read the file: {e}")
        return
    ctx.info = media.summarize(probe)
    ctx.info["subs"] += media.sidecar_subs(ctx.path)
    ctx.result["streams"] = {k: ctx.info[k] for k in ("duration", "format", "bit_rate", "video", "audio", "subs",
                                                      "chapters")}
    check_streams(ctx)
    if not ctx.info["duration"]:
        return

    n, secs = int(ctx.pcfg["segments"]), float(ctx.pcfg["seconds"])
    ctx.segments = media.pick_segments(ctx.info["duration"], n, secs, rng)
    ctx.result["segments"] = ctx.segments
    check_decode(ctx)
    if ctx.info["audio"]:
        check_audio(ctx)
        check_language(ctx)
    check_subtitles(ctx)
    check_multi_episode(ctx)
    queue_reviews(ctx, rng)
    consolidate(ctx)
    # render the frames a reviewer (you or Claude) will look at now, while the file is open and the
    # disk is spinning - viewing reviews later then never starts ffmpeg or wakes a disk
    want = [t for r in ctx.reviews if r.get("status", "pending") == "pending" for t in r["timestamps"]]
    want += [t for i in ctx.issues for t in (i["details"].get("evidence_ts") or [])]
    if want and ctx.info.get("video"):
        ctx.step("saving review frames")
        frames.prerender(ctx.file, ctx.path, list(dict.fromkeys(round(t, 1) for t in want))[:24])
    ctx.step("done")  # closes the timing of the last step


# ====================================================================== integrity

DURATION_CODES = {"duration_short", "duration_short_warn", "double_length", "duration_long"}


def check_streams(ctx: Ctx):
    info = ctx.info
    if not info["video"]:
        ctx.issue("integrity", "no_video", "critical", "No video stream found.")
    if not info["audio"]:
        ctx.issue("integrity", "no_audio", "critical", "No audio stream found.")
    dur = info["duration"]
    if not dur:
        ctx.issue("integrity", "unreadable", "critical", "Container reports no duration (file may be damaged).")
        return
    check_runtime(ctx, dur)
    v = info["video"][0] if info["video"] else None
    if v and v.get("duration") and abs(v["duration"] - dur) > max(30, 0.05 * dur):
        ctx.issue("integrity", "stream_duration_mismatch", "warning",
                  f"Video stream length ({v['duration'] / 60:.1f} min) differs from container ({dur / 60:.1f} min).")


# per-opinion thresholds: (error below, warning below, double-length band, "much longer" above)
_RUNTIME_LIMITS = {
    "files": (0.6, 0.75, (1.7, 2.4), 2.6),             # the season's/series' own files: reliable
    "episode metadata": (0.45, 0.62, (1.7, 2.4), 2.6),  # TVDB per-episode runtimes
    "series metadata": (0.3, 0.62, (1.7, 2.4), 2.6),    # one number for the whole show: weakest
    "movie": (0.6, 0.85, None, 1.6),
}
_RANK = {"ok": 0, "long": 1, "double": 2, "short_warn": 3, "short": 4}


def _runtime_verdict(ratio: float, limits: tuple, single: bool) -> str:
    err, warn, double, long_ = limits
    if ratio < err:
        return "short"
    if ratio < warn:
        return "short_warn"
    if double and single and double[0] <= ratio <= double[1]:
        return "double"
    if ratio > long_:
        return "long"
    return "ok"


def check_runtime(ctx: Ctx, dur: float):
    """Length vs. two independent opinions; a file is only flagged when both say it's off the same way."""
    is_movie = ctx.item["kind"] == "movie"
    mapped = 1 if is_movie else max(1, len(ctx.episodes))
    per_file = dur / 60.0 / mapped
    if is_movie:
        est = {"metadata_min": float(ctx.item.get("runtime") or 0) or None, "metadata_source": "movie"}
        opinions = [("movie", est["metadata_min"], "movie")] if est["metadata_min"] else []
    else:
        est = runtime.estimate(ctx.item, ctx.file, ctx.episodes)
        opinions = []
        if est.get("files_min"):
            opinions.append(("files", est["files_min"], est["files_source"]))
        if est.get("metadata_min"):
            opinions.append((est["metadata_source"], est["metadata_min"], est["metadata_source"]))
    ctx.result["runtime_estimate"] = est
    if not opinions:
        return
    verdicts = []
    for kind, minutes, label in opinions:
        r = per_file / minutes
        verdicts.append({"basis": label, "per_episode_min": round(minutes, 2), "ratio": round(r, 2),
                         "verdict": _runtime_verdict(r, _RUNTIME_LIMITS[kind], mapped == 1)})
    ctx.result["runtime_opinions"] = verdicts
    ctx.result["runtime_ratio"] = verdicts[0]["ratio"]
    ctx.result["expected_runtime_s"] = round(opinions[0][1] * mapped * 60, 1)
    kinds = {v["verdict"] for v in verdicts}
    shorts, longs = {"short", "short_warn"}, {"double", "long"}
    if "ok" in kinds or not (kinds <= shorts or kinds <= longs):
        if kinds - {"ok"}:
            # one opinion objects, the other doesn't: no issue, but let the title cards settle it
            ctx.result["runtime_disputed"] = True
        return
    final = max((v["verdict"] for v in verdicts), key=lambda k: _RANK[k])  # both agree: take the stronger
    why = "; ".join(f"{v['basis']} ~{v['per_episode_min']:.1f} min/episode ({v['ratio']:.0%})" for v in verdicts)
    d = {"ratio": verdicts[0]["ratio"], "mapped_episodes": mapped, "file_min": round(dur / 60, 1), "opinions": verdicts}
    if final == "short":
        ctx.issue("integrity", "duration_short", "error",
                  f"File is {dur / 60:.1f} min for {mapped} episode(s) - too short against {why} "
                  "(wrong content, sample, or truncated).", **d)
    elif final == "short_warn":
        ctx.issue("integrity", "duration_short_warn", "warning",
                  f"File is shorter than expected ({dur / 60:.1f} min; {why}).", **d)
    elif final == "double":
        finale = _is_season_finale(ctx)
        ctx.issue("multi_episode", "double_length", "info" if finale else "warning",
                  f"Double-length file ({dur / 60:.1f} min; {why})"
                  + (": it's the season finale, which often runs long." if finale else
                     ": a special or double episode, or a different/second episode mapped as this one. "
                     "Title cards queued for review."), season_finale=finale, **d)
    elif final == "long":
        ctx.issue("integrity", "duration_long", "info", f"File is much longer than expected ({dur / 60:.0f} min; {why}).", **d)


def _is_season_finale(ctx: Ctx) -> bool:
    eps = ctx.episodes
    if len(eps) != 1 or eps[0].get("season") in (None, 0):
        return False
    try:
        season = [e for e in ocr.series_episodes(ctx.item) if e.get("season") == eps[0]["season"]]
    except Exception:  # noqa: BLE001
        return False
    return bool(season) and eps[0].get("episode") == max(e.get("episode") or 0 for e in season)


def check_decode(ctx: Ctx):
    threads = int(ctx.cfg["scan"].get("ffmpeg_threads", 0))
    hw = ctx.cfg["scan"].get("hw_decode", "auto")
    has_video = bool(ctx.info["video"])
    results = []
    for i, (s, d) in enumerate(ctx.segments):
        ctx.step(f"decoding segment {i + 1}/{len(ctx.segments)}")
        results.append(media.decode_segment(ctx.path, s, d, threads, has_video=has_video, hw=hw))
    ctx.result["decode"] = results
    ctx.result["gpu_decode"] = any(r.get("gpu") for r in results)
    if has_video:
        mid_idx = list(range(1, len(results) - 1)) if len(results) > 2 else []
        for i in mid_idx:
            ctx.step("video checks")
            vc = media.video_checks(ctx.path, results[i]["start"], results[i]["dur"])
            results[i]["black"], results[i]["freeze_candidates"] = vc["black"], vc["freeze_candidates"]
        check_freeze(ctx, results, mid_idx)
        ctx.step("interlace check")
        probe_at = results[mid_idx[0]]["start"] if mid_idx else results[0]["start"]
        results[0]["idet"] = media.idet_sample(ctx.path, probe_at, 10)
    # A segment that stopped early on the GPU is retried in software before it counts against the file
    # (NVDEC seeks near the end of some files return nothing even though the file plays fine).
    for i, r in enumerate(results):
        if r.get("gpu") and not r.get("timeout") and r["decoded"] < r["dur"] * 0.9 - 2:
            ctx.step("re-checking a segment in software")
            again = media.decode_segment(ctx.path, r["start"], r["dur"], threads, has_video=has_video, hw="off")
            again["retried_from_gpu"] = {"decoded": r["decoded"], "errors": r["errors"][:5]}
            again["black"], again["freeze_candidates"] = r.get("black", []), r.get("freeze_candidates", [])
            results[i] = again
    ctx.result["decode"] = results
    total_err = sum(r.get("error_count", 0) for r in results)
    bad_segs = sum(1 for r in results if r.get("error_count", 0) > 5)
    last = len(results) - 1
    for i, r in enumerate(results):
        short = r["decoded"] < r["dur"] * 0.9 - 2
        if short and i == last:
            confirm = confirm_truncation(ctx, threads, has_video)
            ctx.result["truncation_check"] = confirm
            if confirm["truncated"]:
                ctx.issue("integrity", "truncated", "critical",
                          f"Decoding stopped {r['decoded']:.0f}s into the final {r['dur']:.0f}s segment, and a "
                          f"software re-check of the last 30 s confirmed it - file looks truncated.",
                          segment_start=r["start"], confirm=confirm,
                          evidence_ts=[round(max(0.0, r["start"] + r["decoded"] - 5), 1), round(r["start"] + r["decoded"] + 5, 1)])
        elif short and not r.get("timeout"):
            bad_segs += 1
    stopped = [r["start"] for i, r in enumerate(results)
               if i != last and not r.get("timeout") and r["decoded"] < r["dur"] * 0.9 - 2]
    if total_err == 0 and len(stopped) >= 2:
        ctx.issue("integrity", "decode_stopped_early", "error",
                  f"Decoding stopped early in {len(stopped)} of {len(results)} sampled segments (no decoder "
                  "errors were printed) - the file is likely damaged or cut short.", segments=stopped)
    elif total_err > 20 or bad_segs >= 2:
        ctx.issue("integrity", "decode_errors_severe", "error",
                  f"{total_err} decode errors across {len(results)} sampled segments"
                  + (f"; decoding stopped early in {len(stopped)}" if stopped else "") + ".",
                  sample=[e for r in results for e in r["errors"]][:10])
    elif total_err:
        ctx.issue("integrity", "decode_errors", "warning", f"{total_err} decode error(s) in sampled segments.",
                  sample=[e for r in results for e in r["errors"]][:10])
    mids = results[1:-1] if len(results) > 2 else []
    for r in mids:
        black = sum(b[2] for b in r.get("black", []))
        if black > 0.5 * r["dur"]:
            ctx.issue("video", "black_video", "info",
                      f"{black:.0f}s of black video in the segment at {r['start'] / 60:.1f} min.", start=r["start"])
            break
    tff = sum((r["idet"] or {}).get("tff", 0) for r in results)
    bff = sum((r["idet"] or {}).get("bff", 0) for r in results)
    prog = sum((r["idet"] or {}).get("progressive", 0) for r in results)
    if tff + bff > 50 and (tff + bff) > 0.3 * (tff + bff + prog):
        ctx.issue("video", "interlaced", "info", "Video looks interlaced (not deinterlaced at encode).",
                  tff=tff, bff=bff, progressive=prog)
    if ctx.pcfg.get("full_decode"):
        ctx.step("full decode")
        fd = media.full_decode(ctx.path, threads, hw=hw)
        ctx.result["full_decode"] = {"error_count": fd["error_count"], "errors": fd["errors"][:20]}
        if fd["error_count"] > 20:
            ctx.issue("integrity", "full_decode_errors", "error", f"Full decode found {fd['error_count']} errors.",
                      sample=fd["errors"][:10])
        elif fd["error_count"]:
            ctx.issue("integrity", "full_decode_minor", "warning", f"Full decode found {fd['error_count']} error(s).",
                      sample=fd["errors"][:10])


def confirm_truncation(ctx: Ctx, threads: int, has_video: bool) -> dict:
    """Software decode (accurate seek) of the last 30 s before the video stream's own end."""
    dur = ctx.info["duration"]
    v = (ctx.info.get("video") or [{}])[0]
    end = min(dur, v.get("duration") or dur)
    start = max(0.0, end - 35)
    r = media.decode_segment(ctx.path, start, 30, threads, has_video=has_video, hw="off")
    ok = r["decoded"] >= 20
    return {"truncated": not ok, "start": round(start, 1), "decoded": round(r["decoded"], 1),
            "container_s": round(dur, 1), "video_stream_s": round(v.get("duration") or 0, 1),
            "errors": r["errors"][:5]}


EDGE_S = 120  # ignore the first/last 2 minutes for picture checks (logos, credits, fades)


def check_freeze(ctx: Ctx, results: list[dict], mid_idx: list[int]):
    """Screen candidates on keyframes, confirm each with a continuous decode, report with evidence."""
    dur = ctx.info["duration"]
    evidence, confirmed_segs = [], set()
    budget = 3  # continuous decodes per file
    for i in mid_idx:
        for a, b in results[i].get("freeze_candidates", []):
            if budget <= 0 or a < EDGE_S or b > dur - EDGE_S:
                continue
            budget -= 1
            ctx.step("confirming possible freeze")
            c = media.confirm_freeze(ctx.path, a, min(b - a + 2, 60))
            evidence.append({"segment": i, "candidate": (a, b), **c})
            if c.get("confirmed"):
                confirmed_segs.add(i)
    ctx.result["freeze_check"] = evidence
    hits = [e for e in evidence if e.get("confirmed")]
    if not hits:
        return
    f0 = hits[0]["freeze"]
    ts = [round(f0[0] + (f0[1] - f0[0]) * k / 3, 1) for k in range(4)]
    where = ", ".join(f"{e['freeze'][0] / 60:.1f} min ({e['longest_s']}s)" for e in hits)
    two = len(confirmed_segs) >= 2
    ctx.issue("video", "frozen_video", "warning" if two else "info",
              f"Picture frozen at {where}" + ("" if two else " - seen in one place only, so treat as unconfirmed")
              + ". Confirmed with a continuous decode at 1 frame/s.",
              freezes=[e["freeze"] for e in hits], windows=[e["window"] for e in hits],
              unchanged_fraction=[e["unchanged_fraction"] for e in hits], evidence_ts=ts)


# ====================================================================== audio & language

def _audio_segments(ctx: Ctx) -> list[int]:
    """Windows used for audio analysis: the middle segments (no intros/credits) when there are at
    least 3 of them, otherwise every segment - language ID wants >= 3 windows from across the file."""
    idx = list(range(len(ctx.segments)))
    mids = idx[1:-1] if len(idx) > 2 else []
    return mids if len(mids) >= 3 else idx


def _expected(ctx: Ctx) -> str:
    return langs.norm(ctx.item.get("language_override")) or langs.norm(ctx.item.get("original_language"))


def check_audio(ctx: Ctx):
    ctx.expected = _expected(ctx)
    dub = langs.norm(ctx.cfg["anime"]["dub_language"])
    tracks = [a for a in ctx.info["audio"] if not a.get("commentary")]

    def prio(a):
        return (0 if lang_match(a["lang"], ctx.expected) else 1, 0 if a["default"] else 1,
                0 if lang_match(a["lang"], dub) else 1, a["a_index"])

    tracks = sorted(tracks, key=prio)[: int(ctx.cfg["scan"].get("max_audio_tracks", 4))]
    segs = _audio_segments(ctx)
    for si in segs:
        ctx.step(f"reading audio segment {si + 1}")
        s, d = ctx.segments[si]
        for ai, audio in media.extract_audio_multi(ctx.path, [a["a_index"] for a in tracks], s, d).items():
            ctx.audio[(ai, si)] = audio
    for a in tracks:
        ai = a["a_index"]
        speech_all, levels = [], []
        for si in segs:
            ctx.step(f"speech detection track {ai + 1}")
            s, d = ctx.segments[si]
            audio = ctx.audio.get((ai, si), np.zeros(0, dtype=np.float32))
            levels.append(analysis.rms_db(audio))
            iv = stt.speech_intervals(audio)
            ctx.vad[(ai, si)] = [(s + a0, s + b0) for a0, b0 in iv]
            speech_all.append((audio, iv))
        speech_s = sum(e - s for _, iv in speech_all for s, e in iv)
        t = {"a_index": ai, "tag": a["lang"], "title": a["title"], "default": a["default"],
             "channels": a.get("channels"), "codec": a["codec"], "speech_s": round(speech_s, 1),
             "window_s": round(sum(ctx.segments[si][1] for si in segs), 1),
             "level_db": round(max(levels) if levels else -120, 1), "detected": "", "prob": 0.0, "windows": []}
        if levels and max(levels) < -70:
            ctx.issue("audio", "silent_audio", "error", f"Audio track #{ai + 1} ({langs.name(a['lang'])}) is silent.",
                      a_index=ai)
        else:
            # language ID per window (each from a different part of the file), not one pooled clip
            ctx.step(f"language detection track {ai + 1}")
            for si, (au, iv) in zip(segs, speech_all):
                if sum(e - s for s, e in iv) < 4 or len(t["windows"]) >= 5:
                    continue
                try:
                    lang, prob, _ = stt.WHISPER.detect_language(stt.speech_audio(au, iv, 30))
                except Exception as e:  # noqa: BLE001
                    t["error"] = str(e)
                    ctx.result.setdefault("warnings", []).append(f"language detection failed: {e}")
                    break
                t["windows"].append({"at": round(ctx.segments[si][0], 1), "lang": langs.norm(lang) or lang,
                                     "prob": round(prob, 3)})
            agg = analysis.aggregate_language([(w["lang"], w["prob"]) for w in t["windows"]])
            t.update(detected=agg["detected"], prob=agg["prob"], agree=agg["agree"], all_agree=agg["all_agree"])
        ctx.tracks[ai] = t
    ctx.result["audio_tracks"] = list(ctx.tracks.values())


def _extra_language_windows(ctx: Ctx, t: dict, positions=(0.15, 0.35, 0.55, 0.75), secs: float = 30.0):
    """More language samples from across the file for a track that has too few."""
    dur = ctx.info.get("duration") or 0
    for p in positions:
        if len(t["windows"]) >= 5 or dur < 120:
            break
        start = dur * p
        if any(abs(start - w["at"]) < secs for w in t["windows"]):
            continue
        audio = media.extract_audio_multi(ctx.path, [t["a_index"]], start, secs).get(t["a_index"])
        if audio is None or audio.size == 0:
            continue
        iv = stt.speech_intervals(audio)
        if sum(e - s for s, e in iv) < 4:
            continue
        try:
            lang, prob, _ = stt.WHISPER.detect_language(stt.speech_audio(audio, iv, 30))
        except Exception:  # noqa: BLE001
            break
        t["windows"].append({"at": round(start, 1), "lang": langs.norm(lang) or lang, "prob": round(prob, 3),
                             "extra": True})
    agg = analysis.aggregate_language([(w["lang"], w["prob"]) for w in t["windows"]])
    t.update(detected=agg["detected"], prob=agg["prob"], agree=agg["agree"], all_agree=agg["all_agree"])


def _sibling_mislabel(ctx: Ctx, exp: str) -> dict | None:
    """Another file of this series whose track, tagged like ours, was confirmed to be the expected language."""
    if not ctx.item.get("id"):
        return None
    for r in db.q("SELECT x.details, x.message, f.label FROM issues x JOIN files f ON f.id=x.file_id WHERE "
                  "f.item_id=? AND f.id!=? AND f.missing=0 AND x.code='audio_mislabeled' AND x.status IN ('open','ignored')",
                  (ctx.item["id"], ctx.file["id"])):
        d = db.unj(r["details"], {}) or {}
        det = d.get("detected")
        if not det:
            m = re.search(r"sounds like (\w+)", r["message"] or "")
            det = langs.norm(m.group(1)) if m else ""
        if lang_match(det, exp):
            return {"label": r["label"], "tag": d.get("tag", ""), "detected": det}
    return None


def check_language(ctx: Ctx):
    exp = ctx.expected
    dub = langs.norm(ctx.cfg["anime"]["dub_language"])
    ctx.result["expected_language"] = exp
    tracks = list(ctx.tracks.values())
    if not tracks:
        return
    # Before concluding the expected language is missing, give thinly-sampled tracks more chances.
    if exp and not any(lang_match(t["tag"], exp) or lang_match(t.get("detected"), exp) for t in tracks):
        for t in tracks:
            if len(t.get("windows", [])) < 3 and t.get("level_db", -120) > -70:
                ctx.step(f"extra language samples track {t['a_index'] + 1}")
                _extra_language_windows(ctx, t)
    for t in tracks:
        n, agree = len(t.get("windows", [])), t.get("agree", 0)
        det, tag = t["detected"], t["tag"]
        t["overridden"] = False
        if tag and det and not lang_match(tag, det):
            # overriding an explicit tag needs >= 3 windows that all agree, averaging >= 90%
            if n >= 3 and t.get("all_agree") and t["prob"] >= 0.9:
                t.update(effective=det, confident=True, overridden=True)
                ctx.issue("language", "audio_mislabeled", "warning",
                          f"Audio track #{t['a_index'] + 1} is tagged {langs.name(tag)} but sounds like "
                          f"{langs.name(det)} in all {n} sampled windows ({t['prob']:.0%} avg).",
                          a_index=t["a_index"], windows=t["windows"], tag=tag, detected=det)
            else:
                t.update(effective=tag, confident=False, disputed=True)  # keep the tag; evidence stays in the result
        elif tag:
            t.update(effective=tag, confident=bool(det))
        elif det and ((agree >= 2 and t["prob"] >= 0.6) or (n == 1 and t["prob"] >= 0.8)):
            t.update(effective=det, confident=True)
            ctx.issue("language", "audio_untagged", "info",
                      f"Audio track #{t['a_index'] + 1} has no language tag; detected {langs.name(det)} "
                      f"({agree}/{n} windows).", a_index=t["a_index"], windows=t["windows"])
        else:
            t.update(effective="", confident=False)
    if not exp:
        ctx.issue("language", "original_language_unknown", "info",
                  "Original language unknown in Sonarr/Radarr - set a language override to check it.")
        return
    orig = [t for t in tracks if lang_match(t["effective"], exp)]
    orig.sort(key=lambda t: (not t["confident"], not t["default"]))
    overridden_orig = [t for t in tracks if t.get("overridden") and lang_match(t["tag"], exp)]
    if orig:
        ctx.orig_track = orig[0]["a_index"]
    elif overridden_orig:
        # a track *tagged* with the expected language was re-identified by detection; the mislabel warning
        # covers it - don't escalate to original_audio_missing until a person or a second pass confirms
        ctx.orig_track = overridden_orig[0]["a_index"]
        ctx.result["original_audio_note"] = "tagged track re-identified by detection; not raised as missing"
    else:
        unsure = [t for t in tracks if not t["effective"]]
        found = sorted({langs.name(t["effective"]) for t in tracks if t["effective"]})
        what = "preferred" if ctx.item.get("language_override") else "original"
        sib = _sibling_mislabel(ctx, exp)
        tag_only = not any(t.get("confident") and t.get("detected") for t in tracks)
        if unsure and len(unsure) == len(tracks):
            ctx.issue("language", "language_unconfirmed", "warning",
                      f"Could not confirm {what} language ({langs.name(exp)}) audio - not enough clear speech.")
        elif sib and tag_only:
            ctx.issue("language", "language_unconfirmed", "warning",
                      f"Tracks are tagged {', '.join(found) or 'unknown'}, but {sib['label']} from this series has the "
                      f"same kind of tag and is confirmed {langs.name(exp)} - probably mislabeled too. Not raised as "
                      "missing until speech confirms it.", sibling=sib)
        else:
            ctx.issue("language", "original_audio_missing", "error",
                      f"No {langs.name(exp)} audio ({what} language). Found: {', '.join(found) or 'unknown'}.",
                      expected=exp, found=found)
        ctx.orig_track = next((t["a_index"] for t in tracks if t["default"]), tracks[0]["a_index"])
        ctx.result["orig_track_confirmed"] = False  # a stand-in, not a confirmed original-language track
    # players fall back to the first audio track when nothing is flagged default
    any_flag = any(a["default"] for a in ctx.info["audio"])
    default = next((t for t in tracks if t["default"]), None) if any_flag else ctx.tracks.get(0)
    if orig and default and not lang_match(default["effective"], exp):
        from . import defaults

        view = _track_view(ctx)
        tgt = defaults.pick_audio(view, exp)
        ctx.issue("language", "default_audio_not_preferred", "warning",
                  defaults.fix_text_audio(view, exp) or
                  f"Default audio track is {langs.name(default['effective'])}, not {langs.name(exp)}.",
                  fix_available=bool(tgt), target_track=tgt["a_index"] + 1 if tgt else None)
    if ctx.item.get("is_anime") and ctx.cfg["anime"]["prefer_dual_audio"] and not lang_match(exp, dub):
        has_dub = any(lang_match(t["effective"], dub) for t in tracks)
        ctx.result["dual_audio"] = bool(orig) and has_dub
        if not has_dub:
            ctx.issue("language", "no_dub", "info",
                      f"Dual audio preferred: no {langs.name(dub)} dub track.")


# ====================================================================== subtitles

DUB_TITLE = re.compile(r"\bdub|dubtitle|\bcc\b|closed.?caption", re.I)


def _hardsubs(ctx: Ctx, sub_lang: str) -> dict | None:
    """Burned-in subtitles? OCR the bottom third of frames taken mid-sentence. Only English OCR is bundled."""
    if "hardsubs" in ctx.result:
        return ctx.result["hardsubs"] or None
    ctx.result["hardsubs"] = {}
    if not (ocr.available() and ctx.info.get("video") and lang_match(sub_lang, "eng") and ctx.path):
        return None
    at = ctx.orig_track if ctx.orig_track is not None else next(iter(ctx.tracks), None)
    spans = sorted((e - s, (s + e) / 2) for (ai, _), ivs in ctx.vad.items() if ai == at for s, e in ivs)
    ts = [m for length, m in reversed(spans) if length >= 1.5][:5]
    if not ts:  # no speech timing available: spread over the sampled segments
        mids = ctx.segments[1:-1] if len(ctx.segments) >= 5 else ctx.segments
        ts = [s + d * f for s, d in mids for f in (0.3, 0.7)][:5]
    ctx.step("checking for burned-in subtitles")
    hits, sample, total = 0, "", 0
    for t in sorted(ts):
        try:
            words = ocr.confident_words(media.extract_ocr_frame(ctx.path, t, bottom_third=True))
        except Exception:  # noqa: BLE001
            continue
        total += len(words)
        if ocr.looks_like_subtitle(words):
            hits += 1
            sample = sample or " ".join(words)[:120]
    # most dialogue frames carry readable text, and enough of it that a sign or credit can't do it alone
    found = len(ts) >= 2 and hits >= max(2, (len(ts) + 1) // 2) and total >= 5
    ctx.result["hardsubs"] = {"checked": len(ts), "with_text": hits, "found": found, "sample": sample,
                              "timestamps": [round(t, 1) for t in ts]}
    return ctx.result["hardsubs"] if found else None


def _no_dialogue(ctx: Ctx) -> bool:
    """Near-zero speech in every analysed track (e.g. Baraka) - subtitle requirements don't apply."""
    tr = [t for t in ctx.tracks.values() if t.get("window_s")]
    return bool(tr) and all(t["speech_s"] < 10 and t["speech_s"] / t["window_s"] < 0.03 for t in tr)


def check_subtitles(ctx: Ctx):
    lcfg = ctx.cfg["languages"]
    sub_lang = langs.norm(lcfg["subtitle_language"]) or "eng"
    exp = ctx.expected or _expected(ctx)
    override = langs.norm(ctx.item.get("language_override"))
    if override and lang_match(override, sub_lang):
        needs = bool(lcfg["require_subs_for_native_audio"])
    else:
        needs = bool(ctx.item.get("is_anime")) or (bool(exp) and not lang_match(exp, sub_lang)) \
            or bool(lcfg["require_subs_for_native_audio"])
    if needs and _no_dialogue(ctx):
        ctx.issue("subtitles", "no_dialogue", "info",
                  "Almost no speech detected, so subtitle requirements were skipped.")
        needs = False
    subs = ctx.info["subs"]
    mine = [s for s in subs if lang_match(s["lang"], sub_lang)]
    if needs and not mine:
        hs = _hardsubs(ctx, sub_lang)
        if hs:
            ctx.issue("subtitles", "hardsubs", "info",
                      f"No {langs.name(sub_lang)} subtitle track, but subtitles are burned into the picture "
                      f"(text found in {hs['with_text']} of {hs['checked']} frames during dialogue).",
                      evidence_ts=hs["timestamps"][:4], sample=hs["sample"])
            needs = False
    ctx.result["subs_needed"] = needs
    if not ctx.pcfg.get("subtitles"):
        if needs and not mine:
            ctx.issue("subtitles", "subs_missing", "error",
                      f"No {langs.name(sub_lang)} subtitles (embedded or external) for {langs.name(exp)} audio.")
        elif needs and all(s["forced"] or "sign" in s["title"].lower() for s in mine):
            ctx.issue("subtitles", "subs_maybe_signs_only", "warning",
                      f"{langs.name(sub_lang)} subtitles look like signs/songs only (by track flags).")
        return

    dur = ctx.info["duration"]
    min_rate = float(lcfg["min_full_sub_cues_per_min"])
    # Embedded subtitles are read only inside the sampled windows (no full-file read).
    windows = [ctx.segments[i] for i in _audio_segments(ctx)]
    win_s = sum(d for _, d in windows) or dur
    packets = {}
    if any(not s["external"] for s in mine):
        ctx.step("reading subtitles")
        packets = media.sub_packets(ctx.path, windows)
    analysed = []
    for s in mine[:6]:
        cues, span = [], dur
        if s["external"]:
            if s["text"]:
                raw = _read(s["path"])
                if raw is None:
                    continue
                parsed, _ = analysis.dialogue_cues(raw)
                cues = [(c.start, c.end, c.text) for c in parsed]
            else:
                if any(a.get("kind") == "full" for a in analysed):
                    continue
                ctx.step("reading image subtitle timing")
                cues = [(a, b, "") for a, b in media.image_sub_times(s["path"])]
        else:
            span = win_s
            pk = packets.get(s["index"], [])
            if s["text"]:
                for p in pk:
                    style, text = media.text_from_packet(s["codec"], p["data"])
                    c = analysis.clean_cue(p["start"], p["start"] + (p["dur"] or 2.0), style, text)
                    if c:
                        cues.append((c.start, c.end, c.text))
            else:
                for i, p in enumerate(pk):
                    if p["size"] < 200:  # PGS "clear" display sets are tiny
                        continue
                    end = p["start"] + p["dur"] if p["dur"] > 0 else (pk[i + 1]["start"] if i + 1 < len(pk) else p["start"] + 3)
                    cues.append((p["start"], min(end, p["start"] + 10), ""))
        # density only counts cues inside the sampled windows (packets are read with padding)
        counted = len(cues) if s["external"] else sum(
            1 for c in cues if any(c[1] > ws and c[0] < ws + wd for ws, wd in windows))
        kind = analysis.classify_track(counted, span, s["title"], s["forced"], min_rate)
        analysed.append({"track": s, "cues": cues, "kind": kind, "per_min": round(counted / max(span / 60, 1), 1),
                         "name": s["title"] or s["codec"]})

    full = [a for a in analysed if a["kind"] == "full"]
    ctx.result["subtitle_tracks"] = [
        {"title": a["track"]["title"], "lang": a["track"]["lang"], "codec": a["track"]["codec"],
         "external": a["track"]["external"], "default": a["track"]["default"], "forced": a["track"]["forced"],
         "s_index": a["track"].get("s_index"), "hearing_impaired": bool(a["track"].get("hearing_impaired")),
         "text": bool(a["track"].get("text")), "path": a["track"].get("path") if a["track"]["external"] else None,
         "kind": a["kind"], "cues": len(a["cues"]), "per_min": a["per_min"]}
        for a in analysed]
    if needs and not mine:
        ctx.issue("subtitles", "subs_missing", "error",
                  f"No {langs.name(sub_lang)} subtitles (embedded or external) for {langs.name(exp)} audio.")
    elif needs and not full:
        ctx.issue("subtitles", "subs_signs_only", "error",
                  f"{langs.name(sub_lang)} subtitles are signs/songs only - no full dialogue track.")
    if needs and full and all(not a["track"]["text"] for a in full):
        ctx.issue("subtitles", "subs_image_only", "info",
                  "Only image-based (PGS/VobSub) full subtitles - many Plex clients will need to burn them in.")
    emb_mine = [a for a in analysed if not a["track"]["external"]]
    dflt = next((a for a in emb_mine if a["track"]["default"]), None)
    # A default forced track in a film whose audio is already in your language (translating the odd
    # foreign-language scene) is correct - only flag it when the main audio is foreign.
    if needs and dflt and dflt["kind"] == "signs" and full and exp and not lang_match(exp, sub_lang):
        emb_full = sorted([a for a in full if not a["track"]["external"]],
                          key=lambda a: (not a["track"]["text"], bool(a["track"].get("hearing_impaired")), a["track"]["s_index"]))
        fix = (f" Fix available: make subtitle track s{emb_full[0]['track']['s_index'] + 1} ('{emb_full[0]['name']}') "
               "the default." if emb_full else " The full track is an external file, so its flag can't be changed.")
        ctx.issue("subtitles", "default_subs_signs", "info",
                  f"The default-flagged subtitle track s{dflt['track']['s_index'] + 1} is signs/songs only; the full "
                  f"track is not default.{fix}", fix_available=bool(emb_full))

    # ---- timing against the ORIGINAL-language speech
    at = ctx.orig_track
    skip = None
    if at is None or at not in ctx.tracks:
        skip = "no analysed audio track"
    elif ctx.result.get("orig_track_confirmed") is False:
        eff = ctx.tracks[at].get("effective")
        if eff and exp and not lang_match(eff, exp):
            skip = (f"no {langs.name(exp)} track identified; not comparing subtitles against the "
                    f"{langs.name(eff)} track")
    per_track = []
    if full and not skip:
        ctx.result["sync_audio_track"] = at
        for a in full[:3]:
            ctx.step(f"subtitle sync: {a['name']}")
            per = []
            for si in _audio_segments(ctx):
                if (at, si) not in ctx.vad:
                    continue
                s, d = ctx.segments[si]
                per.append(analysis.sync_offset(ctx.vad[(at, si)], [(c[0], c[1]) for c in a["cues"]], s, d))
            a["sync"] = analysis.aggregate_sync(per)
            per_track.append(a)
    elif full:
        ctx.result["sync_skipped"] = skip

    # ---- text: transcribe a couple of windows of the audio that is in the subtitle language
    text_full = [a for a in full if a["track"]["text"]]
    native = next((t for t in ctx.tracks.values() if lang_match(t.get("effective", ""), sub_lang)), None)
    is_dub = bool(exp) and not lang_match(exp, sub_lang)
    transcript = []
    if text_full and native and (not is_dub or (ctx.item.get("is_anime") and ctx.cfg["anime"]["detect_dubtitles"])):
        ai = native["a_index"]
        segs = sorted([si for si in _audio_segments(ctx) if (ai, si) in ctx.audio],
                      key=lambda si: -sum(e - s for s, e in ctx.vad.get((ai, si), [])))[:2]
        if segs:
            ctx.step("transcribing for subtitle content check")
            for si in segs:
                try:
                    transcript.append((si, stt.WHISPER.transcribe(ctx.audio[(ai, si)], langs.iso1(sub_lang))))
                except Exception as e:  # noqa: BLE001
                    ctx.result.setdefault("warnings", []).append(f"transcription failed: {e}")
                    transcript = []
                    break
    if transcript:
        ctx.result["transcript_sample"] = " ".join(x[2] for _, t in transcript for x in t)[:1500]

    def overlap(a: dict, shift: float) -> dict:
        t_all, s_all = [], []
        for si, segs_t in transcript:
            s, d = ctx.segments[si]
            t_all += [x[2] for x in segs_t]
            s_all += [c[2] for c in a["cues"] if c[1] - shift > s and c[0] - shift < s + d]
        return analysis.text_overlap(" ".join(t_all), " ".join(s_all))

    checks = []
    if transcript and is_dub:  # anime: are these subs just the dub's script?
        dub_flags = []
        for a in text_full[:2]:
            ov = overlap(a, 0.0)
            ov["track"] = a["name"]
            checks.append(ov)
            if ov["recall"] is not None and ov["recall"] >= 0.55 and ov["precision"] >= 0.55:
                dub_flags.append((a, ov))
        for a, ov in dub_flags:
            others = [b for b in full if b is not a and b not in [x for x, _ in dub_flags]
                      and not DUB_TITLE.search(b["track"]["title"] or "")]
            labeled = bool(DUB_TITLE.search(a["track"]["title"] or ""))
            if labeled and others:
                ctx.issue("subtitles", "dubtitles_labeled", "info",
                          f"Track '{a['name']}' is labeled as dub captions and a separate translation track exists.", **ov)
            else:
                ctx.issue("subtitles", "dubtitles", "warning",
                          f"Subtitle track '{a['name']}' matches the {langs.name(sub_lang)} dub almost word for word - "
                          "these are dub captions ('dubtitles'), not a real translation.", **ov)

    # ---- one verdict per track: timing, confirmed or overruled by text aligned at the detected offset
    for a in per_track:
        sync = a["sync"]
        if transcript and not is_dub and a["track"]["text"]:
            shift = sync["offset"] if sync["verdict"] == "offset" else 0.0
            ov = overlap(a, shift)
            ov["track"], ov["shift"] = a["name"], shift
            checks.append(ov)
            a["verdict"] = analysis.track_verdict(sync["verdict"], ov["recall"])
            a["text"] = ov
        else:
            a["verdict"] = sync["verdict"]
    if checks:
        ctx.result["subtitle_content"] = checks
    ctx.result["subtitle_sync"] = [dict(a["sync"], track=a["name"], external=a["track"]["external"],
                                        verdict_final=a["verdict"]) for a in per_track]
    known = [a for a in per_track if a["verdict"] != "unknown"]
    if not known:
        return
    bad = [a for a in known if a["verdict"] == "mismatch"]
    off = [a for a in known if a["verdict"] == "offset"]

    def why(a):
        if a.get("text") and a["text"].get("recall") is not None and a["text"]["recall"] < 0.2:
            return "text doesn't match the dialogue" + (f" even after shifting {a['text']['shift']:+.1f}s"
                                                      if a["text"]["shift"] else "")
        return "timing doesn't match the dialogue"

    evidence = {"windows": [list(w) for w in windows], "sync_audio_track": at}
    if bad and len(bad) == len(known):
        ctx.issue("subtitles", "subs_mismatch", "error" if needs else "warning",
                  "Subtitles don't match this audio (" + "; ".join(f"{a['name']}: {why(a)}" for a in bad)
                  + ") - subs for a different episode/cut, or the wrong video.",
                  tracks=[a["name"] for a in bad], all_tracks_failed=True, track_count=len(known), **evidence)
    elif bad:
        ctx.issue("subtitles", "subs_mismatch_partial", "warning",
                  f"{len(bad)} of {len(known)} subtitle tracks don't match the dialogue: "
                  + "; ".join(f"{a['name']} ({why(a)})" for a in bad), tracks=[a["name"] for a in bad], **evidence)
    if off:
        ctx.issue("subtitles", "subs_out_of_sync", "warning",
                  "Subtitles are offset: " + ", ".join(f"{a['name']} {a['sync']['offset']:+.1f}s" for a in off)
                  + (" (text matches once shifted)" if any(a.get("text") for a in off) else "") + ".",
                  offset=off[0]["sync"]["offset"], tracks=[a["name"] for a in off], **evidence)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ====================================================================== multi-episode

def check_multi_episode(ctx: Ctx):
    if ctx.item["kind"] != "series":
        return
    eps = ctx.episodes
    mapped = len(eps)
    dur = ctx.info["duration"]
    ctx.result["episodes_mapped"] = mapped
    boundaries = [0.0]
    if {i["code"] for i in ctx.issues} & DURATION_CODES:
        ctx.result["segment_breaks"] = "skipped: the file's length is already off, so break positions can't be predicted"
    elif mapped >= 2 and ctx.pcfg.get("subtitles") and ctx.info["video"]:
        # expected breaks come from the file's own length, split evenly between its mapped episodes
        for k in range(mapped - 1):
            target = dur * (k + 1) / mapped
            ch = [c["start"] for c in ctx.info["chapters"] if abs(c["start"] - target) < 120 and c["start"] > 30]
            ctx.step(f"finding segment break {k + 1}")
            scan = media.boundary_scan(ctx.path, max(0.0, target - 150), 300)
            br = analysis.nearest_break(target, scan["black"], scan["silence"])
            if br is None and ch:
                br = min(ch, key=lambda c: abs(c - target))
            if br is None:
                ctx.issue("multi_episode", "segment_break_not_found", "info",
                          f"Couldn't find a clear segment break near {target / 60:.1f} min.", target=target)
                br = target
            boundaries.append(br)
    ctx.result["segment_starts"] = boundaries


def queue_reviews(ctx: Ctx, rng: random.Random):
    if not ctx.info.get("video") or not ctx.info.get("duration"):
        return
    rcfg = ctx.cfg["review"]
    policy = rcfg.get("title_cards", "multi_episode")
    dur = ctx.info["duration"]
    codes = {i["code"] for i in ctx.issues}
    flagged = bool(codes & (DURATION_CODES | {"subs_mismatch", "subs_text_mismatch", "wrong_content"}))
    if ctx.item["kind"] == "series":
        eps = ctx.episodes
        want = policy == "always" or (policy == "multi_episode" and (len(eps) >= 2 or "double_length" in codes
                                                                   or ctx.result.get("runtime_disputed"))) \
            or (policy in ("flagged", "multi_episode") and flagged)
        if want and policy != "never":
            starts = ctx.result.get("segment_starts") or [0.0]
            per = max(2, 8 // len(starts))
            offsets = [4, 12, 25, 45, 70, 100][:per]
            ts = sorted({round(min(dur - 1, s + o), 1) for s in starts for o in offsets})
            listing = "; ".join(f"S{e.get('season', 0):02d}E{e.get('episode', 0):02d} \"{e.get('title', '')}\"" for e in eps)
            review = {
                "kind": "title_cards", "timestamps": ts[:8],
                "question": (f"Series: {ctx.item['title']}. This file should contain, in order: {listing}. "
                             f"Look for title cards / episode titles in these frames. Do they match the expected "
                             f"episode(s) and order? Is anything missing or extra?"),
                "context": {"episodes": eps, "segment_starts": starts}}
            if rcfg.get("ocr_title_cards", True) and ocr.available():
                auto_title_cards(ctx, review)
            ctx.reviews.append(review)
    elif policy != "never":
        ratio = ctx.result.get("runtime_ratio")
        reasons = []
        if flagged:
            reasons.append("its length or subtitles look wrong")
        if "original_audio_missing" in codes:
            reasons.append("the audio isn't in the film's original language")
        if ratio and not 0.85 <= ratio <= 1.15 and not flagged:
            reasons.append(f"its runtime is {ratio:.0%} of the listed runtime")
        if reasons:
            ts = [round(dur * p, 1) for p in (0.01, 0.05, 0.15, 0.35, 0.55, 0.75, 0.9)]
            extra = db.unj(ctx.item.get("extra"), {}) or {}
            orig = extra.get("originalTitle")
            found = sorted({langs.name(t.get("effective")) for t in ctx.tracks.values() if t.get("effective")})
            ctx.reviews.append({
                "kind": "identify_movie", "timestamps": ts,
                "question": (f"Expected movie: {ctx.item['title']} ({ctx.item.get('year') or '?'})"
                             + (f", original title \"{orig}\"" if orig and orig != ctx.item["title"] else "")
                             + f", original language {langs.name(ctx.item.get('original_language'))}. Checked because "
                             + " and ".join(reasons) + (f" (audio found: {', '.join(found)})" if found else "")
                             + ". Do these frames (opening credits especially) show this movie, not a different "
                             "film, a remake/miniseries with the same name, a sample or a trailer?"),
                "context": {"reasons": reasons}})
    if ctx.profile == "deep" and rcfg.get("visual_quality_on_deep"):
        ts = sorted(round(rng.uniform(dur * 0.05, dur * 0.95), 1) for _ in range(6))
        ctx.reviews.append({
            "kind": "visual_quality", "timestamps": ts,
            "question": ("Check these frames for: hardcoded/burned-in subtitles, watermarks or channel logos, "
                         "camcorder/screener look, wrong aspect ratio (stretched/squashed/windowboxed), heavy "
                         "compression artifacts, or color problems. Report anything that would justify a re-download."),
            "context": {}})


def auto_title_cards(ctx: Ctx, review: dict):
    """OCR the review's frames. All expected titles found -> auto-pass. Another episode's title found
    instead -> wrong content. No usable text -> stays in the manual queue."""
    ctx.step("reading title cards")
    texts = []
    for t in review["timestamps"]:
        try:
            texts.append(ocr.read_text(media.extract_ocr_frame(ctx.path, t)))
        except Exception:  # noqa: BLE001
            texts.append("")
    found = ocr.match(texts, ocr.series_episodes(ctx.item), ctx.item.get("title", ""))
    expected = {e["id"] for e in ctx.episodes}
    hit_expected = {m["id"] for m in found if m["id"] in expected}
    others = [m for m in found if m["id"] not in expected and m["score"] >= 0.9]
    ctx.result["title_cards_ocr"] = {"matches": found, "text": [t.strip()[:200] for t in texts]}
    if expected and hit_expected == expected and not others:
        review.update(status="done", verdict="ok", notes="auto: title cards matched by OCR - "
                      + ", ".join(f"\"{m['title']}\"" for m in found if m["id"] in expected))
    elif others and hit_expected != expected:
        o = others[0]
        ts = review["timestamps"][o["frame"]] if 0 <= o["frame"] < len(review["timestamps"]) else None
        label = f"S{(o.get('season') or 0):02d}E{(o.get('episode') or 0):02d}" if o.get("episode") is not None else ""
        ctx.issue("identity", "wrong_content", "error",
                  f"Title card reads \"{o['title']}\" {label} but the file is mapped to "
                  + ", ".join(f"\"{e.get('title', '')}\"" for e in ctx.episodes) + ".",
                  ocr_matches=found, evidence_ts=[ts] if ts is not None else review["timestamps"][:2])
        review["notes"] = "OCR found a different episode's title - please confirm"
    elif found:
        review["notes"] = "OCR matched some titles: " + ", ".join(m["title"] for m in found)


SUB_FAIL = {"subs_mismatch", "subs_mismatch_partial", "subs_text_mismatch", "subs_out_of_sync"}
BROKEN = {"truncated", "unreadable"}
# checks that can't be trusted once the file is known to be cut short / unreadable
DOWNSTREAM_OF_BROKEN = {"silent_audio", "decode_errors", "decode_errors_severe", "decode_stopped_early",
                        "duration_short", "duration_short_warn", "stream_duration_mismatch", "frozen_video",
                        "black_video", "segment_break_not_found", "language_unconfirmed", "dubtitles"} | SUB_FAIL
DOWNSTREAM_OF_WRONG = SUB_FAIL | DURATION_CODES | {"segment_break_not_found", "dubtitles"}


def consolidate(ctx: Ctx):
    """Group findings by root cause so one problem doesn't show up as five issues with the wrong fix."""
    codes = {i["code"] for i in ctx.issues}
    suppressed = []

    def drop(which: set, because: str):
        keep = []
        for i in ctx.issues:
            if i["code"] in which:
                suppressed.append({"code": i["code"], "message": i["message"], "because": because})
            else:
                keep.append(i)
        ctx.issues[:] = keep

    root = next((c for c in ("unreadable", "truncated") if c in codes), None)
    if root:
        drop(DOWNSTREAM_OF_BROKEN, root)
    else:
        # wrong video reported as wrong subtitles: subs fail AND something else is off -> it's the content
        sub = next((i for i in ctx.issues if i["code"] == "subs_mismatch"), None)
        dur_issue = next((i for i in ctx.issues if i["code"] in DURATION_CODES - {"duration_long"}), None)
        several_failed = bool(sub and sub["details"].get("all_tracks_failed") and sub["details"].get("track_count", 0) >= 2)
        wrong = next((i for i in ctx.issues if i["code"] == "wrong_content"), None)
        if sub and (dur_issue or several_failed or wrong):
            merged = [i for i in ctx.issues if i["code"] in DOWNSTREAM_OF_WRONG]
            if not wrong:
                reasons = [sub["message"].split(" (")[0]] + ([dur_issue["message"]] if dur_issue else []) \
                    + (["every subtitle track fails"] if several_failed else [])
                ctx.issue("identity", "wrong_content", "error",
                          "This file probably isn't the episode/movie it's mapped to: " + "; ".join(reasons)
                          + ". Re-download (or check the title cards) rather than replacing subtitles.",
                          evidence=[{"code": i["code"], "message": i["message"]} for i in merged])
            drop(DOWNSTREAM_OF_WRONG, "wrong_content")
        elif wrong:
            drop(DOWNSTREAM_OF_WRONG - DURATION_CODES, "wrong_content")
    if suppressed:
        ctx.result["suppressed"] = suppressed


def _audio_evidence(ctx: Ctx) -> str | None:
    """Per-track language evidence kept on the file row, so retag batches never re-read whole scan results."""
    audio = (ctx.info or {}).get("audio") or []
    if not audio or not ctx.tracks:
        return None
    return db.j({"scan_started": ctx.live.get("started_at") or time.time(), "container": ctx.info.get("format", ""),
                 "tracks": _track_view(ctx), "subs": ctx.result.get("subtitle_tracks") or []})


def _track_view(ctx: Ctx) -> list[dict]:
    """Every audio track (analysed or not) with its real flags and the language verdict, if any."""
    out = []
    for i, a in enumerate((ctx.info or {}).get("audio") or []):
        ai = a.get("a_index", i)
        t = ctx.tracks.get(ai, {})
        out.append({"a_index": ai, "tag": a.get("lang", ""), "title": a.get("title", ""), "codec": a.get("codec"),
                    "channels": a.get("channels"), "default": bool(a.get("default")),
                    "commentary": bool(a.get("commentary")), "forced": bool(a.get("forced")),
                    "hi": bool(a.get("hearing_impaired") or a.get("visual_impaired")),
                    "analysed": bool(t), "speech_s": t.get("speech_s"),
                    "effective": t.get("effective", a.get("lang", "") if not t else ""), "confident": bool(t.get("confident")),
                    "all_agree": bool(t.get("all_agree")),
                    "windows": [{"lang": w["lang"], "prob": w["prob"]} for w in t.get("windows", [])]})
    return out


# ====================================================================== persist

def persist(ctx: Ctx, scan_id: int, status: str, error: str, elapsed: float) -> dict:
    from . import actions

    now = time.time()
    fid = ctx.file["id"]
    if status == "cancelled":
        db.x("UPDATE scans SET status='cancelled', finished_at=? WHERE id=?", (now, scan_id))
        return {"scan_id": scan_id, "status": status}
    ctx.result["elapsed_s"] = round(elapsed, 1)
    with db.tx() as c:
        ignored = {r[0] for r in c.execute("SELECT DISTINCT code FROM issues WHERE file_id=? AND status='ignored'", (fid,))}
        c.execute("UPDATE issues SET status='resolved', updated_at=? WHERE file_id=? AND status='open'", (now, fid))
        for i in ctx.issues:
            c.execute("INSERT INTO issues(file_id, scan_id, category, code, severity, message, details, status, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (fid, scan_id, i["category"], i["code"], i["severity"], i["message"], db.j(i["details"]),
                       "ignored" if i["code"] in ignored else "open", now, now))
        for r in ctx.reviews:
            c.execute("UPDATE reviews SET status='dismissed', updated_at=? WHERE file_id=? AND kind=? AND status='pending'",
                      (now, fid, r["kind"]))
            c.execute("INSERT INTO reviews(file_id, scan_id, kind, question, timestamps, context, status, verdict, "
                      "notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (fid, scan_id, r["kind"], r["question"], db.j(r["timestamps"]), db.j(r["context"]),
                       r.get("status", "pending"), r.get("verdict", ""), r.get("notes", ""), now, now))
        open_sev = [r[0] for r in c.execute("SELECT severity FROM issues WHERE file_id=? AND status='open'", (fid,))]
        fstatus = "failed" if status == "failed" else (db.worst(open_sev) or "ok")
        c.execute("UPDATE files SET status=?, open_issues=?, last_scan_at=?, last_scan_id=?, last_profile=?, "
                  "duration=COALESCE(?, duration), audio_evidence=COALESCE(?, audio_evidence) WHERE id=?",
                  (fstatus, len(open_sev), now, scan_id, ctx.profile, (ctx.info or {}).get("duration") or None,
                   _audio_evidence(ctx), fid))
        c.execute("UPDATE scans SET status=?, finished_at=?, result=?, error=? WHERE id=?",
                  (status, now, db.j(ctx.result), error, scan_id))
        c.execute("DELETE FROM queue WHERE file_id=?", (fid,))
    label = f"{ctx.item['title']} {ctx.file.get('label', '')}".strip()
    db.log(f"Scanned {label} [{ctx.profile}] -> {fstatus} ({len(ctx.issues)} issues, {elapsed:.0f}s)",
           "warning" if fstatus in ("error", "critical", "failed") else "info")
    if status == "done":
        try:
            from . import retag

            from . import defaults

            retag.after_scan(fid, ctx.info or {}, scan_id)
            retag.refresh_item(ctx.item["id"])
            defaults.refresh_item(ctx.item["id"])
        except Exception:  # noqa: BLE001
            log.exception("retag bookkeeping failed")
        try:
            actions.reconcile(fid)
            actions.propose_for_file(fid)
        except Exception:  # noqa: BLE001
            log.exception("proposing actions failed")
    return {"scan_id": scan_id, "status": status, "file_status": fstatus, "issues": ctx.issues,
            "reviews": len(ctx.reviews), "error": error}
