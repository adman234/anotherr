"""Shift an offset subtitle track back into sync (no Bazarr needed).

The sync check already knows by how much a track is off ("text matches once shifted"), so the fix is just moving
every cue by that amount. Sidecar files (.srt / .vtt / .ass / .ssa) are rewritten in place after a one-time backup.
Embedded Matroska text tracks are remuxed with mkvmerge --sync into a temp file that replaces the original only
after a sanity check. Positive offset = subtitles appear late, so the cues move earlier by that amount."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time

from . import db, settings

KIND = "shift_subtitle"
MKVMERGE = os.environ.get("ANOTHERR_MKVMERGE", "mkvmerge")
MAX_SHIFT = 15.0  # the sync check never reports more; refuse anything bigger rather than guess
BACKUP_SUFFIX = ".anotherr-bak"
TEXT_EXT = (".srt", ".vtt", ".ass", ".ssa")
REMUX_TIMEOUT = 3600

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def _fmt_ts(sec: float, comma: bool) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"


def _shift_ts(m: re.Match, delta: float, comma: bool) -> str:
    h, mi, s, frac = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    sec = int(h or 0) * 3600 + mi * 60 + s + int(frac.ljust(3, "0")) / 1000.0
    return _fmt_ts(sec + delta, comma)


def shift_srt_like(text: str, delta: float) -> str:
    """SRT and WebVTT: only the 'start --> end' lines carry times (VTT cue settings after the end time are kept)."""
    out = []
    for line in text.split("\n"):
        if "-->" in line:
            comma = "," in line.split("-->")[0]
            line = _TS.sub(lambda m: _shift_ts(m, delta, comma), line)
        out.append(line)
    return "\n".join(out)


def _ass_ts(sec: float) -> str:
    cs = max(0, int(round(sec * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_ASS_T = re.compile(r"^(\d+):(\d{2}):(\d{2})[.](\d{2})$")


def shift_ass(text: str, delta: float) -> str:
    """ASS/SSA: Start and End are fields 2 and 3 of every Dialogue/Comment line."""
    def one(v: str) -> str:
        m = _ASS_T.match(v.strip())
        if not m:
            return v
        sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100.0
        return _ass_ts(sec + delta)

    out = []
    for line in text.split("\n"):
        if re.match(r"^(Dialogue|Comment):", line):
            head, _, rest = line.partition(":")
            parts = rest.lstrip().split(",", 9)
            if len(parts) >= 10:
                parts[1], parts[2] = one(parts[1]), one(parts[2])
                line = f"{head}: " + ",".join(parts)
        out.append(line)
    return "\n".join(out)


def shift_text(text: str, delta: float, ext: str) -> str:
    return shift_ass(text, delta) if ext.lower() in (".ass", ".ssa") else shift_srt_like(text, delta)


def plan(file_id: int) -> list[dict]:
    """Tracks of the file's latest finished scan that were judged offset: [{name, external, path, s_index, offset}]."""
    row = db.q1("SELECT result FROM scans WHERE file_id=? AND status='done' AND result LIKE '%subtitle_sync%' "
                "ORDER BY id DESC LIMIT 1", (file_id,))
    res = db.unj(row["result"], {}) if row else {}
    tracks = res.get("subtitle_tracks") or []
    by_name = {}
    for t in tracks:
        by_name.setdefault(t.get("title") or t.get("codec"), t)
    out = []
    for s in res.get("subtitle_sync") or []:
        if s.get("verdict_final") != "offset" or s.get("offset") is None:
            continue
        t = by_name.get(s.get("track")) or {}
        out.append({"name": s.get("track"), "external": bool(s.get("external")),
                    "path": s.get("path") or t.get("path"), "s_index": s.get("s_index", t.get("s_index")),
                    "codec": t.get("codec"), "text": t.get("text", True), "offset": float(s["offset"])})
    return out


def _backup(path: str) -> None:
    bak = path + BACKUP_SUFFIX
    if not os.path.exists(bak):  # keep the first original, so repeated shifts can always be undone to the source
        with open(path, "rb") as a, open(bak, "wb") as b:
            b.write(a.read())


