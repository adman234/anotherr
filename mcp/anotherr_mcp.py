# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "httpx"]
# ///
"""Anotherr MCP server for Claude Desktop.

A thin stdio bridge to the Anotherr API running on your Unraid server. All the heavy
lifting (ffmpeg, Whisper, subtitle sync) happens on the server; Claude only gets called in
for things that need eyes or judgment - reading title cards, spotting hardcoded subs, etc.

Claude Desktop config (claude_desktop_config.json):

  "mcpServers": {
    "anotherr": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/anotherr_mcp.py"],
      "env": {"ANOTHERR_URL": "http://tower.local:9797", "ANOTHERR_API_KEY": ""}
    }
  }
"""
from __future__ import annotations

import base64
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP, Image

URL = os.environ.get("ANOTHERR_URL", "http://localhost:9797").rstrip("/")
KEY = os.environ.get("ANOTHERR_API_KEY", "")
MAX_FRAMES = int(os.environ.get("ANOTHERR_MAX_FRAMES", "8"))
FRAME_WIDTH = int(os.environ.get("ANOTHERR_FRAME_WIDTH", "768"))

mcp = FastMCP("anotherr", instructions=(
    "Anotherr monitors a Plex media library on Unraid (via Sonarr/Radarr) for bad files: corruption, wrong "
    "audio language, missing/out-of-sync subtitles, wrong episodes. Local checks run on the server. Use "
    "pending_reviews + get_review to look at frames the server flagged for visual checks, then submit_review. "
    "Never approve a redownload without the user's say-so unless they asked you to handle approvals."))


def _client() -> httpx.Client:
    return httpx.Client(base_url=URL, headers={"X-Api-Key": KEY} if KEY else {}, timeout=120)


def _get(path: str, **params):
    with _client() as c:
        r = c.get(path, params={k: v for k, v in params.items() if v not in (None, "")})
        r.raise_for_status()
        return r.json()


def _send(method: str, path: str, body: dict | None = None):
    with _client() as c:
        r = c.request(method, path, json=body or {})
        r.raise_for_status()
        return r.json()


def _frame(file_id: int, t: float, width: int = FRAME_WIDTH) -> Image:
    with _client() as c:
        r = c.get(f"/api/files/{file_id}/frame", params={"t": t, "w": width})
        r.raise_for_status()
        return Image(data=r.content, format="jpeg")


def _j(v) -> str:
    return json.dumps(v, indent=1, default=str)


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ---------------------------------------------------------------- overview

@mcp.tool()
def status() -> str:
    """Server status: current scan, queue, scheduler/pause state, Whisper device, issue/review/action counts."""
    return _j(_get("/api/status"))


@mcp.tool()
def coverage() -> str:
    """How much of each library and each Unraid disk has been checked, plus what will be scanned next."""
    return _j(_get("/api/coverage"))


@mcp.tool()
def recent_activity(limit: int = 30) -> str:
    """Recent log lines (scans, syncs, actions)."""
    rows = _get("/api/activity", limit=limit)
    return "\n".join(f"[{r['level']}] {r['message']}" for r in rows)


# ---------------------------------------------------------------- library

@mcp.tool()
def search_library(query: str = "", source: str = "", status: str = "", page: int = 1) -> str:
    """Find series/movies. source: sonarr|radarr. status: problems|unscanned. Returns item ids for other tools."""
    res = _get("/api/items", search=query, source=source, status=status, page=page, per_page=40)
    lines = [f"{res['total']} items (page {res['page']})"]
    for r in res["rows"]:
        lines.append(f"item {r['id']}: {r['title']} ({r.get('year') or '?'}) [{r['source']}{' anime' if r['is_anime'] else ''}] "
                     f"lang={r['original_language'] or '?'}{' override=' + r['language_override'] if r['language_override'] else ''} "
                     f"files={r['files']} scanned={r['scanned'] or 0} bad={r['bad'] or 0} warn={r['warn'] or 0}")
    return "\n".join(lines)


