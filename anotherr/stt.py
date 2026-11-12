"""faster-whisper wrapper: GPU when available, in a child process that stops when idle so VRAM is fully freed."""
from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time

import numpy as np

from . import settings

log = logging.getLogger("anotherr.stt")
SR = 16000
MODEL_DIR = os.environ.get("ANOTHERR_MODELS", os.path.join(os.environ.get("ANOTHERR_CONFIG", "/config"), "models"))


def _load_model(cfg: dict):
    """Runs in the child process. Returns (model, "device/compute", model name) or raises with every attempt's error."""
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
            log.info("Loaded whisper %s on %s (%s) in %.1fs", model, device, ct, time.time() - t0)
            return m, f"{device}/{ct}", model
        except Exception as e:  # noqa: BLE001
            errors.append(f"{device}: {e}")
            log.warning("Whisper load failed on %s: %s", device, e)
    raise RuntimeError("; ".join(errors))


def _serve(conn, cfg: dict, loader=_load_model):
    """Child process main loop. The model, and the CUDA context CTranslate2 creates (which a live process never gives
    back), exist only here, so ending this process frees all of Whisper's VRAM."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.environ.get("ANOTHERR_LOG_LEVEL", "INFO"),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        m, device, name = loader(cfg)
    except Exception as e:  # noqa: BLE001
        conn.send(("err", str(e)))
        return
    conn.send(("ok", (device, name)))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return  # parent went away
        op = msg[0]
        if op == "quit":
            return
        try:
            if op == "detect":
                _, info = m.transcribe(msg[1][: SR * 30], language=None, beam_size=1, condition_on_previous_text=False)
                probs = list(getattr(info, "all_language_probs", None) or [])[:5]
                conn.send(("ok", (info.language, float(info.language_probability), [(l, float(p)) for l, p in probs])))
            elif op == "transcribe":
                segs, _ = m.transcribe(msg[1], language=msg[2] or None, beam_size=2, vad_filter=True,
                                       condition_on_previous_text=False)
                conn.send(("ok", [(s.start, s.end, s.text.strip()) for s in segs]))
            else:
                conn.send(("err", f"unknown op {op}"))
        except Exception as e:  # noqa: BLE001
            conn.send(("err", f"{type(e).__name__}: {e}"))


class Whisper:
    """faster-whisper in a child process: started on first use, stopped when idle (or on unload()) so GPU memory
    drops to zero instead of keeping CUDA's per-process context for the life of Anotherr."""

    def __init__(self):
        self._proc = None
        self._conn = None
        self._key = None
        self._lock = threading.RLock()
        self.device = None
        self.model_name = None
        self.last_used = 0.0
        self.load_error = ""

    @property
    def loaded(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    # ------------------------------------------------------------ lifecycle
    def _start(self):
        cfg = settings.get()["whisper"]
        key = (cfg["model"], cfg["device"], cfg["compute_type"])
        if self.loaded and self._key == key:
            return
        self.unload()
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_serve, args=(child, dict(cfg)), name="anotherr-whisper", daemon=True)
        proc.start()
        child.close()
        try:
            status, val = parent.recv()
        except (EOFError, OSError):
            status, val = "err", f"whisper process exited during load (code {proc.exitcode})"
        if status != "ok":
            proc.join(5)
            if proc.is_alive():
                proc.kill()
            parent.close()
            self.load_error = val
            raise RuntimeError("Could not load whisper: " + val)
        self._proc, self._conn, self._key = proc, parent, key
        self.device, self.model_name = val
        self.load_error = ""

    def _call(self, *msg):
        with self._lock:
            self._start()
            self.last_used = time.time()
            try:
                self._conn.send(msg)
                status, val = self._conn.recv()
            except (EOFError, OSError) as e:
                code = self._proc.exitcode if self._proc else None
                self.unload()
                raise RuntimeError(f"whisper process died (exit code {code})") from e
            self.last_used = time.time()
            if status != "ok":
                raise RuntimeError(val)
            return val

    def unload(self):
        with self._lock:
            proc, conn = self._proc, self._conn
            self._proc = self._conn = self._key = None
            self.device = None
            if proc is None:
                return
            try:
                conn.send(("quit",))
            except OSError:
                pass
            proc.join(10)
            if proc.is_alive():
                proc.kill()
                proc.join(5)
            conn.close()
            log.info("Whisper model unloaded (process stopped, VRAM freed)")

    def maybe_unload_idle(self):
        mins = settings.get()["whisper"].get("idle_unload_minutes", 10)
        if self._proc is not None and mins and time.time() - self.last_used > mins * 60:
            self.unload()

    # ------------------------------------------------------------ inference
    def detect_language(self, audio: np.ndarray) -> tuple[str, float, list[tuple[str, float]]]:
        """Language detection on (up to) the first 30 s of speech audio."""
        return tuple(self._call("detect", np.ascontiguousarray(audio[: SR * 30], dtype=np.float32)))

    def transcribe(self, audio: np.ndarray, language: str | None) -> list[tuple[float, float, str]]:
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        return [tuple(x) for x in self._call("transcribe", audio, language or None)]


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
