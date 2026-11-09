"""subgen upload mode: Anotherr extracts the audio track, POSTs it to /asr and writes <stem>.en.srt itself.

subgen gets no path and needs no media access. httpx and ffmpeg are faked; no real subgen is needed."""
import json
import os
import shutil
import subprocess
import threading

import httpx
import pytest
from test_subgen import (  # noqa: F401
    ENGLISH,
    T,
    _approved,
    drain,
    episode,
    fake,
    files_of,
    lib,
    plan,
    show,
)

from anotherr import db, settings, subgen


def srt_text(lines=ENGLISH, n=240):
    out = []
    for k in range(n):
        s, e = k * 5, k * 5 + 3
        out.append(f"{k + 1}\n00:{s // 60:02}:{s % 60:02},000 --> 00:{e // 60:02}:{e % 60:02},000\n{lines[k % len(lines)]}\n")
    return "\n".join(out) + "\n"


class Resp:
    def __init__(self, code=200, text=None, ctype="text/plain; charset=utf-8"):
        self.status_code, self.text = code, srt_text() if text is None else text
        self.headers = {"content-type": ctype}

    def json(self):
        return json.loads(self.text)


class FakeAsr:
    """subgen over HTTP: GET /queue is always idle; POST /asr answers from `replies` (a Resp or an exception)."""

    def __init__(self):
        self.calls, self.replies, self.other = [], [], []
        self.during = None  # runs while the "transcription" is in progress
        self.ffmpeg, self.ffmpeg_fail, self.scratch = [], False, None

    def request(self, method, url, params=None, timeout=None):
        self.other.append((method, url, params))
        return Resp(200, json.dumps({"queued": [], "processing": [], "idle": True, "version": "v4.39",
                                     "model": "large-v3"}), "application/json")

    def post(self, url, params=None, files=None, timeout=None):
        name, fh, mime = files["audio_file"]
        self.calls.append({"url": url, "params": dict(params), "fields": sorted(files), "name": name, "mime": mime,
                           "streamed": hasattr(fh, "read") and not isinstance(fh, (bytes, str)),
                           "audio": fh.name, "size": len(fh.read()), "timeout": timeout})
        if self.during:
            self.during(self.calls[-1])
        r = self.replies.pop(0) if self.replies else Resp()
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def asr(lib, monkeypatch):  # noqa: F811
    scratch = lib / "scratch"
    settings.save({**settings.get(), "subgen": {**settings.get()["subgen"], "mode": "upload", "temp_dir": str(scratch)}})
    f = FakeAsr()
    f.scratch = scratch
    monkeypatch.setattr(subgen.httpx, "post", f.post)
    monkeypatch.setattr(subgen.httpx, "request", f.request)

    def run(cmd, timeout, binary=False):  # stands in for ffmpeg: writes some "audio" to the output path
        f.ffmpeg.append(cmd)
        if f.ffmpeg_fail:
            return subprocess.CompletedProcess(cmd, 1, "", "Stream map '0:a:5' matches no streams.")
        with open(cmd[-1], "wb") as fh:
            fh.write(b"fLaC" + b"\0" * 40000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subgen.media, "run", run)
    yield f
    if subgen._asr_thread is not None:
        subgen._asr_thread.join(5)


def _leftover_audio(f):
    return [n for n in os.listdir(f.scratch) if not n.endswith(".srt")] if os.path.isdir(f.scratch) else []


def _upload_batch(lib, n=1, tracks=None):  # noqa: F811
    iid = show()
    fids = [episode(lib, iid, f"S05E{k:02}", tracks or [T(0, "kor", default=True)], season=5) for k in range(1, n + 1)]
    subgen.request(fids)
    aid = db.scalar("SELECT id FROM actions WHERE kind='subgen_transcribe' AND status='proposed' ORDER BY id DESC")
    assert aid, [plan(x) for x in fids]
    assert subgen.approve(aid)["ok"]
    return aid, fids


def _stem(fid):
    return os.path.splitext(db.scalar("SELECT local_path FROM files WHERE id=?", (fid,)))[0]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _mode(m):
    settings.save({**settings.get(), "subgen": {**settings.get()["subgen"], "mode": m}})


# ---------------------------------------------------------------- the /asr request itself

