"""ffmpeg / ffprobe helpers."""
from __future__ import annotations

import glob
import json
import os
import random
import re
import subprocess
import tempfile

import numpy as np

from . import langs

FFMPEG = os.environ.get("ANOTHERR_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("ANOTHERR_FFPROBE", "ffprobe")
SR = 16000

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text", "srt"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
SIDE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup")

# ffmpeg [error] lines that are harmless for our purposes
_BENIGN = re.compile(
    r"(deprecated pixel format|Timestamps are unset|non monotonically increasing dts|"
    r"Application provided invalid|Could not find codec parameters|frame size not set|"
    r"Last message repeated|Error parsing Opus packet header|Discarding ID3)", re.I)


def run(cmd: list[str], timeout: float, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, text=not binary,
                          **({} if binary else {"errors": "replace"}))


def nice_prefix() -> list[str]:
    pre = []
    if os.path.exists("/usr/bin/ionice"):
        pre += ["ionice", "-c2", "-n7"]  # best-effort/lowest; idle class can starve on a busy array
    if os.path.exists("/usr/bin/nice"):
        pre += ["nice", "-n", "15"]
    return pre


# ---------------------------------------------------------------- probe

def ffprobe(path: str) -> dict:
    p = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", path],
            timeout=120)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffprobe failed").strip()[-500:])
    return json.loads(p.stdout or "{}")


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _stream_duration(s: dict) -> float:
    d = _f(s.get("duration"))
    if d:
        return d
    tags = {k.upper(): v for k, v in (s.get("tags") or {}).items()}
    for k, v in tags.items():
        if k.startswith("DURATION"):
            m = re.match(r"(\d+):(\d+):([\d.]+)", str(v))
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0.0


def summarize(probe: dict) -> dict:
    fmt = probe.get("format") or {}
    video, audio, subs = [], [], []
    for s in probe.get("streams", []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        base = {
            "index": s.get("index"),
            "codec": s.get("codec_name", ""),
            "lang_tag": tags.get("language", ""),
            "lang": langs.norm(tags.get("language", "")),
            "title": tags.get("title", ""),
            "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")),
            "duration": _stream_duration(s),
        }
        t = s.get("codec_type")
        if t == "video":
            if disp.get("attached_pic"):
                continue
            base.update(width=s.get("width"), height=s.get("height"), fps=s.get("avg_frame_rate"),
                        field_order=s.get("field_order", ""), v_index=len(video))
            video.append(base)
        elif t == "audio":
            base.update(channels=s.get("channels"), a_index=len(audio),
                        commentary=bool(disp.get("comment")) or "comment" in base["title"].lower(),
                        hearing_impaired=bool(disp.get("hearing_impaired")),
                        visual_impaired=bool(disp.get("visual_impaired")))
            audio.append(base)
        elif t == "subtitle":
            base.update(s_index=len(subs), hearing_impaired=bool(disp.get("hearing_impaired")),
                        text=base["codec"] in TEXT_SUB_CODECS, external=False)
            subs.append(base)
    return {
        "duration": _f(fmt.get("duration")),
        "size": int(_f(fmt.get("size"))),
        "format": fmt.get("format_name", ""),
        "bit_rate": int(_f(fmt.get("bit_rate"))),
        "video": video, "audio": audio, "subs": subs,
        "chapters": [{"start": _f(c.get("start_time")), "end": _f(c.get("end_time")),
                      "title": (c.get("tags") or {}).get("title", "")} for c in probe.get("chapters", [])],
    }