@mcp.tool()
def item_files(item_id: int) -> str:
    """List files (episodes) of a series/movie with their check status."""
    it = _get(f"/api/items/{item_id}")
    lines = [f"{it['title']} ({it.get('year')}) - {len(it['files'])} files"]
    for f in it["files"]:
        lines.append(f"file {f['id']}: {f['label'] or ''} {f['status']} issues={f['open_issues']} disk={f['disk']} "
                     f"{f['quality']} [{f['release_group']}]")
    return "\n".join(lines)


@mcp.tool()
def file_report(file_id: int) -> str:
    """Full report for one file: streams, audio language detection, subtitle analysis, issues, reviews, actions."""
    f = _get(f"/api/files/{file_id}")
    res = (f.get("last_scan") or {}).get("result") or {}
    slim = {
        "title": f["item"]["title"], "label": f["label"], "path": f["arr_path"], "disk": f["disk"],
        "status": f["status"], "episodes": f["episodes"], "release_group": f["release_group"],
        "expected_language": res.get("expected_language"), "runtime_ratio": res.get("runtime_ratio"),
        "audio_tracks": res.get("audio_tracks"), "subtitle_tracks": res.get("subtitle_tracks"),
        "subtitle_sync": res.get("subtitle_sync"), "subtitle_content": res.get("subtitle_content"),
        "segment_starts": res.get("segment_starts"), "duration": (res.get("streams") or {}).get("duration"),
        "issues": [{k: i[k] for k in ("id", "severity", "code", "message", "status")} for i in f["issues"]],
        "reviews": [{k: r[k] for k in ("id", "kind", "status", "verdict", "notes")} for r in f["reviews"]],
        "actions": [{k: a[k] for k in ("id", "kind", "status", "reason", "result")} for a in f["actions"]],
    }
    return _j(slim)


@mcp.tool()
def set_language_override(item_id: int, language: str = "") -> str:
    """Override the preferred audio language for a series/movie (e.g. 'eng' to prefer a dub). Empty clears it."""
    it = _send("PATCH", f"/api/items/{item_id}", {"language_override": language})
    return f"{it['title']}: language_override={it['language_override'] or '(none - original language)'}"


# ---------------------------------------------------------------- scanning

@mcp.tool()
def run_now(hours: float = 2) -> str:
    """Start priority-based scheduled scanning immediately, ignoring the scan windows, for N hours.
    hours=0 cancels. Parity-check and Plex-stream pauses still apply."""
    res = _send("POST", "/api/control/run-now", {"hours": hours})
    return "Run-now cancelled." if not res["override_until"] else f"Scanning by priority for {hours}h."


@mcp.tool()
def queue_scan(scope: str, ids: list[int] | None = None, disk: str = "", library: str = "", count: int = 0,
               profile: str = "standard") -> str:
    """Queue scans. scope: file|item|disk|library|random|problems|unscanned.
    library: tv|movies|anime. count limits/samples the selection (random defaults to 10).
    profile: quick (~seconds) | standard (~1-2 min) | deep (full decode, several min)."""
    res = _send("POST", "/api/scan", {"scope": scope, "ids": ids or [], "disk": disk, "library": library,
                                      "count": count, "profile": profile})
    return f"Queued {res['queued']} file(s)."


@mcp.tool()
def list_issues(severity: str = "", code: str = "", search: str = "", status: str = "open", page: int = 1) -> str:
    """List detected issues. severity: info|warning|error|critical (comma-separated ok)."""
    res = _get("/api/issues", severity=severity, code=code, search=search, status=status, page=page, per_page=60)
    lines = [f"{res['total']} issues. Open by code: " + ", ".join(f"{c['code']}({c['severity']})={c['n']}" for c in res["codes"][:15])]
    for r in res["rows"]:
        lines.append(f"#{r['id']} file {r['file_id']} [{r['severity']}] {r['title']} {r['label'] or ''}: {r['message']}")
    return "\n".join(lines)


@mcp.tool()
def ignore_issue(issue_id: int) -> str:
    """Mark an issue as ignored (stays ignored on rescans)."""
    _send("PATCH", f"/api/issues/{issue_id}", {"status": "ignored"})
    return "ignored"