def test_asr_request_streams_the_file_in_the_audio_file_field(tmp_path, monkeypatch):
    f = FakeAsr()
    monkeypatch.setattr(subgen.httpx, "post", f.post)
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"fLaC" + b"\0" * 5000)
    text = subgen.Client("http://sg:9000/").asr(str(audio), "ko", "translate", fmt="flac", timeout=1234)
    c = f.calls[0]
    assert "-->" in text and c["url"] == "http://sg:9000/asr"
    assert c["params"] == {"task": "translate", "language": "ko", "output": "srt", "encode": "true"}
    assert c["fields"] == ["audio_file"] and c["mime"] == "audio/flac" and c["name"].endswith(".flac")
    assert c["streamed"] and c["size"] == 5004  # an open file handle, not bytes held in memory
    assert c["timeout"].read == 1234 and c["timeout"].connect == subgen.CONNECT_TIMEOUT_S
    subgen.Client("http://sg:9000").asr(str(audio), "", "transcribe", fmt="pcm")
    assert f.calls[1]["params"] == {"task": "transcribe", "output": "srt", "encode": "false"}


@pytest.mark.parametrize("reply, retry, words", [
    (Resp(500, "Internal Server Error"), True, "-> 500"),
    (Resp(422, '{"detail": "audio_file missing"}', "application/json"), False, "-> 422"),
    (Resp(200, ""), False, "empty"),
    (Resp(200, '{"status": "error", "message": "Audio file is empty"}', "application/json"), False,
     "Audio file is empty"),
    (Resp(200, '{"status": "timeout", "message": "ASR processing timed out after 18000 seconds"}',
          "application/json"), False, "timed out"),
    (Resp(200, "<html>proxy error</html>"), False, "isn't an SRT"),
    (httpx.RemoteProtocolError("Server disconnected without sending a response."), True, "connection dropped"),
    (httpx.ConnectError("connection refused"), True, "connection dropped"),
    (httpx.ConnectTimeout("timed out"), True, "unreachable"),
    (httpx.ReadTimeout("timed out"), False, "no answer"),
])
def test_asr_failures_are_classified(tmp_path, monkeypatch, reply, retry, words):
    f = FakeAsr()
    f.replies.append(reply)
    monkeypatch.setattr(subgen.httpx, "post", f.post)
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x" * 2000)
    with pytest.raises(subgen.AsrError) as ex:
        subgen.Client("http://sg").asr(str(audio), "ko", "translate")
    assert words in str(ex.value) and ex.value.retry is retry


# ---------------------------------------------------------------- the worker in upload mode

def test_upload_mode_end_to_end_writes_en_srt_without_sending_a_path(lib, asr):  # noqa: F811
    # dual audio, Korean is track 2 tagged 'und': path mode refuses it, upload mode extracts exactly that track
    _mode("path")
    assert "subgen would transcribe audio track 1" in plan(
        episode(lib, show("Path check"), "S01E01", [T(0, "eng", default=True), T(1, "kor", tag="und")]))[1]
    _mode("upload")
    aid, fids = _upload_batch(lib, 2, tracks=[T(0, "eng", default=True), T(1, "kor", tag="und")])
    p0 = files_of(aid)[0]
    assert p0["path"] == "" and p0["upload"] and p0["a_index"] == 1
    drain()
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "done" and "2 of 2 done" in a["result"], a["result"]
    assert len(asr.calls) == 2
    assert all(c["params"] == {"task": "translate", "language": "ko", "output": "srt", "encode": "true"}
               for c in asr.calls)
    assert asr.other and all(m == "GET" and u.endswith("/queue") for m, u, _ in asr.other)  # no /batch, no path
    for fid, cmd in zip(fids, asr.ffmpeg):
        local = db.scalar("SELECT local_path FROM files WHERE id=?", (fid,))
        assert cmd[cmd.index("-i") + 1] == local and cmd[cmd.index("-map") + 1] == "0:a:1"
        assert cmd[cmd.index("-ac") + 1] == "1" and cmd[cmd.index("-ar") + 1] == "16000"
        assert os.path.dirname(cmd[-1]) == str(asr.scratch)  # temp audio is not next to the media
        assert _read(_stem(fid) + ".en.srt") == srt_text()
    assert all(c["audio"].startswith(str(asr.scratch)) for c in asr.calls)
    e = files_of(aid)[0]
    assert e["machine_generated"] and e["upload_mb"] == 0.0 and e["check"]["english_score"] > 0.3
    assert os.listdir(asr.scratch) == []  # audio and the scratch .srt are gone
    assert db.scalar("SELECT COUNT(*) FROM queue WHERE source='subgen'") == 2