def sidecar_subs(video_path: str) -> list[dict]:
    """External subtitle files next to the video (e.g. written by Bazarr)."""
    stem, _ = os.path.splitext(video_path)
    out = []
    for p in sorted(glob.glob(glob.escape(stem) + ".*")):
        if not p.lower().endswith(SIDE_EXTS) or p == video_path:
            continue
        parts = os.path.basename(p)[len(os.path.basename(stem)):].lower().strip(".").split(".")
        flags = set(parts[:-1])
        lang = ""
        for part in parts[:-1]:
            if langs.norm(part):
                lang = langs.norm(part)
                break
        ext = parts[-1]
        out.append({
            "path": p, "codec": {"srt": "subrip", "vtt": "webvtt", "sup": "hdmv_pgs_subtitle", "sub": "dvd_subtitle"}.get(ext, ext),
            "lang": lang, "lang_tag": lang, "title": os.path.basename(p), "default": False,
            "forced": "forced" in flags, "hearing_impaired": bool(flags & {"hi", "sdh", "cc"}),
            "text": ext in ("srt", "ass", "ssa", "vtt"), "external": True,
        })
    return out


def sidecar_subs_any(video_paths: list[str]) -> list[dict]:
    """sidecar_subs next to any of these views of one video (e.g. its physical disk path and its /mnt/user path),
    each subtitle file name once, first view first. Unraid can keep a folder's files on different disks or the
    cache; only the user share shows them all."""
    seen, out = set(), []
    for vp in dict.fromkeys(p for p in video_paths if p):
        for sd in sidecar_subs(vp):
            name = os.path.basename(sd["path"])
            if name not in seen:
                seen.add(name)
                out.append(sd)
    return out


# ---------------------------------------------------------------- sampling

def pick_segments(duration: float, n: int, seconds: float, rng: random.Random) -> list[tuple[float, float]]:
    """Start + end segments always (catch broken intros and truncation), the rest random."""
    if duration <= 0:
        return []
    if duration <= seconds * 1.2:
        return [(0.0, duration)]
    seconds = min(seconds, max(5.0, duration / max(n, 1)))
    segs = [(min(5.0, duration * 0.01), seconds), (max(0.0, duration - seconds - 1.0), seconds)]
    lo, hi = seconds + 10, duration - 2 * seconds - 10
    tries = 0
    while len(segs) < n and hi > lo and tries < 200:
        tries += 1
        s = rng.uniform(lo, hi)
        if all(abs(s - a) > seconds for a, _ in segs):
            segs.append((s, seconds))
    return sorted(segs)


# ---------------------------------------------------------------- decode / filters

