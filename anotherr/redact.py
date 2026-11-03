"""Keep secrets out of logs, the activity feed and error messages.

Tautulli only accepts its API key as a URL parameter, and httpx puts full URLs in its
request logs and exception messages - so anything that prints a URL can leak a key.
"""
from __future__ import annotations

import logging
import os
import re

MASK = "***"

# key=value in URLs / query strings / form bodies
_PARAM = re.compile(r"(?i)\b(apikey|api_key|api-key|x-api-key|token|access_token|password|passwd|secret)=([^&\s'\"<>]+)")
# header-style "X-Api-Key: value" or JSON-ish "api_key": "value"
_HEADER = re.compile(r"(?i)\b(x-api-key|api[_-]?key|authorization)(['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?([^\s'\",}&]+)")

_MIN_LITERAL = 6  # don't blank out tiny strings that would match everywhere


def _known_secrets() -> list[str]:
    vals = [os.environ.get("ANOTHERR_API_KEY", "")]
    try:
        from . import settings

        cfg = settings._cache or {}  # never trigger a DB read from inside logging
        for svc in ("sonarr", "radarr", "bazarr", "tautulli"):
            vals.append((cfg.get(svc) or {}).get("api_key", ""))
    except Exception:  # noqa: BLE001
        pass
    return sorted({v for v in vals if v and len(v) >= _MIN_LITERAL and v != "********"}, key=len, reverse=True)


def redact(text) -> str:
    if text is None:
        return ""
    s = str(text)
    s = _PARAM.sub(lambda m: f"{m.group(1)}={MASK}", s)
    s = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{MASK}", s)
    for secret in _known_secrets():
        s = s.replace(secret, MASK)
    return s


class RedactingFilter(logging.Filter):
    """Rewrites every record's final message (including exception text) with secrets masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg, record.args = redact(msg), None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install() -> None:
    """Attach the filter to every root handler and quiet per-request HTTP client logs."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
    # httpx logs every request URL at INFO (that's where the Tautulli key came from)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