def _shift_sidecar(path: str, offset: float) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXT:
        raise RuntimeError(f"{os.path.basename(path)}: not a text subtitle file")
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"{os.path.basename(path)}: not writable")
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
        enc = "utf-8"
    except UnicodeDecodeError:
        text, enc = raw.decode("latin-1"), "latin-1"
    crlf = "\r\n" in text
    new = shift_text(text.replace("\r\n", "\n"), -offset, ext)
    if crlf:
        new = new.replace("\n", "\r\n")
    if new == text:
        raise RuntimeError(f"{os.path.basename(path)}: no cue times found to shift")
    _backup(path)
    tmp = path + ".anotherr-tmp"
    with open(tmp, "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + new.encode(enc, errors="replace"))
    os.replace(tmp, path)
    return f"{os.path.basename(path)} shifted {-offset:+.2f}s (backup kept as {os.path.basename(path)}{BACKUP_SUFFIX})"


def _mkv_tracks(path: str) -> list[dict]:
    r = subprocess.run([MKVMERGE, "-J", path], capture_output=True, text=True, timeout=120)
    if r.returncode > 1:
        raise RuntimeError("mkvmerge could not read the file: " + (r.stderr or r.stdout)[:200])
    return json.loads(r.stdout).get("tracks") or []


def remux_args(path: str, tmp: str, delays: dict[int, int]) -> list[str]:
    args = [MKVMERGE, "-o", tmp]
    for tid, ms in sorted(delays.items()):
        args += ["--sync", f"{tid}:{ms}"]
    return args + [path]


def _shift_embedded(path: str, f: dict, picks: list[dict]) -> str:
    from . import trackedit

    if not path.lower().endswith(trackedit.MATROSKA):
        raise RuntimeError("embedded subtitles can only be shifted in Matroska files")
    cfg = settings.get()
    from .sync import map_path

    if not cfg["retag"].get("enabled"):
        raise RuntimeError("file editing is turned off in Settings -> Retag audio")
    wp = trackedit.write_path(f, path)
    if not wp:
        raise RuntimeError("file is not writable")
    if not trackedit._allowed([path, map_path(f["arr_path"]), wp], cfg):
        raise RuntimeError("file is outside the allowed paths (Settings -> Retag audio)")
    if os.stat(wp).st_nlink > 1 and not cfg["retag"].get("break_hardlinks"):
        raise RuntimeError("file is hardlinked (see retag.break_hardlinks)")
    if trackedit._playing(wp):
        raise RuntimeError("file is playing right now - try again later")
    before = _mkv_tracks(wp)
    subs = [t for t in before if t.get("type") == "subtitles"]
    delays = {}
    for p in picks:
        i = p.get("s_index")
        if i is None or i >= len(subs):
            raise RuntimeError(f"embedded track {p['name']} not found in the file any more")
        delays[subs[i]["id"]] = int(round(-p["offset"] * 1000))
    tmp = trackedit.tmp_path(wp)
    trackedit.log_temp(wp)  # a restart mid-remux kills us before the finally below; the startup sweep cleans up
    try:
        r = subprocess.run(remux_args(wp, tmp, delays), capture_output=True, text=True, timeout=REMUX_TIMEOUT)
        if r.returncode > 1 or not os.path.exists(tmp):
            raise RuntimeError("mkvmerge failed: " + (r.stderr or r.stdout)[-300:])
        after = _mkv_tracks(tmp)
        if [t.get("type") for t in after] != [t.get("type") for t in before]:
            raise RuntimeError("track layout changed during remux - original left untouched")
        a, b = os.path.getsize(wp), os.path.getsize(tmp)
        if abs(a - b) > max(a * 0.02, 5_000_000):
            raise RuntimeError(f"remuxed size {b} differs too much from {a} - original left untouched")
        os.replace(tmp, wp)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return "; ".join(f"embedded {p['name']} shifted {-p['offset']:+.2f}s" for p in picks) + " (remuxed)"


def run(f: dict) -> str:
    """Shift every offset track of the file; raises with a reason when nothing could be done."""
    from .scanner import resolve_path

    picks = [p for p in plan(f["id"]) if 0 < abs(p["offset"]) <= MAX_SHIFT]
    if not picks:
        raise RuntimeError("no offset subtitle track in the last scan to shift (rescan the file first)")
    path = resolve_path(f)
    if not path:
        raise RuntimeError("file not found on disk")
    done, errors = [], []
    ext_picks = [p for p in picks if p["external"] and p.get("path")]
    emb_picks = [p for p in picks if not p["external"] and p.get("text") is not False]
    for p in ext_picks:
        try:
            done.append(_shift_sidecar(p["path"], p["offset"]))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    if emb_picks:
        try:
            done.append(_shift_embedded(path, f, emb_picks))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    if not done:
        raise RuntimeError("; ".join(errors) or "nothing to shift")
    return "; ".join(done + [f"skipped: {e}" for e in errors])


def undo_command(path: str) -> str:
    return shlex.join(["mv", "-f", path + BACKUP_SUFFIX, path])


def queue_recheck(file_id: int) -> None:
    db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?,?,?,?)",
         (file_id, "standard", "action", time.time() + 30))