_BLACK = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)")
_FREEZE = re.compile(r"freezedetect.freeze_duration:\s*([\d.]+)")
_IDET = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)")
_SILENCE = re.compile(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")


# ---- GPU (NVDEC) decode: None = untested, True = working, False = unavailable
HW: dict = {"cuda": None, "reason": ""}
_HW_LINE = re.compile(r"(cuda|nvdec|cuvid|hwaccel|hw_frames|hwdevice|Device creation failed|hwupload|hwdownload)", re.I)
_HW_FATAL = re.compile(r"(Cannot load lib(cuda|nvcuvid)|Device creation failed|No device available|CUDA_ERROR|"
                       r"Failed to init CUDA|cuInit|Could not dynamically load CUDA)", re.I)


def hw_wanted(mode: str) -> bool:
    return mode != "off" and HW["cuda"] is not False


def _split_errors(stderr: str) -> tuple[list[str], list[str]]:
    errs, hw = [], []
    for line in (stderr or "").splitlines():
        if not line.startswith(("[error]", "[fatal]")) or _BENIGN.search(line):
            continue
        (hw if _HW_LINE.search(line) else errs).append(line[:300])
    return errs, hw


def _note_hw(hw_lines: list[str], used: bool):
    if not used:
        return
    fatal = next((l for l in hw_lines if _HW_FATAL.search(l)), None)
    if fatal:
        if HW["cuda"] is not False:
            HW.update(cuda=False, reason=fatal)
            import logging
            logging.getLogger("anotherr.media").warning(
                "GPU decoding unavailable, using CPU: %s. On Unraid set NVIDIA_DRIVER_CAPABILITIES=compute,utility,video",
                fatal)
    elif HW["cuda"] is None:
        HW["cuda"] = True


def decode_segment(path: str, start: float, dur: float, threads: int = 2, has_video: bool = True,
                   hw: str = "auto", _force_cpu: bool = False) -> dict:
    """Decode a segment fully (GPU when possible) to find decoder errors and truncation."""
    use_hw = has_video and hw_wanted(hw) and not _force_cpu
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+error", "-threads", str(threads)]
    if use_hw:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-ss", f"{start:.3f}", "-i", path, "-t", f"{dur:.3f}"]
    if has_video:
        cmd += ["-map", "0:v:0"]
    cmd += ["-map", "0:a?", "-sn", "-dn", "-f", "null", "-", "-progress", "pipe:1", "-nostats"]
    try:
        p = run(cmd, timeout=max(300, dur * 20))
    except subprocess.TimeoutExpired:
        return {"start": start, "dur": dur, "decoded": 0.0, "errors": ["decode timed out"], "error_count": 1,
                "timeout": True, "black": [], "freeze": [], "idet": None, "gpu": use_hw}
    decoded = 0.0
    for m in re.finditer(r"out_time_us=(\d+)", p.stdout or ""):
        decoded = int(m.group(1)) / 1e6
    errors, hw_lines = _split_errors(p.stderr)
    _note_hw(hw_lines, use_hw)
    if use_hw and hw_lines and decoded < dur * 0.9 - 2:
        # the GPU attempt itself failed - redo on CPU so we don't report a false truncation
        return decode_segment(path, start, dur, threads, has_video, hw, _force_cpu=True)
    if p.returncode != 0 and not errors and not hw_lines:
        errors.append(f"ffmpeg exited with {p.returncode}")
    return {"start": start, "dur": dur, "decoded": decoded, "errors": errors[:50], "error_count": len(errors),
            "black": [], "freeze": [], "idet": None, "gpu": use_hw and not (hw_lines and _HW_FATAL.search(" ".join(hw_lines)))}


_FREEZE_START = re.compile(r"freezedetect.freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"freezedetect.freeze_end:\s*([\d.]+)")


def _freeze_intervals(stderr: str, offset: float, window_end: float) -> list[tuple[float, float]]:
    starts = [offset + float(v) for v in _FREEZE_START.findall(stderr or "")]
    ends = [offset + float(v) for v in _FREEZE_END.findall(stderr or "")]
    out = []
    for i, st in enumerate(starts):
        out.append((st, ends[i] if i < len(ends) else window_end))  # a freeze running to the end has no end line
    return out


_SHOWINFO_PTS = re.compile(r"showinfo.*?\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:\s*(-?[\d.]+)")
THUMB_W, THUMB_H = 64, 36


def video_checks(path: str, start: float, dur: float, min_span: float = 10.0) -> dict:
    """Cheap screening pass on keyframes only: black spans and *candidate* freezes.

    ffmpeg's freezedetect can't be used on keyframes alone (it treats the gap between two sparse
    frames as a freeze), so consecutive keyframes are compared here on 64x36 grey thumbnails using
    their real timestamps. A candidate is a run of practically identical keyframes spanning
    >= min_span seconds; confirm it with confirm_freeze() before reporting anything.
    """
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+info", "-skip_frame", "nokey",
                           "-ss", f"{start:.3f}", "-i", path, "-t", f"{dur:.3f}", "-map", "0:v:0",
                           "-vf", f"scale={THUMB_W}:{THUMB_H},format=gray,blackdetect=d=1.5:pix_th=0.10,showinfo",
                           "-fps_mode", "passthrough", "-an", "-sn", "-f", "rawvideo", "pipe:1"]
    try:
        p = run(cmd, timeout=300, binary=True)
    except subprocess.TimeoutExpired:
        return {"black": [], "freeze_candidates": [], "keyframes": 0}
    err = (p.stderr or b"").decode("utf-8", "replace")
    pts = [start + float(t) for _, t in _SHOWINFO_PTS.findall(err)]
    size = THUMB_W * THUMB_H
    n = min(len(pts), len(p.stdout or b"") // size)
    frames = np.frombuffer((p.stdout or b"")[: n * size], dtype=np.uint8).reshape(n, size).astype(np.int16) if n else None
    black = [(start + float(a), start + float(b), float(c)) for a, b, c in _BLACK.findall(err)]
    cands = []
    if n >= 2:
        same = [float(np.mean(np.abs(frames[i + 1] - frames[i]))) < 1.0 and float(frames[i].mean()) > 20
                for i in range(n - 1)]  # dark frames are blackdetect's job
        i = 0
        while i < n - 1:
            if same[i]:
                j = i
                while j < n - 1 and same[j]:
                    j += 1
                if pts[j] - pts[i] >= min_span:
                    cands.append((round(pts[i], 2), round(pts[j], 2)))
                i = j
            i += 1
    return {"black": black, "freeze_candidates": cands, "keyframes": n,
            "keyframe_pts": [round(t, 2) for t in pts[:200]]}


CONFIRM_W, CONFIRM_H = 320, 180


def confirm_freeze(path: str, start: float, dur: float, min_freeze: float = 10.0) -> dict:
    """Confirm a candidate freeze with a continuous decode (CPU, accurate seek, native frame rate),
    sampling one real frame per second.

    A 1 s pair counts as unchanged when fewer than 0.05% of pixels moved by more than compression
    noise (|diff| > 16 at 320x180) - a talking head's mouth alone changes far more than that. The span
    is confirmed when >= 90% of its pairs are unchanged and the longest unchanged run is >= min_freeze.
    Dark frames are ignored (black holds are reported by blackdetect, not as freezes).
    """
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}",
                           "-i", path, "-t", f"{dur:.3f}", "-map", "0:v:0",
                           "-vf", f"fps=1,scale={CONFIRM_W}:{CONFIRM_H},format=gray",
                           "-an", "-sn", "-f", "rawvideo", "pipe:1"]
    try:
        p = run(cmd, timeout=max(300, dur * 10), binary=True)
    except subprocess.TimeoutExpired:
        return {"confirmed": False, "error": "timeout", "pairs": 0}
    size = CONFIRM_W * CONFIRM_H
    n = len(p.stdout or b"") // size
    if n < 3:
        return {"confirmed": False, "pairs": 0, "frames": n}
    fr = np.frombuffer(p.stdout[: n * size], dtype=np.uint8).reshape(n, size).astype(np.int16)
    unchanged = []
    for i in range(n - 1):
        if fr[i].mean() <= 20:
            unchanged.append(False)
            continue
        moved = int(np.count_nonzero(np.abs(fr[i + 1] - fr[i]) > 16))
        unchanged.append(moved < 0.0005 * size)
    run_len = best = 0
    best_end = 0
    for i, u in enumerate(unchanged):
        run_len = run_len + 1 if u else 0
        if run_len > best:
            best, best_end = run_len, i + 1
    frac = sum(unchanged) / len(unchanged)
    ok = frac >= 0.9 and best >= min_freeze
    return {"confirmed": ok, "pairs": len(unchanged), "unchanged_fraction": round(frac, 3), "longest_s": best,
            "freeze": (round(start + best_end - best, 1), round(start + best_end, 1)) if best else None,
            "window": (round(start, 1), round(dur, 1))}


