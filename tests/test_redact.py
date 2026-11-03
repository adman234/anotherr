import io
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from anotherr import arr, db, redact, settings

KEY = "SECRETKEY1234567890"


@pytest.fixture
def fake_tautulli():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"response": {"result": "error", "message": "Invalid apikey"}}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    settings.save({"tautulli": {"url": f"http://127.0.0.1:{srv.server_port}", "api_key": KEY}})
    yield srv
    srv.shutdown()


@pytest.fixture
def captured_root_log():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    redact.install()
    yield buf
    root.removeHandler(h)
    root.setLevel(old_level)


def test_tautulli_key_not_in_logs_or_errors(fake_tautulli, captured_root_log):
    logging.getLogger("httpx").setLevel(logging.INFO)  # even if something re-enables httpx request logs...
    with pytest.raises(arr.ServiceError) as e:
        arr.Tautulli().stream_count()
    assert KEY not in str(e.value) and "apikey=***" in str(e.value)
    res = arr.test("tautulli")
    assert not res["ok"] and KEY not in res["error"]
    out = captured_root_log.getvalue()
    assert KEY not in out
    assert "apikey=***" in out  # ...the request line is still there, but masked


def test_install_quiets_httpx():
    redact.install()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_activity_feed_redacted():
    settings.save({"tautulli": {"url": "http://t", "api_key": KEY}})
    db.log(f"tautulli failed: http://t/api/v2?apikey={KEY}&cmd=x and raw {KEY}")
    msg = db.q1("SELECT message FROM activity ORDER BY id DESC LIMIT 1")["message"]
    assert KEY not in msg


def test_exception_tracebacks_redacted(captured_root_log):
    try:
        raise RuntimeError(f"boom http://x/?apikey={KEY}")
    except RuntimeError:
        logging.getLogger("anotherr.test").exception("failed")
    assert KEY not in captured_root_log.getvalue()


def test_startup_scrubs_old_entries():
    from anotherr import api

    db.x("INSERT INTO activity(ts, level, message) VALUES (?, 'error', ?)", (time.time(), f"GET /api/v2?apikey={KEY}&cmd=a"))
    api._scrub_stored_secrets()
    assert not db.q("SELECT id FROM activity WHERE message LIKE ?", (f"%{KEY}%",))


@pytest.mark.parametrize("raw", [
    f"http://h/api/v2?cmd=a&apikey={KEY}",
    f"X-Api-Key: {KEY}",
    f'{{"api_key": "{KEY}"}}',
    f"Authorization: Bearer {KEY}",
    f"/api/webhook/sonarr?apikey={KEY}",
])
def test_patterns(raw):
    assert KEY not in redact.redact(raw)


def test_leaves_normal_text_alone():
    s = "Scanned House S05E10 [standard] -> ok (0 issues, 31s)"
    assert redact.redact(s) == s
