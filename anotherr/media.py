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
                        commentary=bool(disp.get("comment")) or "comment" in base["title"].lower())
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


def video_checks(path: str, start: float, dur: float) -> dict:
    """Black / frozen video on keyframes only, downscaled - a fraction of a second per segment."""
    cmd = nice_prefix() + [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "level+info", "-skip_frame", "nokey",
                           "-ss", f"{start:.3f}", "-i", path, "-t", f"{dur:.3f}", "-map", "0:v:0",
                           "-vf", "scale=320:-2,blackdetect=d=1.5:pix_th=0.10,freezedetect=n=-60dB:d=8",
                           "-an", "-sn", "-f", "null", "-"]
    try:
        p = run(cmd, timeout=300)
    except subprocess.TimeoutExpired:
        return {"black": [], "freeze": []}
    err = p.stderr or ""
    return {"black": [(start + float(a), start + float(b), float(c)) for a, b, c in _BLACK.findall(err)],
            "freeze": [float(v) for v in _FREEZE.findall(err)]}


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


def sub_packets(path: str, windows: list[tuple[float, float]], timeout: float = 600) -> dict[int, list[dict]]:
    """Subtitle packets (all subtitle streams) inside the given windows only - no full-file read.

    Returns {stream_index: [{start, dur, size, data}]} with absolute timestamps.
    """
    if not windows:
        return {}
    intervals = ",".join(f"{max(0.0, s):.3f}%+{d:.3f}" for s, d in windows)
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


def extract_frame(path: str, t: float, width: int = 768) -> bytes:
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={int(width)}:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    p = run(cmd, timeout=120, binary=True)
    if not p.stdout:
        raise RuntimeError("frame extraction failed")
    return p.stdout