def idet_sample(path: str, start: float, dur: float = 10.0) -> dict | None:
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+info", "-ss", f"{start:.3f}",
                           "-i", path, "-t", f"{dur:.3f}", "-map", "0:v:0", "-vf", "idet", "-an", "-sn", "-f", "null", "-"]
    try:
        p = run(cmd, timeout=300)
    except subprocess.TimeoutExpired:
        return None
    m = _IDET.findall(p.stderr or "")
    if not m:
        return None
    tff, bff, prog, und = map(int, m[-1])
    return {"tff": tff, "bff": bff, "progressive": prog, "undetermined": und}


def full_decode(path: str, threads: int = 2, timeout: float = 4 * 3600, hw: str = "auto") -> dict:
    use_hw = hw_wanted(hw)
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+error", "-threads", str(threads)]
    if use_hw:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", path, "-map", "0:v:0?", "-map", "0:a?", "-f", "null", "-"]
    try:
        p = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"errors": ["full decode timed out"], "error_count": 1}
    errors, hw_lines = _split_errors(p.stderr)
    _note_hw(hw_lines, use_hw)
    return {"errors": errors[:100], "error_count": len(errors), "returncode": p.returncode}


def boundary_scan(path: str, start: float, dur: float) -> dict:
    """Low-res black + silence detection in a window (segment breaks in multi-episode files)."""
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+info",
                           "-ss", f"{start:.3f}", "-i", path, "-t", f"{dur:.3f}",
                           "-map", "0:v:0", "-vf", "scale=320:-2,blackdetect=d=0.3:pix_th=0.12",
                           "-map", "0:a:0?", "-af", "silencedetect=n=-45dB:d=0.3", "-f", "null", "-"]
    try:
        p = run(cmd, timeout=600)
    except subprocess.TimeoutExpired:
        return {"black": [], "silence": []}
    err = p.stderr or ""
    black = [(start + float(a), start + float(b)) for a, b, _ in _BLACK.findall(err)]
    silence = [(start + float(e) - float(d), start + float(e)) for e, d in _SILENCE.findall(err)]
    return {"black": black, "silence": silence}


