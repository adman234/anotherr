"""faster-whisper wrapper: GPU when available, lazy load, unload when idle to free VRAM for Plex."""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

import numpy as np

from . import gpu, settings

log = logging.getLogger("anotherr.stt")
SR = 16000
MODEL_DIR = os.environ.get("ANOTHERR_MODELS", os.path.join(os.environ.get("ANOTHERR_CONFIG", "/config"), "models"))


class Whisper:
    def __init__(self):
        self._model = None
        self._key = None
        self._lock = threading.RLock()
        self.device = None
        self.model_name = None
        self.last_used = 0.0
        self.load_error = ""

    # ------------------------------------------------------------ lifecycle
    def _load(self):
        cfg = settings.get()["whisper"]
        key = (cfg["model"], cfg["device"], cfg["compute_type"])
        if self._model is not None and self._key == key:
            return self._model
        self.unload()
        from faster_whisper import WhisperModel

        attempts = []
        if cfg["device"] in ("auto", "cuda"):
            ct = "float16" if cfg["compute_type"] == "auto" else cfg["compute_type"]
            attempts.append((cfg["model"], "cuda", ct))
        if cfg["device"] in ("auto", "cpu"):
            ct = "int8" if cfg["compute_type"] in ("auto", "float16", "int8_float16") else cfg["compute_type"]
            model = cfg["model"] if cfg["device"] == "cpu" else (cfg.get("cpu_fallback_model") or cfg["model"])
            attempts.append((model, "cpu", ct))
        errors = []
        for model, device, ct in attempts:
            try:
                t0 = time.time()
                m = WhisperModel(model, device=device, compute_type=ct, download_root=MODEL_DIR,
                                 cpu_threads=max(2, (os.cpu_count() or 4) // 2))
                if device == "cuda":  # make sure CUDA kernels actually run on this GPU
                    list(m.transcribe(np.zeros(SR, dtype=np.float32), language="en", beam_size=1)[0])
                self._model, self._key, self.device, self.model_name = m, key, f"{device}/{ct}", model
                self.load_error = ""
                log.info("Loaded whisper %s on %s (%s) in %.1fs", model, device, ct, time.time() - t0)
                return m
            except Exception as e:  # noqa: BLE001
                errors.append(f"{device}: {e}")
                log.warning("Whisper load failed on %s: %s", device, e)
        self.load_error = "; ".join(errors)
        raise RuntimeError("Could not load whisper: " + self.load_error)

    def unload(self):
        with self._lock:
            if self._model is not None:
                self._model = None
                self._key = None
                self.device = None
                import gc

                gc.collect()
                log.info("Whisper model unloaded")

    def maybe_unload_idle(self):
        mins = settings.get()["whisper"].get("idle_unload_minutes", 10)
        if self._model is not None and mins and time.time() - self.last_used > mins * 60:
            self.unload()

    def _gpu(self):
        return gpu.use() if (self.device or "").startswith("cuda") else contextlib.nullcontext()

    # ------------------------------------------------------------ inference
    def detect_language(self, audio: np.ndarray) -> tuple[str, float, list[tuple[str, float]]]:
        """Language detection on (up to) the first 30 s of speech audio."""
        with self._lock:
            m = self._load()
            self.last_used = time.time()
            clip = audio[: SR * 30]
            with self._gpu():
                _, info = m.transcribe(clip, language=None, beam_size=1, condition_on_previous_text=False)
            probs = list(getattr(info, "all_language_probs", None) or [])[:5]
            return info.language, float(info.language_probability), [(l, float(p)) for l, p in probs]

    def transcribe(self, audio: np.ndarray, language: str | None) -> list[tuple[float, float, str]]:
        with self._lock:
            m = self._load()
            self.last_used = time.time()
            with self._gpu():  # segments are a generator: the GPU work happens while it is consumed
                segs, _ = m.transcribe(audio, language=language or None, beam_size=2, vad_filter=True,
                                       condition_on_previous_text=False)
                out = [(s.start, s.end, s.text.strip()) for s in segs]
            self.last_used = time.time()
            return out


def speech_intervals(audio: np.ndarray) -> list[tuple[float, float]]:
    """Silero VAD (bundled with faster-whisper, runs on CPU)."""
    if audio.size < SR // 2:
        return []
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(threshold=0.5, min_speech_duration_ms=200, min_silence_duration_ms=300, speech_pad_ms=100)
    ts = get_speech_timestamps(audio, opts, sampling_rate=SR)
    return [(t["start"] / SR, t["end"] / SR) for t in ts]


def speech_audio(audio: np.ndarray, intervals: list[tuple[float, float]], max_s: float = 30.0) -> np.ndarray:
    parts, total = [], 0.0
    for s, e in intervals:
        if total >= max_s:
            break
        parts.append(audio[int(s * SR): int(e * SR)])
        total += e - s
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


WHISPER = Whisper()