# ---------------------------------------------------------------- visual reviews

@mcp.tool()
def pending_reviews(limit: int = 15) -> str:
    """Files waiting for a visual check (title cards, movie identity, picture quality)."""
    rows = _get("/api/reviews", status="pending", limit=limit)
    if not rows:
        return "No pending reviews."
    return "\n".join(f"review {r['id']} [{r['kind']}] file {r['file_id']}: {r['title']} {r['label'] or ''} "
                     f"({len(r['timestamps'])} frames)" for r in rows)


@mcp.tool()
def get_review(review_id: int) -> list:
    """Get a review's question and its frames as images. Look carefully, then call submit_review."""
    r = _get(f"/api/reviews/{review_id}")
    out: list = [f"Review {r['id']} ({r['kind']}) - {r['title']} {r['label'] or ''}\n{r['question']}\n"
                 f"Frames at: {', '.join(_ts(t) for t in r['timestamps'][:MAX_FRAMES])}"]
    for t in r["timestamps"][:MAX_FRAMES]:
        try:
            out.append(f"Frame @ {_ts(t)}")
            out.append(_frame(r["file_id"], t))
        except httpx.HTTPError as e:
            out.append(f"(frame at {_ts(t)} failed: {e})")
    return out


@mcp.tool()
def get_frames(file_id: int, timestamps: list[float], width: int = FRAME_WIDTH) -> list:
    """Extract frames (seconds) from any file for an ad-hoc look. Max 8 per call."""
    out: list = []
    for t in timestamps[:MAX_FRAMES]:
        out.append(f"Frame @ {_ts(t)}")
        out.append(_frame(file_id, t, width))
    return out


@mcp.tool()
def submit_review(review_id: int, verdict: str, notes: str = "", request_redownload: bool = False,
                  severity: str = "error") -> str:
    """Record your finding. verdict: ok | problem | dismiss (can't tell). With problem, set request_redownload
    to propose a redownload (it still needs approval unless automation allows it)."""
    res = _send("POST", f"/api/reviews/{review_id}/verdict",
                {"verdict": verdict, "notes": notes, "request_redownload": request_redownload, "severity": severity})
    return _j(res)


# ---------------------------------------------------------------- actions

@mcp.tool()
def list_actions(status: str = "proposed") -> str:
    """Proposed/finished fix-up actions. status: proposed|done|failed|rejected (comma-separated ok)."""
    rows = _get("/api/actions", status=status)
    if not rows:
        return "No actions."
    return "\n".join(f"action {a['id']} [{a['status']}] {a['kind']} - {a['title']} {a['label'] or ''}: {a['reason']}"
                     + (f" -> {a['result']}" if a.get("result") else "") for a in rows)


@mcp.tool()
def propose_action(file_id: int, kind: str, reason: str) -> str:
    """Propose resub (Bazarr subtitle search) or redownload (blocklist + search in Sonarr/Radarr)."""
    return _j(_send("POST", "/api/actions", {"file_id": file_id, "kind": kind, "reason": reason}))


@mcp.tool()
def decide_action(action_id: int, approve: bool) -> str:
    """Approve (runs immediately) or reject a proposed action. Only approve when the user has agreed."""
    path = f"/api/actions/{action_id}/{'approve' if approve else 'reject'}"
    return _j(_send("POST", path))


@mcp.prompt()
def review_pending() -> str:
    """Work through the visual review queue."""
    return ("Use the anotherr tools: call pending_reviews, then for each review call get_review, study the frames "
            "against the question, and call submit_review with verdict ok/problem/dismiss and short notes. Only set "
            "request_redownload when you're confident. Summarise what you found at the end.")


@mcp.prompt()
def nightly_summary() -> str:
    """Summarise what the scanner found recently."""
    return ("Use the anotherr tools: check status, recent_activity and list_issues (severity error,critical). "
            "Summarise new problems grouped by show/movie, what actions are proposed, and what I should approve.")


if __name__ == "__main__":
    mcp.run()