# ---------------------------------------------------------------- audio / subs / frames

def extract_audio(path: str, a_index: int, start: float, dur: float) -> np.ndarray:
    return extract_audio_multi(path, [a_index], start, dur).get(a_index, np.zeros(0, dtype=np.float32))


def extract_audio_multi(path: str, a_indices: list[int], start: float, dur: float) -> dict[int, np.ndarray]:
    """Read one window once and pull out several audio tracks (16 kHz mono)."""
    out: dict[int, np.ndarray] = {}
    if not a_indices:
        return out
    with tempfile.TemporaryDirectory(prefix="anotherr-audio-") as td:
        cmd = nice_prefix() + [FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", path,
                               "-t", f"{dur:.3f}"]
        for a in a_indices:
            cmd += ["-map", f"0:a:{a}", "-ac", "1", "-ar", str(SR), "-f", "s16le", os.path.join(td, f"{a}.raw")]
        try:
            run(cmd, timeout=max(120, dur * 5), binary=True)
        except subprocess.TimeoutExpired:
            pass
        for a in a_indices:
            fp = os.path.join(td, f"{a}.raw")
            data = open(fp, "rb").read() if os.path.exists(fp) else b""
            out[a] = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    return out


def _hexdata(dump: str) -> bytes:
    hexchars = "".join(line[10:49].replace(" ", "") for line in (dump or "").splitlines() if len(line) > 10)
    try:
        return bytes.fromhex(hexchars)
    except ValueError:
        return b""


