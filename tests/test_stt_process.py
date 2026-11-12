"""Whisper runs in a child process so stopping it frees all of its VRAM (CUDA keeps a per-process context otherwise)."""
import threading
import types

import numpy as np
import pytest

from anotherr import settings, stt


class FakeModel:
    def transcribe(self, audio, language=None, **kw):
        if language == "boom":
            raise ValueError("bad audio")
        info = types.SimpleNamespace(language="ja", language_probability=0.9, all_language_probs=[("ja", 0.9), ("en", 0.1)])
        segs = [types.SimpleNamespace(start=0.0, end=1.5, text=f" {audio.size} samples ")]
        return iter(segs), info


def fake_loader(cfg):
    if cfg["model"] == "broken":
        raise RuntimeError("cuda: no device; cpu: no model")
    return FakeModel(), "cpu/int8", cfg["model"]


class ThreadProcess:
    """Stands in for a spawned process: runs _serve in a thread with the fake model loader."""
    started = []

    def __init__(self, target, args, name=None, daemon=None):
        self._t = threading.Thread(target=lambda: target(*args, loader=fake_loader), daemon=True)
        self.exitcode = None
        ThreadProcess.started.append(self)

    def start(self):
        self._t.start()

    def is_alive(self):
        return self._t.is_alive()

    def join(self, timeout=None):
        self._t.join(timeout)
        if not self._t.is_alive():
            self.exitcode = 0

    def kill(self):
        pass


@pytest.fixture
def fake_ctx(monkeypatch):
    import multiprocessing

    real = multiprocessing.get_context("spawn")

    class ChildEnd:  # the parent closes its copy of the child end; in one process that would close the thread's
        def __init__(self, c):
            self.send, self.recv, self.close = c.send, c.recv, (lambda: None)

    def pipe():
        a, b = real.Pipe()
        return a, ChildEnd(b)

    ctx = types.SimpleNamespace(Pipe=pipe, Process=ThreadProcess)
    monkeypatch.setattr(stt.multiprocessing, "get_context", lambda kind: ctx)
    ThreadProcess.started = []
    return ctx


def test_child_loads_once_answers_and_stops_on_unload(fake_ctx):
    w = stt.Whisper()
    assert not w.loaded
    lang, prob, top = w.detect_language(np.zeros(stt.SR * 60, dtype=np.float32))
    assert (lang, prob, top[0]) == ("ja", 0.9, ("ja", 0.9))
    assert w.transcribe(np.zeros(100, dtype=np.float32), "en") == [(0.0, 1.5, "100 samples")]
    assert w.loaded and w.device == "cpu/int8" and len(ThreadProcess.started) == 1   # one child, reused
    with pytest.raises(RuntimeError, match="bad audio"):
        w.transcribe(np.zeros(10, dtype=np.float32), "boom")
    assert w.loaded                                                                    # an error doesn't kill it
    w.unload()
    assert not w.loaded and not ThreadProcess.started[0].is_alive()                    # the child really exited
    w.detect_language(np.zeros(10, dtype=np.float32))
    assert len(ThreadProcess.started) == 2                                             # next use starts a new one
    w.unload()


def test_idle_unload_stops_the_child(fake_ctx, monkeypatch):
    w = stt.Whisper()
    w.detect_language(np.zeros(10, dtype=np.float32))
    w.maybe_unload_idle()
    assert w.loaded                                    # just used
    w.last_used -= settings.get()["whisper"].get("idle_unload_minutes", 10) * 60 + 1
    w.maybe_unload_idle()
    assert not w.loaded


def test_load_failure_is_reported_and_leaves_nothing_running(fake_ctx, monkeypatch):
    cfg = settings.get()
    cfg["whisper"]["model"] = "broken"
    monkeypatch.setattr(settings, "get", lambda: cfg)
    w = stt.Whisper()
    with pytest.raises(RuntimeError, match="no model"):
        w.detect_language(np.zeros(10, dtype=np.float32))
    assert not w.loaded and "no model" in w.load_error


def test_real_spawned_child_reports_load_errors(monkeypatch, tmp_path):
    """A real spawn: the child can't load a model that doesn't exist offline, says so, and exits."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    cfg = settings.get()
    cfg["whisper"].update(model=str(tmp_path / "no-such-model"), device="cpu", compute_type="int8")
    monkeypatch.setattr(settings, "get", lambda: cfg)
    w = stt.Whisper()
    with pytest.raises(RuntimeError, match="Could not load whisper"):
        w.detect_language(np.zeros(10, dtype=np.float32))
    assert not w.loaded and w._proc is None
