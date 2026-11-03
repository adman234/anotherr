"""Time-window helpers. A window: {"days": [0..6 Mon=0], "start": "HH:MM", "end": "HH:MM"}.

Windows where end <= start run overnight; 'days' refers to the day the window starts.
An empty window list means "no restriction": scanning may run at any time.
"""
from __future__ import annotations

import datetime as dt


def _hm(s: str) -> int:
    h, m = (s or "0:0").split(":")[:2]
    return int(h) * 60 + int(m)


def active_window(windows: list[dict], now: dt.datetime | None = None) -> tuple[dict, str] | None:
    """Return (window, key) if now falls inside a window. key identifies this occurrence."""
    now = now or dt.datetime.now()
    if not windows:
        return {"start": "00:00", "end": "24:00", "anytime": True}, f"{now.date()}@anytime"
    mins = now.hour * 60 + now.minute
    for w in windows:
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


def next_window_start(windows: list[dict], now: dt.datetime | None = None) -> dt.datetime | None:
    """When the next window opens (None when there are no windows, i.e. always open)."""
    now = now or dt.datetime.now()
    if not windows:
        return None
    best = None
    for off in range(0, 8):
        day = (now + dt.timedelta(days=off)).date()
        for w in windows:
            if day.weekday() not in w.get("days", list(range(7))):
                continue
            a = _hm(w.get("start", "00:00"))
            t = dt.datetime.combine(day, dt.time(a // 60, a % 60))
            if t > now and (best is None or t < best):
                best = t
    return best