def test_upload_http_error_is_retried_once_then_fails(lib, asr):  # noqa: F811
    asr.replies += [Resp(502, "Bad Gateway"), Resp(502, "Bad Gateway")]
    aid, fids = _upload_batch(lib, 1)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and "-> 502" in e["outcome"] and "already retried once" in e["outcome"]
    assert len(asr.calls) == 2 and e["retried"]
    assert not os.path.exists(_stem(fids[0]) + ".en.srt") and _leftover_audio(asr) == []


def test_upload_dropped_connection_is_retried_once(lib, asr):  # noqa: F811
    asr.replies.append(httpx.RemoteProtocolError("Server disconnected without sending a response."))
    aid, fids = _upload_batch(lib, 1)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "done" and e["retried"] and len(asr.calls) == 2
    assert os.path.exists(_stem(fids[0]) + ".en.srt") and _leftover_audio(asr) == []


@pytest.mark.parametrize("reply, words", [
    (Resp(200, ""), "empty"),
    (Resp(200, '{"status": "error", "message": "ASR processing failed: CUDA out of memory"}', "application/json"),
     "CUDA out of memory"),
    (httpx.ReadTimeout("timed out"), "no answer"),
])
def test_upload_bad_answer_or_timeout_fails_without_retry(lib, asr, reply, words):  # noqa: F811
    asr.replies.append(reply)
    aid, fids = _upload_batch(lib, 1)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and words in e["outcome"] and len(asr.calls) == 1
    assert not os.path.exists(_stem(fids[0]) + ".en.srt") and _leftover_audio(asr) == []


def test_upload_failures_count_towards_the_batch_stop(lib, asr):  # noqa: F811
    asr.replies += [Resp(400, "bad request")] * 3
    aid, _ = _upload_batch(lib, 4)
    drain()
    assert [e["status"] for e in files_of(aid)] == ["failed"] * 3 + ["not attempted"]
    assert "3 failures in a row" in db.scalar("SELECT result FROM actions WHERE id=?", (aid,))
    assert _leftover_audio(asr) == []


def test_upload_budget_is_the_read_timeout(lib, asr):  # noqa: F811
    settings.save({**settings.get(), "subgen": {**settings.get()["subgen"], "timeout_factor": 2, "timeout_min_s": 60}})
    _upload_batch(lib, 1)
    drain()
    assert asr.calls[0]["timeout"].read == 2400 and asr.calls[0]["timeout"].connect == subgen.CONNECT_TIMEOUT_S


def test_wrong_language_result_is_rejected_and_not_written_to_the_library(lib, asr):  # noqa: F811
    asr.replies.append(Resp(200, srt_text(["안녕하세요 여러분 반갑습니다"])))
    aid, fids = _upload_batch(lib, 1)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and "isn't English" in e["outcome"]
    assert [n for n in os.listdir(os.path.dirname(_stem(fids[0]))) if n.endswith(".srt")] == []
    assert _leftover_audio(asr) == []


def test_existing_en_srt_is_never_overwritten(lib, asr):  # noqa: F811
    aid, fids = _upload_batch(lib, 1)
    dst = _stem(fids[0]) + ".en.srt"

    def someone_writes_one(call):
        with open(dst, "w") as fh:
            fh.write("mine")

    asr.during = someone_writes_one
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and "already exists" in e["outcome"]
    assert _read(dst) == "mine" and _leftover_audio(asr) == []
    assert [n for n in os.listdir(os.path.dirname(dst)) if n.endswith(".tmp")] == []


def test_ffmpeg_failure_fails_the_file_and_leaves_no_temp_audio(lib, asr):  # noqa: F811
    asr.ffmpeg_fail = True
    aid, _ = _upload_batch(lib, 1)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and "ffmpeg couldn't extract audio track 1" in e["outcome"]
    assert asr.calls == [] and _leftover_audio(asr) == []


