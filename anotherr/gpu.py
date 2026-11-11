"""Shared GPU gate: caps how many GPU jobs (NVDEC decodes, Whisper calls) run at once and keeps
anotherr's share of GPU time near `scan.gpu_duty_percent` by resting after each job.

A job that took t seconds holds the next GPU job back for t * (100 / duty - 1) seconds, so at 50%
the GPU works about half the time on anotherr's behalf. Settings are read on every call."""
import contextlib
import threading
import time

from . import settings

_cond = threading.Condition()
_running = 0
_next_start = 0.0


def _limits() -> tuple[int, float]:
    sc = settings.get()["scan"]
    slots = max(1, int(sc.get("gpu_slots", 1) or 1))
    duty = float(sc.get("gpu_duty_percent", 100) or 100)
    return slots, min(100.0, max(5.0, duty))


@contextlib.contextmanager
def use():
    global _running, _next_start
    with _cond:
        while True:
            slots, _ = _limits()
            wait = _next_start - time.time()
            if _running < slots and wait <= 0:
                break
            _cond.wait(timeout=max(0.05, min(wait, 1.0)) if wait > 0 else 1.0)
        _running += 1
    t0 = time.time()
    try:
        yield
    finally:
        took = time.time() - t0
        with _cond:
            _running -= 1
            _, duty = _limits()
            if duty < 100:
                _next_start = max(_next_start, time.time() + took * (100.0 / duty - 1.0))
            _cond.notify_all()
