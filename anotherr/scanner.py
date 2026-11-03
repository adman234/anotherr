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

from . import analysis, db, langs, media, ocr, runtime, settings, stt

log = logging.getLogger("anotherr.scan")

CURRENT: dict = {}
CANCEL = threading.Event()
_scan_lock = threading.Lock()

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

    def issue(self, category: str, code: str, severity: str, message: str, **details):
        self.issues.append({"category": category, "code": code, "severity": severity, "message": message,
                            "details": details})

    def step(self, name: str):
        if CANCEL.is_set():
            raise Cancelled()
        now = time.time()
        prev, prev_at = CURRENT.get("step"), CURRENT.get("step_at")
        if prev and prev_at:
            group = prev.split(":")[0].split(" segment")[0].split(" track")[0].strip()
            t = self.result.setdefault("timings", {})
            t[group] = round(t.get(group, 0) + now - prev_at, 1)
        CURRENT["step"] = name
        CURRENT["step_at"] = now

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


def scan_file(file_id: int, profile: str | None = None, source: str = "manual", seed: int | None = None) -> dict:
    with _scan_lock:
        CANCEL.clear()
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
        CURRENT.clear()
        CURRENT.update(file_id=file_id, scan_id=scan_id, title=item["title"], label=f.get("label", ""),
                       profile=profile, started_at=started, step="starting")
        ctx = Ctx(file=f, item=item, cfg=cfg, profile=profile, pcfg=pcfg, seed=seed)
        status, error = "done", ""
        try:
            run_pipeline(ctx)
        except Cancelled:
            status = "cancelled"
        except Exception as e:  # noqa: BLE001
            status, error = "failed", f"{e}\n{traceback.format_exc()[-1500:]}"
            log.exception("scan failed for file %s", file_id)
        finally:
            CURRENT.clear()
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


def check_runtime(ctx: Ctx, dur: float):
    is_movie = ctx.item["kind"] == "movie"
    if is_movie:
        per_ep, mapped, est = float(ctx.item.get("runtime") or 0), 1, {"source": "metadata"}
    else:
        mapped = max(1, len(ctx.episodes))
        est = runtime.estimate(ctx.item, ctx.file, ctx.episodes)
        per_ep = float(est.get("per_episode_min") or 0)
    ctx.result["runtime_estimate"] = est
    if per_ep <= 0:
        return
    exp = per_ep * mapped * 60
    ctx.result["expected_runtime_s"] = round(exp, 1)
    ratio = dur / exp
    ctx.result["runtime_ratio"] = round(ratio, 3)
    # "metadata (matches series files)" is verified by the files, so it gets the strict thresholds too
    from_files = est.get("source") in ("series files", "metadata (matches series files)")
    basis = (f"{mapped} x ~{per_ep:.1f} min (checked against this series' files)" if from_files
             else f"~{exp / 60:.0f} min ({est.get('source')})")
    d = {"ratio": round(ratio, 2), "expected_min": round(exp / 60, 1), "mapped_episodes": mapped,
         "per_episode_min": round(per_ep, 2), "basis": est.get("source"), "samples": est.get("samples")}
    if is_movie:
        short_err, short_warn, long_info = 0.6, 0.85, 1.6
    elif from_files:
        short_err, short_warn, long_info = 0.6, 0.75, 2.6
    else:  # unverified metadata (e.g. 30 min for a show numbered by 11 min segments): only warn unless far off
        short_err, short_warn, long_info = 0.3, 0.62, 2.6
    if ratio < short_err:
        ctx.issue("integrity", "duration_short", "error",
                  f"File is {dur / 60:.1f} min but {basis} was expected (wrong content, sample, or truncated).", **d)
    elif ratio < short_warn:
        ctx.issue("integrity", "duration_short_warn", "warning",
                  f"File is shorter than expected ({dur / 60:.1f} min vs {basis}).", **d)
    elif not is_movie and mapped == 1 and 1.7 <= ratio <= 2.4:
        ctx.issue("multi_episode", "double_length", "warning",
                  f"Double-length file ({dur / 60:.1f} min vs ~{per_ep:.1f} min per episode for this series): a "
                  "special or double episode, or a different/second episode mapped as this one. Title cards queued "
                  "for review.", **d)
    elif ratio > long_info:
        ctx.issue("integrity", "duration_long", "info",
                  f"File is much longer than expected ({dur / 60:.0f} min vs {basis}).", **d)


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
    total_err = sum(r.get("error_count", 0) for r in results)
    bad_segs = sum(1 for r in results if r.get("error_count", 0) > 5)
    last = len(results) - 1
    for i, r in enumerate(results):
        short = r["decoded"] < r["dur"] * 0.9 - 2
        if short and i == last:
            ctx.issue("integrity", "truncated", "critical",
                      f"Decoding stopped {r['decoded']:.0f}s into the final {r['dur']:.0f}s segment - file looks truncated.",
                      segment_start=r["start"])
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
            ctx.issue("video", "black_video", "warning",
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


def check_language(ctx: Ctx):
    exp = ctx.expected
    dub = langs.norm(ctx.cfg["anime"]["dub_language"])
    ctx.result["expected_language"] = exp
    tracks = list(ctx.tracks.values())
    if not tracks:
        return
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
                          a_index=t["a_index"], windows=t["windows"])
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
        if unsure and len(unsure) == len(tracks):
            ctx.issue("language", "language_unconfirmed", "warning",
                      f"Could not confirm {what} language ({langs.name(exp)}) audio - not enough clear speech.")
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
        ctx.issue("language", "default_audio_not_preferred", "warning",
                  f"Default audio track is {langs.name(default['effective'])}, not {langs.name(exp)}.")
    if ctx.item.get("is_anime") and ctx.cfg["anime"]["prefer_dual_audio"] and not lang_match(exp, dub):
        has_dub = any(lang_match(t["effective"], dub) for t in tracks)
        ctx.result["dual_audio"] = bool(orig) and has_dub
        if not has_dub:
            ctx.issue("language", "no_dub", "info",
                      f"Dual audio preferred: no {langs.name(dub)} dub track.")


# ====================================================================== subtitles

DUB_TITLE = re.compile(r"\bdub|dubtitle|\bcc\b|closed.?caption", re.I)


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
        ctx.issue("subtitles", "default_subs_signs", "info",
                  "The default-flagged subtitle track is signs/songs only; the full track is not default.")

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
        want = policy == "always" or (policy == "multi_episode" and (len(eps) >= 2 or "double_length" in codes)) \
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
    found = ocr.match(texts, ocr.series_episodes(ctx.item))
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
                  "duration=COALESCE(?, duration) WHERE id=?",
                  (fstatus, len(open_sev), now, scan_id, ctx.profile, (ctx.info or {}).get("duration") or None, fid))
        c.execute("UPDATE scans SET status=?, finished_at=?, result=?, error=? WHERE id=?",
                  (status, now, db.j(ctx.result), error, scan_id))
        c.execute("DELETE FROM queue WHERE file_id=?", (fid,))
    label = f"{ctx.item['title']} {ctx.file.get('label', '')}".strip()
    db.log(f"Scanned {label} [{ctx.profile}] -> {fstatus} ({len(ctx.issues)} issues, {elapsed:.0f}s)",
           "warning" if fstatus in ("error", "critical", "failed") else "info")
    if status == "done":
        try:
            actions.propose_for_file(fid)
        except Exception:  # noqa: BLE001
            log.exception("proposing actions failed")
    return {"scan_id": scan_id, "status": status, "file_status": fstatus, "issues": ctx.issues,
            "reviews": len(ctx.reviews), "error": error}