def test_cancel_during_an_upload_drops_the_request_and_removes_the_audio(lib, asr):  # noqa: F811
    aid, _ = _upload_batch(lib, 2)
    release = threading.Event()

    def cancel_meanwhile(call):
        subgen.control(aid, "cancel")
        release.wait(5)

    asr.during = cancel_meanwhile
    t = threading.Thread(target=drain)
    t.start()
    t.join(10)
    assert not t.is_alive()  # the worker didn't wait for subgen's answer
    fs = files_of(aid)
    assert fs[0]["status"] == "failed" and "cancelled" in fs[0]["outcome"]
    assert fs[1]["status"] == "not attempted" and len(asr.calls) == 1
    assert db.scalar("SELECT status FROM actions WHERE id=?", (aid,)) == "failed"
    release.set()
    subgen._asr_thread.join(5)
    assert _leftover_audio(asr) == []


def test_path_mode_is_the_default_and_unchanged(lib, fake):  # noqa: F811
    assert settings.DEFAULTS["subgen"]["mode"] == "path" and not subgen.upload_mode()
    aid, _ = _approved(lib, 1, fake=fake)
    e = files_of(aid)[0]
    assert e["path"] and "upload" not in e
    drain()
    assert len(fake.submits) == 1 and files_of(aid)[0]["status"] == "done"


# ---------------------------------------------------------------- real ffmpeg, if there is one

@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_extract_audio_track_with_real_ffmpeg(tmp_path):
    src = tmp_path / "test.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-f", "lavfi",
                    "-i", "sine=frequency=880:duration=5", "-map", "0", "-map", "1", "-ac", "2", "-ar", "48000",
                    str(src)], check=True)
    for fmt in ("flac", "opus", "pcm"):
        out = tmp_path / ("out" + subgen.AUDIO_FORMATS[fmt][1])
        size = subgen.extract_audio_track(str(src), 1, str(out), fmt)
        assert size > 1024 and out.stat().st_size == size
        if fmt == "pcm":
            assert abs(size - 5 * 16000 * 2) < 3200  # 5 s of 16 kHz mono s16le
            continue
        st = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
                                       capture_output=True, text=True, check=True).stdout)["streams"]
        assert len(st) == 1 and st[0]["channels"] == 1
        if fmt == "flac":
            assert st[0]["sample_rate"] == "16000"
    with pytest.raises(subgen.SubgenError, match="couldn't extract audio track 3"):
        subgen.extract_audio_track(str(src), 2, str(tmp_path / "none.flac"))


def test_upload_mode_skips_before_extracting_when_the_en_srt_is_on_another_disk(lib, asr):  # noqa: F811
    from test_subgen import split_across_disks

    aid, fids = _upload_batch(lib, 2)
    srt = split_across_disks(lib, fids[0])
    drain()
    fs = files_of(aid)
    assert fs[0]["status"] == "done" and "already exists" in fs[0]["outcome"] and srt in fs[0]["outcome"]
    assert fs[1]["status"] == "done" and len(asr.ffmpeg) == 1 and len(asr.calls) == 1  # no audio extracted for #1
    local0 = db.scalar("SELECT local_path FROM files WHERE id=?", (fids[0],))
    assert all(cmd[cmd.index("-i") + 1] != local0 for cmd in asr.ffmpeg)
    assert not os.path.exists(_stem(fids[0]) + ".en.srt")  # nothing written next to the physical copy either


# ---------------------------------------------------------------- replacing a signs-only .en.srt

def test_upload_mode_replaces_the_signs_only_sidecar_atomically(lib, asr):  # noqa: F811
    from test_subgen import SIGNS, signs_batch

    aid, fid, srt = signs_batch(lib)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "done" and e["outcome"].startswith("replaced signs-only "), e
    assert _read(srt) == srt_text() and SIGNS not in _read(srt)
    assert [n for n in os.listdir(os.path.dirname(srt)) if n.endswith(".tmp")] == []
    assert len(asr.calls) == 1  # the existing-sub skip did not fire


def test_upload_mode_failed_check_leaves_the_signs_only_sidecar(lib, asr):  # noqa: F811
    from test_subgen import SIGNS, signs_batch

    asr.replies.append(Resp(200, srt_text(["안녕하세요 여러분 반갑습니다"])))
    aid, fid, srt = signs_batch(lib)
    drain()
    assert files_of(aid)[0]["status"] == "failed"
    assert _read(srt) == SIGNS
    assert [n for n in os.listdir(os.path.dirname(srt)) if n.endswith(".tmp")] == []


def test_upload_mode_does_not_replace_a_full_sidecar(lib, asr):  # noqa: F811
    from test_subgen import signs_episode

    fid, srt = signs_episode(lib, show(), "S06E09", kind="full")
    assert subgen.request([fid])["proposed"] == []
    assert not asr.calls
