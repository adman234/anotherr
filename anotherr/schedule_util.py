"""Time-window helpers. A window: {"days": [0..6 Mon=0], "start": "HH:MM", "end": "HH:MM"}.

Windows where end <= start run overnight; 'days' refers to the day the window starts.
"""
from __future__ import annotations

import datetime as dt


def _hm(s: str) -> int:
    h, m = (s or "0:0").split(":")[:2]
    return int(h) * 60 + int(m)


def active_window(windows: list[dict], now: dt.datetime | None = None) -> tuple[dict, str] | None:
    """Return (window, key) if now falls inside a window. key identifies this occurrence."""
    now = now or dt.datetime.now()
    mins = now.hour * 60 + now.minute
    for w in windows or []:
        days = w.get("days", list(range(7)))
        a, b = _hm(w.get("start", "00:00")), _hm(w.get("end", "00:00"))
        today = now.weekday()
        if a < b:
            if today in days and a <= mins < b:
                return w, f"{now.date()}@{w.get('start')}"
        else:  # overnight (or 24h when a == b)
            if today in days and mins >= a:
                return w, f"{now.date()}@{w.get('start')}"
            yday = (today - 1) % 7
            if yday in days and mins < b:
                return w, f"{(now - dt.timedelta(days=1)).date()}@{w.get('start')}"
    return None


def in_windows(windows: list[dict], now: dt.datetime | None = None) -> bool:
    return active_window(windows, now) is not None
