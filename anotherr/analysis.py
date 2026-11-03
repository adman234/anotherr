"""Pure analysis functions (no I/O) - easy to unit test."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import numpy as np

RES = 0.1  # seconds per bin for sync correlation
MAX_OFFSET = 12.0

_SIGN_STYLE = re.compile(r"sign|song|op\b|ed\b|opening|ending|title|karaoke|kfx|lyric|insert|note|typeset|ts\b", re.I)
_TAGS = re.compile(r"\{[^}]*\}")
_POS = re.compile(r"\\(pos|move|org|clip|k|kf|ko|K)\b|\\p[1-9]")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def dialogue_cues(subs_text: str) -> tuple[list[Cue], int]:
    """Parse ASS/SRT text, return (dialogue-like cues, total event count). Signs/typesetting are dropped."""
    import pysubs2

    try:
        sf = pysubs2.SSAFile.from_string(subs_text)
    except Exception:
        return [], 0
    cues, total = [], 0
    for ev in sf.events:
        if ev.is_comment:
            continue
        total += 1
        c = clean_cue(ev.start / 1000.0, ev.end / 1000.0, ev.style or "", ev.text or "")
        if c:
            cues.append(c)
    return cues, total


def clean_cue(start: float, end: float, style: str, text: str) -> Cue | None:
    """A dialogue cue, or None for signs/typesetting/karaoke and junk."""
    if end - start < 0.2 or end - start > 15:
        return None
    if _SIGN_STYLE.search(style or "") or _POS.search(text or ""):
        return None
    text = _TAGS.sub("", text or "").replace("\\N", " ").replace("\\n", " ").replace("<i>", "").replace("</i>", "").strip()
    return Cue(start, end, text) if text else None


def classify_track(cue_count: int, duration_s: float, title: str, forced: bool, min_per_min: float) -> str:
    """full | signs | empty"""
    t = (title or "").lower()
    if forced or any(w in t for w in ("sign", "song", "forced", "s&s")):
        return "signs"
    if cue_count == 0:
        return "empty"
    per_min = cue_count / max(duration_s / 60.0, 1.0)
    return "full" if per_min >= min_per_min else "signs"


def _bins(intervals, t0: float, n: int) -> np.ndarray:
    arr = np.zeros(n, dtype=np.float32)
    for s, e in intervals:
        a, b = int(round((s - t0) / RES)), int(round((e - t0) / RES))
        a, b = max(a, 0), min(b, n)
        if b > a:
            arr[a:b] = 1.0
    return arr


def sync_offset(speech: list[tuple[float, float]], cues: list[tuple[float, float]], win_start: float,
                win_dur: float) -> dict | None:
    """Best subtitle offset for one window. Positive offset = subtitles appear late.

    speech/cues are absolute times. Returns None when there is not enough signal.
    """
    n = int(win_dur / RES)
    m = int(MAX_OFFSET / RES)
    s_arr = _bins(speech, win_start, n)
    c_arr = _bins(cues, win_start - MAX_OFFSET, n + 2 * m)
    speech_s, cue_s = s_arr.sum() * RES, c_arr[m:m + n].sum() * RES
    cues_in = sum(1 for s, e in cues if e > win_start and s < win_start + win_dur)
    if speech_s < 5 or cues_in < 3:
        return None
    corr = np.correlate(c_arr, s_arr, mode="valid")  # len 2m+1
    best = float(corr.max())
    # ties (e.g. very regular dialogue) resolve to the smallest absolute offset
    near = np.flatnonzero(corr >= best * 0.995)
    k = int(near[np.argmin(np.abs(near - m))])
    best = float(corr[k])
    aligned = c_arr[k:k + n]
    union = s_arr.sum() + aligned.sum() - best
    jacc = best / union if union else 0.0
    z = float(corr[m])
    union0 = s_arr.sum() + c_arr[m:m + n].sum() - z
    jacc0 = z / union0 if union0 else 0.0
    med = float(np.median(corr)) or 1e-6
    return {
        "offset": round(k * RES - MAX_OFFSET, 2),
        "score": round(jacc, 3),
        "score_at_zero": round(jacc0, 3),
        "peak_ratio": round(best / med, 2),
        "speech_s": round(float(speech_s), 1),
        "cue_s": round(float(cue_s), 1),
        "cues": cues_in,
    }


def aggregate_sync(results: list[dict]) -> dict:
    """Combine per-window sync results into a verdict: ok | offset | mismatch | unknown."""
    res = [r for r in results if r]
    if not res:
        return {"verdict": "unknown", "windows": 0}
    offsets = [r["offset"] for r in res]
    scores = [r["score"] for r in res]
    med_off = statistics.median(offsets)
    spread = statistics.median([abs(o - med_off) for o in offsets]) if len(offsets) > 1 else 0.0
    med_score = statistics.median(scores)
    med_zero = statistics.median([r["score_at_zero"] for r in res])
    out = {"windows": len(res), "offset": round(med_off, 2), "spread": round(spread, 2),
           "score": round(med_score, 3), "score_at_zero": round(med_zero, 3)}
    if med_score < 0.22 and med_zero < 0.22:
        out["verdict"] = "mismatch"
    elif abs(med_off) >= 0.6 and spread <= 1.0 and med_score - med_zero >= 0.05:
        out["verdict"] = "offset"
    else:
        out["verdict"] = "ok"
    return out


_WORD = re.compile(r"[a-z0-9']+")
_STOP = set("""a an the and or but if of to in on at for with is are was were be been am i you he she it we they me
him her us them my your his its our their this that these those what who whom which there here do does did not no
yes oh so just get got go going gonna wanna let lets im youre its dont cant wont s t ll re ve d m""".split())


def content_words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower().replace("’", "'")) if w not in _STOP and len(w) > 2]


def text_overlap(transcript: str, subs: str) -> dict:
    """Word-overlap between a transcript and subtitle text (recall/precision of content words)."""
    tw, sw = content_words(transcript), content_words(subs)
    if len(tw) < 8 or len(sw) < 8:
        return {"recall": None, "precision": None, "words": len(tw)}
    ts, ss = set(tw), set(sw)
    inter = ts & ss
    return {"recall": round(len(inter) / len(ts), 3), "precision": round(len(inter) / len(ss), 3), "words": len(tw)}


def track_verdict(sync_verdict: str, text_recall: float | None) -> str:
    """Final verdict for one subtitle track: ok | offset | mismatch | unknown.

    Text compared after shifting by the detected offset is the stronger evidence: if it matches,
    the subs are the right content (a timing "mismatch" is overruled); if it still doesn't match,
    the track is a mismatch regardless of timing (one issue, not offset + text mismatch).
    """
    if text_recall is None:
        return sync_verdict
    if text_recall >= 0.5:
        return "offset" if sync_verdict == "offset" else "ok"
    if text_recall < 0.2:
        return "mismatch"
    return sync_verdict


def aggregate_language(windows: list[tuple[str, float]]) -> dict:
    """Combine per-window language detections: majority language, agreement and mean confidence."""
    if not windows:
        return {"detected": "", "prob": 0.0, "agree": 0, "n": 0, "all_agree": False}
    counts: dict[str, list[float]] = {}
    for lang, p in windows:
        counts.setdefault(lang, []).append(p)
    lang, probs = max(counts.items(), key=lambda kv: (len(kv[1]), sum(kv[1])))
    return {"detected": lang, "prob": round(sum(probs) / len(probs), 3), "agree": len(probs), "n": len(windows),
            "all_agree": len(probs) == len(windows)}


def rms_db(audio: np.ndarray) -> float:
    if audio.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(audio))))
    return 20 * np.log10(max(r, 1e-6))


def nearest_break(target: float, black: list[tuple[float, float]], silence: list[tuple[float, float]]) -> float | None:
    """Pick the black interval nearest target, preferring ones that overlap silence."""
    best, best_score = None, None
    for s, e in black:
        mid = (s + e) / 2
        overlaps = any(ss < e and se > s for ss, se in silence)
        score = abs(mid - target) - (60 if overlaps else 0)
        if best_score is None or score < best_score:
            best, best_score = mid, score
    return best
