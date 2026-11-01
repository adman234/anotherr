"""The scan pipeline: probe -> integrity -> audio/language -> subtitles -> multi-episode -> reviews."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
import traceback
from dataclasses import dataclass, field

import numpy as np

from . import analysis, db, langs, media, settings, stt

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
    ctx.step("done")  # closes the timing of the last step


# ====================================================================== integrity

def expected_runtime_min(ctx: Ctx) -> float:
    if ctx.item["kind"] == "movie":
        return float(ctx.item.get("runtime") or 0)
    eps = ctx.episodes
    if not eps:
        return float(ctx.item.get("runtime") or 0)
    return float(sum((e.get("runtime") or ctx.item.get("runtime") or 0) for e in eps))


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
    exp = expected_runtime_min(ctx) * 60
    ctx.result["expected_runtime_s"] = exp
    if exp > 0:
        ratio = dur / exp
        ctx.result["runtime_ratio"] = round(ratio, 3)
        mapped = len(ctx.episodes)
        is_movie = ctx.item["kind"] == "movie"
        if ratio < (0.6 if is_movie else 0.45):
            ctx.issue("integrity", "duration_short", "error",
                      f"File is {dur / 60:.1f} min but ~{exp / 60:.0f} min was expected "
                      f"(wrong content, sample, or truncated).", ratio=round(ratio, 2))
        elif ratio < (0.85 if is_movie else 0.62):
            ctx.issue("integrity", "duration_short_warn", "warning",
                      f"File is shorter than expected ({dur / 60:.1f} of ~{exp / 60:.0f} min).", ratio=round(ratio, 2))
        elif not is_movie and mapped == 1 and 1.7 <= ratio <= 2.4:
            ctx.issue("multi_episode", "possible_multi_episode", "warning",
                      f"File is ~{ratio:.1f}x the episode runtime - it may contain multiple episodes "
                      f"but is mapped to one.", ratio=round(ratio, 2))
        elif not is_movie and mapped >= 2 and ratio < 0.7:
            ctx.issue("multi_episode", "multi_episode_too_short", "warning",
                      f"File is mapped to {mapped} episodes but is only {dur / 60:.1f} min long.", ratio=round(ratio, 2))
        elif ratio > (1.6 if is_movie else 2.6):
            ctx.issue("integrity", "duration_long", "info",
                      f"File is much longer than expected ({dur / 60:.0f} vs ~{exp / 60:.0f} min).", ratio=round(ratio, 2))
    v = info["video"][0] if info["video"] else None
    if v and v.get("duration") and abs(v["duration"] - dur) > max(30, 0.05 * dur):
        ctx.issue("integrity", "stream_duration_mismatch", "warning",
                  f"Video stream length ({v['duration'] / 60:.1f} min) differs from container ({dur / 60:.1f} min).")


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
            results[i]["black"], results[i]["freeze"] = vc["black"], vc["freeze"]
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
    if total_err > 20 or bad_segs >= 2:
        ctx.issue("integrity", "decode_errors_severe", "error",
                  f"{total_err} decode errors across {len(results)} sampled segments.",
                  sample=[e for r in results for e in r["errors"]][:10])
    elif total_err:
        ctx.issue("integrity", "decode_errors", "warning", f"{total_err} decode error(s) in sampled segments.",
                  sample=[e for r in results for e in r["errors"]][:10])
    mids = results[1:-1] if len(results) > 2 else []
    for r in mids:
        black = sum(b[2] for b in r["black"])
        if black > 0.5 * r["dur"]:
            ctx.issue("video", "black_video", "warning",
                      f"{black:.0f}s of black video in the segment at {r['start'] / 60:.1f} min.", start=r["start"])
            break
    for r in mids:
        if sum(r["freeze"]) > 0.6 * r["dur"]:
            ctx.issue("video", "frozen_video", "warning",
                      f"Video appears frozen for most of the segment at {r['start'] / 60:.1f} min.", start=r["start"])
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


# ====================================================================== audio & language

def _audio_segments(ctx: Ctx) -> list[int]:
    idx = list(range(len(ctx.segments)))
    mids = idx[1:-1] if len(idx) > 2 else idx
    return mids or idx


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
             "level_db": round(max(levels) if levels else -120, 1), "detected": "", "prob": 0.0}
        if levels and max(levels) < -70:
            ctx.issue("audio", "silent_audio", "error", f"Audio track #{ai + 1} ({langs.name(a['lang'])}) is silent.",
                      a_index=ai)
        elif speech_s >= 4:
            ctx.step(f"language detection track {ai + 1}")
            clip = np.concatenate([stt.speech_audio(au, iv, 30) for au, iv in speech_all])[: stt.SR * 30]
            try:
                lang, prob, top = stt.WHISPER.detect_language(clip)
                t.update(detected=langs.norm(lang) or lang, prob=round(prob, 3), top=top)
            except Exception as e:  # noqa: BLE001
                t["error"] = str(e)
                ctx.result.setdefault("warnings", []).append(f"language detection failed: {e}")
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
        confident = t["detected"] and t["prob"] >= 0.6 and t["speech_s"] >= 6
        t["effective"] = t["detected"] if confident else t["tag"]
        t["confident"] = bool(confident)
        if confident and t["tag"] and not lang_match(t["tag"], t["detected"]):
            ctx.issue("language", "audio_mislabeled", "warning",
                      f"Audio track #{t['a_index'] + 1} is tagged {langs.name(t['tag'])} but sounds like "
                      f"{langs.name(t['detected'])} ({t['prob']:.0%}).", a_index=t["a_index"])
        elif confident and not t["tag"]:
            ctx.issue("language", "audio_untagged", "info",
                      f"Audio track #{t['a_index'] + 1} has no language tag; detected {langs.name(t['detected'])}.",
                      a_index=t["a_index"])
    if not exp:
        ctx.issue("language", "original_language_unknown", "info",
                  "Original language unknown in Sonarr/Radarr - set a language override to check it.")
        return
    orig = [t for t in tracks if lang_match(t["effective"], exp)]
    orig.sort(key=lambda t: (not t["confident"], not t["default"]))
    if orig:
        ctx.orig_track = orig[0]["a_index"]
    else:
        unsure = [t for t in tracks if not t["confident"]]
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
        kind = analysis.classify_track(len(cues), span, s["title"], s["forced"], min_rate)
        analysed.append({"track": s, "cues": cues, "kind": kind, "per_min": round(len(cues) / max(span / 60, 1), 1)})

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
    if needs and dflt and dflt["kind"] == "signs" and full:
        ctx.issue("subtitles", "default_subs_signs", "info",
                  "The default-flagged subtitle track is signs/songs only; the full track is not default.")

    # ---- sync against original-language speech
    at = ctx.orig_track
    if at is not None and full:
        verdicts = []
        for a in full[:3]:
            ctx.step(f"subtitle sync: {a['track']['title'] or a['track']['codec']}")
            per = []
            for si in _audio_segments(ctx):
                if (at, si) not in ctx.vad:
                    continue
                s, d = ctx.segments[si]
                per.append(analysis.sync_offset(ctx.vad[(at, si)], [(c[0], c[1]) for c in a["cues"]], s, d))
            agg = analysis.aggregate_sync(per)
            agg["track"] = a["track"]["title"] or a["track"]["codec"]
            agg["external"] = a["track"]["external"]
            a["sync"] = agg
            verdicts.append(agg)
        ctx.result["subtitle_sync"] = verdicts
        known = [v for v in verdicts if v["verdict"] != "unknown"]
        if known:
            bad = [v for v in known if v["verdict"] == "mismatch"]
            off = [v for v in known if v["verdict"] == "offset"]
            if len(bad) == len(known):
                ctx.issue("subtitles", "subs_mismatch", "error" if needs else "warning",
                          "Subtitle timing does not match the dialogue at all - likely subs for a different "
                          "episode/cut.", tracks=[v["track"] for v in bad])
            elif bad:
                ctx.issue("subtitles", "subs_mismatch_partial", "warning",
                          f"{len(bad)} of {len(known)} subtitle tracks do not match the dialogue.",
                          tracks=[v["track"] for v in bad])
            if off and len(off) + len(bad) == len(known):
                ctx.issue("subtitles", "subs_out_of_sync", "warning",
                          f"Subtitles are offset by {off[0]['offset']:+.1f}s.", offset=off[0]["offset"],
                          tracks=[v["track"] for v in off])

    # ---- content checks (transcribe a couple of windows)
    text_full = [a for a in full if a["track"]["text"]]
    if not text_full:
        return
    native = next((t for t in ctx.tracks.values() if lang_match(t.get("effective", ""), sub_lang)), None)
    if not native:
        return
    is_dub = not lang_match(exp, sub_lang) and bool(exp)
    if is_dub and not (ctx.item.get("is_anime") and ctx.cfg["anime"]["detect_dubtitles"]):
        return
    ai = native["a_index"]
    segs = sorted([si for si in _audio_segments(ctx) if (ai, si) in ctx.audio],
                  key=lambda si: -sum(e - s for s, e in ctx.vad.get((ai, si), [])))[:2]
    if not segs:
        return
    ctx.step("transcribing for subtitle content check")
    transcript = []
    for si in segs:
        try:
            transcript.append((si, stt.WHISPER.transcribe(ctx.audio[(ai, si)], langs.iso1(sub_lang))))
        except Exception as e:  # noqa: BLE001
            ctx.result.setdefault("warnings", []).append(f"transcription failed: {e}")
            return
    checks = []
    for a in text_full[:2]:
        t_all, s_all = [], []
        for si, segs_t in transcript:
            s, d = ctx.segments[si]
            t_all += [x[2] for x in segs_t]
            s_all += [c[2] for c in a["cues"] if c[1] > s and c[0] < s + d]
        ov = analysis.text_overlap(" ".join(t_all), " ".join(s_all))
        ov["track"] = a["track"]["title"] or a["track"]["codec"]
        checks.append(ov)
        if ov["recall"] is None:
            continue
        if is_dub and ov["recall"] >= 0.55 and ov["precision"] >= 0.55:
            ctx.issue("subtitles", "dubtitles", "warning",
                      f"Subtitle track '{ov['track']}' matches the {langs.name(sub_lang)} dub almost word for word - "
                      "these are dub captions ('dubtitles'), not a real translation.", **ov)
        elif not is_dub and ov["recall"] < 0.2:
            ctx.issue("subtitles", "subs_text_mismatch", "warning",
                      f"Subtitle text barely matches the spoken dialogue on track '{ov['track']}'.", **ov)
    ctx.result["subtitle_content"] = checks
    ctx.result["transcript_sample"] = " ".join(x[2] for _, t in transcript for x in t)[:1500]


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
    if mapped >= 2 and ctx.pcfg.get("subtitles") and ctx.info["video"]:
        weights = [(e.get("runtime") or ctx.item.get("runtime") or 1) for e in eps]
        total_w = sum(weights)
        cum = 0.0
        for k in range(mapped - 1):
            cum += weights[k]
            target = dur * cum / total_w
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
    flagged = bool(codes & {"duration_short", "duration_short_warn", "possible_multi_episode",
                            "multi_episode_too_short", "subs_mismatch"})
    if ctx.item["kind"] == "series":
        eps = ctx.episodes
        want = policy == "always" or (policy == "multi_episode" and (len(eps) >= 2 or "possible_multi_episode" in codes)) \
            or (policy in ("flagged", "multi_episode") and flagged)
        if want and policy != "never":
            starts = ctx.result.get("segment_starts") or [0.0]
            per = max(2, 8 // len(starts))
            offsets = [4, 12, 25, 45, 70, 100][:per]
            ts = sorted({round(min(dur - 1, s + o), 1) for s in starts for o in offsets})
            listing = "; ".join(f"S{e.get('season', 0):02d}E{e.get('episode', 0):02d} \"{e.get('title', '')}\"" for e in eps)
            ctx.reviews.append({
                "kind": "title_cards", "timestamps": ts[:8],
                "question": (f"Series: {ctx.item['title']}. This file should contain, in order: {listing}. "
                             f"Look for title cards / episode titles in these frames. Do they match the expected "
                             f"episode(s) and order? Is anything missing or extra?"),
                "context": {"episodes": eps, "segment_starts": starts}})
    elif flagged and policy != "never":
        ts = [round(dur * p, 1) for p in (0.05, 0.15, 0.35, 0.55, 0.75, 0.9)]
        ctx.reviews.append({
            "kind": "identify_movie", "timestamps": ts,
            "question": (f"Expected movie: {ctx.item['title']} ({ctx.item.get('year') or '?'}). The file's length is "
                         f"suspicious. Do these frames look like this movie (not a different film, sample, or trailer)?"),
            "context": {}})
    if ctx.profile == "deep" and rcfg.get("visual_quality_on_deep"):
        ts = sorted(round(rng.uniform(dur * 0.05, dur * 0.95), 1) for _ in range(6))
        ctx.reviews.append({
            "kind": "visual_quality", "timestamps": ts,
            "question": ("Check these frames for: hardcoded/burned-in subtitles, watermarks or channel logos, "
                         "camcorder/screener look, wrong aspect ratio (stretched/squashed/windowboxed), heavy "
                         "compression artifacts, or color problems. Report anything that would justify a re-download."),
            "context": {}})


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
            c.execute("INSERT INTO reviews(file_id, scan_id, kind, question, timestamps, context, status, created_at, "
                      "updated_at) VALUES (?,?,?,?,?,?,'pending',?,?)",
                      (fid, scan_id, r["kind"], r["question"], db.j(r["timestamps"]), db.j(r["context"]), now, now))
        open_sev = [r[0] for r in c.execute("SELECT severity FROM issues WHERE file_id=? AND status='open'", (fid,))]
        fstatus = "failed" if status == "failed" else (db.worst(open_sev) or "ok")
        c.execute("UPDATE files SET status=?, open_issues=?, last_scan_at=?, last_scan_id=?, last_profile=? WHERE id=?",
                  (fstatus, len(open_sev), now, scan_id, ctx.profile, fid))
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
