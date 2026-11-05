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

import logging

import httpx
from mcp.server.fastmcp import FastMCP, Image

# httpx logs every request URL at INFO; keep that out of Claude Desktop's MCP log
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BRIDGE_VERSION = 4  # the server reports the version it expects; older bridges say so in their answers
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


_version_note: list = []


def _outdated() -> str:
    """One-line warning (checked once per session) when this bridge script is older than the server expects."""
    if not _version_note:
        try:
            want = int(_get("/api/status").get("mcp_bridge_version") or 0)
        except Exception:  # noqa: BLE001
            want = 0
        _version_note.append(
            f"NOTE: this Anotherr MCP bridge (v{BRIDGE_VERSION}) is older than the server expects (v{want}). Tell the "
            f"user to re-download it from {URL}/mcp/anotherr_mcp.py and restart Claude Desktop.\n" if want > BRIDGE_VERSION else "")
    return _version_note[0]


def _j(v) -> str:
    return json.dumps(v, indent=1, default=str)


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ---------------------------------------------------------------- overview

@mcp.tool()
def status() -> str:
    """Server status: running scans, queue, scheduler/pause state, memory, Whisper device, issue/review/action counts."""
    return _outdated() + _j(_get("/api/status"))


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
def process_queue(stop: bool = False) -> str:
    """Work through everything in the scan queue now, ignoring parity/Plex-stream/manual pauses,
    until the queue is empty. stop=True goes back to normal (respect pauses)."""
    res = _send("POST", "/api/queue/process", {"stop": stop})
    return "Back to normal." if stop else f"Processing {res['queued']} queued file(s) now."


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
    by_code = res["codes"]  # every code - the summary always adds up to the open total
    lines = [_outdated() + f"{res['total']} issues (page {res['page']}). All open issues by code ({sum(c['n'] for c in by_code)}): "
             + ", ".join(f"{c['code']}({c['severity']})={c['n']}" for c in by_code)]
    for r in res["rows"]:
        lines.append(f"#{r['id']} file {r['file_id']} [{r['severity']}] {r['title']} {r['label'] or ''}: {r['message']}")
    return "\n".join(lines)


@mcp.tool()
def issue_evidence(issue_id: int, frames: bool = False) -> list:
    """What an issue is based on: its details, the relevant scan measurements (scores, thresholds, sampled
    windows, per-window language detections, subtitle sync/text results) and timestamps worth checking.
    frames=True also returns those frames (max 8)."""
    i = _get(f"/api/issues/{issue_id}")
    out: list = [_j({k: i.get(k) for k in ("id", "file_id", "title", "label", "code", "severity", "message", "status",
                                           "details", "scan", "evidence", "evidence_ts", "suppressed_by_this_scan")})]
    if frames:
        for t in (i.get("evidence_ts") or [])[:MAX_FRAMES]:
            try:
                out.append(f"Frame @ {_ts(t)}")
                out.append(_frame(i["file_id"], t))
            except httpx.HTTPError as e:
                out.append(f"(frame at {_ts(t)} failed: {e})")
    return out


@mcp.tool()
def file_streams(file_id: int) -> str:
    """Every stream in the file right now (live ffprobe): type, codec, language tag, title, default/forced
    flags, channels, duration - plus external subtitle files next to it."""
    r = _get(f"/api/files/{file_id}/streams")
    lines = [f"{r['path']}  duration {_ts(r['duration'])}  {r['format']}"]
    for v in r["video"]:
        lines.append(f"video #{v['index']}: {v['codec']} {v.get('width')}x{v.get('height')} {v.get('field_order') or ''}")
    for a in r["audio"]:
        lines.append(f"audio a:{a['a_index']} (#{a['index']}): {a['codec']} {a.get('channels')}ch lang={a['lang_tag'] or 'und'} "
                     f"title={a['title']!r}{' DEFAULT' if a['default'] else ''}{' commentary' if a.get('commentary') else ''}")
    for s in r["subs"]:
        where = "external " + s["path"] if s.get("external") else f"s:{s['s_index']} (#{s['index']})"
        lines.append(f"subtitle {where}: {s['codec']} lang={s['lang_tag'] or 'und'} title={s['title']!r}"
                     f"{' DEFAULT' if s['default'] else ''}{' FORCED' if s['forced'] else ''}"
                     f"{' HI' if s.get('hearing_impaired') else ''}{' (image)' if not s['text'] else ''}")
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
    """Proposed/finished fix-up actions. status: proposed|done|failed|rejected|withdrawn (comma-separated ok).
    Proposals are withdrawn automatically when the issues behind them are resolved."""
    rows = _get("/api/actions", status=status)
    if not rows:
        return "No actions."
    return "\n".join(f"action {a['id']} [{a['status']}] {a['kind']} - {a['title']} {a['label'] or ''}: {a['reason']}"
                     + (f" -> {a['result']}" if a.get("result") else "") for a in rows)


@mcp.tool()
def retag_preview(action_id: int) -> str:
    """Details of a 'retag_audio' proposal: every file and track it would change (old -> new tag), the language
    evidence behind each, and the exact command that would run. Nothing is changed by looking."""
    a = _get(f"/api/actions/{action_id}")
    p = a.get("payload") or {}
    lines = [f"action {a['id']} [{a['status']}] {a['reason']}",
             f"case: {p.get('case')}  review: {p.get('review')}  series language: {p.get('series_language')}"]
    for f in p.get("files", []):
        lines.append(f"- {f.get('label') or 'file ' + str(f['file_id'])} track {f['a_index'] + 1}: "
                     f"{f.get('old') or 'untagged'} -> {f['new']} ({f.get('why')}, {f.get('conf', 0):.0%})"
                     + (f" | {f.get('status')}: {f.get('outcome', '')}" if f.get("status") not in (None, "pending") else "")
                     + (f"\n    {f.get('command')}" if a["status"] == "proposed" else ""))
    return "\n".join(lines)


@mcp.tool()
def undo_retag(action_id: int) -> str:
    """Restore the original audio tags a finished retag_audio action wrote (from its rollback log).
    Only do this when the user asks."""
    return _j(_send("POST", f"/api/actions/{action_id}/undo"))


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