def sub_packets(path: str, windows: list[tuple[float, float]], pad: float = 12.0,
                timeout: float = 600) -> dict[int, list[dict]]:
    """Subtitle packets (all subtitle streams) around the given windows only - no full-file read.

    Each window is read with `pad` seconds either side (the sync check correlates at offsets up to
    +/-12 s). Returns {stream_index: [{start, dur, size, data}]} with absolute timestamps.
    """
    if not windows:
        return {}
    # Absolute end times: with "start%+duration" ffprobe counts the duration from the keyframe it
    # actually seeked to (before `start`), which silently drops the cues at the end of each window.
    intervals = ",".join(f"{max(0.0, s - pad):.3f}%{s + d + pad:.3f}" for s, d in windows)
    cmd = [FFPROBE, "-v", "error", "-read_intervals", intervals, "-select_streams", "s",
           "-show_packets", "-show_data", "-of", "json", path]
    try:
        p = run(cmd, timeout=timeout)
        pk = json.loads(p.stdout or "{}").get("packets", [])
    except (subprocess.TimeoutExpired, ValueError):
        return {}
    out: dict[int, list[dict]] = {}
    seen = set()
    for x in pk:
        st = _f(x.get("pts_time"), -1)
        key = (x.get("stream_index"), st)
        if st < 0 or key in seen:
            continue
        seen.add(key)
        out.setdefault(x.get("stream_index"), []).append(
            {"start": st, "dur": _f(x.get("duration_time")), "size": int(_f(x.get("size"))), "data": _hexdata(x.get("data"))})
    for v in out.values():
        v.sort(key=lambda r: r["start"])
    return out


def text_from_packet(codec: str, data: bytes) -> tuple[str, str]:
    """(style, text) from a subtitle packet. ASS packets are 'ReadOrder,Layer,Style,Name,...,Text'."""
    if codec == "mov_text":
        data = data[2:]
    raw = data.decode("utf-8", "replace").strip("\x00").strip()
    if codec in ("ass", "ssa"):
        parts = raw.split(",", 8)
        if len(parts) == 9:
            return parts[2], parts[8]
    return "", raw


def image_sub_times(path: str, s_index: int | None = None, timeout: float = 1800) -> list[tuple[float, float]]:
    """Display intervals of image subtitles (PGS/VobSub) from packet timing, no OCR."""
    cmd = [FFPROBE, "-v", "error", "-show_entries", "packet=pts_time,duration_time,size", "-of", "csv=p=0"]
    if s_index is not None:
        cmd += ["-select_streams", f"s:{s_index}"]
    p = run(cmd + [path], timeout=timeout)
    pkts = []
    for line in (p.stdout or "").splitlines():
        parts = line.split(",")
        if len(parts) < 3:
            continue
        pts, durv, size = _f(parts[0], -1), _f(parts[1]), int(_f(parts[2]))
        if pts >= 0:
            pkts.append((pts, durv, size))
    pkts.sort()
    out = []
    for i, (pts, durv, size) in enumerate(pkts):
        if size < 200:  # PGS "clear" display sets are tiny
            continue
        end = pts + durv if durv > 0 else (pkts[i + 1][0] if i + 1 < len(pkts) else pts + 3)
        out.append((pts, min(end, pts + 10)))
    return out


def drop_cache(path: str) -> bool:
    """Tell the kernel we're done with a file so its pages leave the page cache (and the container's memory)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    except (OSError, AttributeError):
        return False
    finally:
        os.close(fd)


def extract_ocr_frame(path: str, t: float, width: int = 1280, bottom_third: bool = False) -> bytes:
    """Greyscale PNG frame for OCR (optionally just the bottom third, where subtitles sit)."""
    vf = f"scale={int(width)}:-2,format=gray"
    if bottom_third:
        vf = "crop=iw:ih/3:0:ih*2/3," + vf
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-threads", "2", "-ss", f"{max(0.0, t):.3f}", "-i", path,
           "-frames:v", "1", "-vf", vf, "-f", "image2", "-c:v", "png", "pipe:1"]
    p = run(cmd, timeout=120, binary=True)
    if not p.stdout:
        raise RuntimeError("frame extraction failed")
    return p.stdout


def extract_frame(path: str, t: float, width: int = 768, threads: int = 2) -> bytes:
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-threads", str(threads), "-ss", f"{max(0.0, t):.3f}", "-i", path,
           "-frames:v", "1",
           "-vf", f"scale={int(width)}:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    p = run(cmd, timeout=120, binary=True)
    if not p.stdout:
        raise RuntimeError("frame extraction failed")
    return p.stdout
