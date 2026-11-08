"""subgen_transcribe: ask the subgen container for an English subtitle, strictly one file at a time.

Talks to the subarr-subgen patch stack (checked against patch rev v4.38):
  GET  /queue                   {"queued": [{path, type}], "processing": [...], "idle": bool, "version", "model",
                                 "capabilities": {...}}; a job is identified by its (subgen-side) path
  POST /batch?directory=<file>&forceLanguage=<iso1>&task=transcribe|translate&bypass_skip=true
                                -> {"walked", "queued", "skipped", "already_in_queue", "no_audio", ...}
                                   404 when walked == 0 (no file there), 403 outside SUBGEN_PATH_ALLOWLIST
  POST /queue/cancel?path=      cancels a QUEUED job only; a running job can't be stopped
subgen keeps its queue in memory (lost on restart) and writes <video stem>[.subgen][.<model>].<lang>.srt, where
<lang> follows its GLOBAL task setting - a per-request translate on a "transcribe" instance names the English
result after the source language (e.g. .ko.srt). Anotherr checks the result and renames it to <stem>.en.srt.

Proposal rule: open subs_missing (or subs_signs_only), the chosen audio track's language is confident with every
window agreeing, not "almost no speech", no full English subtitle, subgen would pick that audio track, and either
Bazarr's search already failed for the series (and the episode is over a week old) or a person asked for it.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import statistics
import threading
import time

import httpx

from . import db, langs, settings

log = logging.getLogger("anotherr.subgen")
KIND = "subgen_transcribe"
TRIGGER = {"subs_missing", "subs_signs_only"}
ACTIVE_FILE = ("waiting", "submitted", "running")
_ENGLISH = set("""the a an and or but of to in on at for with from by is are was were be been being am i you he she
it we they me him her us them my your his its our their this that these there here what who where when why how not no
yes do does did have has had will would can could should just so if then than now up out about all one get got go
going know like okay ok oh hey well right come what's i'm you're it's don't that's let's we're can't""".split())


class SubgenError(Exception):
    pass


# ====================================================================== client

def cfg() -> dict:
    return settings.get().get("subgen") or {}


def configured() -> bool:
    return bool((cfg().get("url") or "").strip())


class Client:
    def __init__(self, url: str | None = None):
        self.url = (url if url is not None else cfg().get("url") or "").rstrip("/")

    def _req(self, method: str, path: str, **params):
        if not self.url:
            raise SubgenError("subgen.url is not set")
        try:
            r = httpx.request(method, self.url + path, params=params, timeout=30)
        except httpx.HTTPError as e:
            raise SubgenError(f"subgen unreachable: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {"text": r.text[:300]}
        return r.status_code, body

    def queue(self) -> dict:
        code, body = self._req("GET", "/queue")
        if code != 200 or not isinstance(body, dict):
            raise SubgenError(f"subgen /queue -> {code}: {str(body)[:200]}")
        return body

    def submit(self, path: str, lang: str, task: str) -> tuple[int, dict]:
        return self._req("POST", "/batch", directory=path, forceLanguage=lang, task=task, bypass_skip="true")

    def cancel(self, path: str) -> dict:
        return self._req("POST", "/queue/cancel", path=path)[1]


def _paths(q: dict, key: str) -> set[str]:
    return {t.get("path") for t in q.get(key) or [] if isinstance(t, dict)}


# ====================================================================== eligibility

def subgen_path(f: dict) -> str:
    """The path subgen receives: the user-share path, through subgen.path_map (longest prefix wins)."""
    from .sync import map_path

    p = map_path(f["arr_path"])
    best = None
    for m in cfg().get("path_map") or []:
        src = (m.get("from") or "").rstrip("/")
        if src and (p == src or p.startswith(src + "/")) and (best is None or len(src) > len(best[0])):
            best = (src, (m.get("to") or "").rstrip("/"))
    return best[1] + p[len(best[0]):] if best else p


def _sub_lang() -> str:
    return langs.norm(settings.get()["languages"]["subtitle_language"]) or "eng"


def _same(a, b) -> bool:
    from .scanner import lang_match

    return lang_match(a or "", b or "")


def _subgen_track_pick(tracks: list[dict], lang3: str) -> dict | None:
    """subgen extracts the first track whose TAG equals the forced language, else the first track."""
    if len(tracks) <= 1:
        return tracks[0] if tracks else None
    return next((t for t in tracks if langs.norm(t.get("tag")) == lang3), tracks[0])


def _busy_files() -> dict[int, str]:
    """Files already covered: in an open/running subgen batch, or recently done/failed there (no auto re-propose)."""
    out = {}
    for a in db.q("SELECT status, payload, updated_at FROM actions WHERE kind=?", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        for e in p.get("files", []):
            st = e.get("status")
            if a["status"] in ("queued", "running") and st in ACTIVE_FILE:
                out[e["file_id"]] = "already queued for subgen"
            elif st == "done" and a["updated_at"] > time.time() - 30 * 86400:
                out[e["file_id"]] = "subgen already made a subtitle for it (waiting for the rescan)"
            elif st == "failed" and a["updated_at"] > time.time() - 7 * 86400:
                out.setdefault(e["file_id"], f"subgen failed on it recently: {e.get('outcome', '')}")
    return out


def _too_new(f: dict) -> bool:
    import datetime as dt

    s = f.get("date_added") or ""
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return t > time.time() - float(cfg().get("new_episode_days", 7)) * 86400


def plan_file(f: dict, item: dict, codes: set[str], manual: bool = False, busy: dict | None = None) -> tuple[dict | None, str]:
    """(plan, why-not). The plan names the audio track, language, task and the path subgen gets."""
    from . import actions, defaults
    from .retag import _evidence, _season

    if not codes & TRIGGER:
        return None, "no missing-subtitle issue"
    if "no_dialogue" in codes:
        return None, "almost no speech detected"
    busy = _busy_files() if busy is None else busy
    if f["id"] in busy:
        return None, busy[f["id"]]
    sub = _sub_lang()
    ev = _evidence(f) or {}
    if any(_same(s.get("lang"), sub) and s.get("kind") == "full" for s in ev.get("subs") or []):
        return None, f"already has a full {langs.name(sub)} subtitle"
    tracks = ev.get("tracks") or []
    ok = [t for t in tracks if t.get("confident") and t.get("all_agree") and t.get("windows") and not t.get("commentary")]
    pref = langs.norm(item.get("language_override")) or langs.norm(item.get("original_language"))
    cur = defaults.current_default_audio(tracks)
    track = (next((t for t in ok if _same(t.get("effective"), pref)), None) if pref else None) or \
        (cur if cur in ok else None)
    if not track:
        return None, "the audio language isn't confidently detected (every sampled window must agree)"
    lang3 = langs.norm(track.get("effective"))
    lang1 = langs.ISO1.get(lang3, "")
    if not lang1:
        return None, f"{langs.name(lang3) or 'that language'} isn't a language subgen's Whisper model supports"
    if _same(lang3, sub):
        task = "transcribe"
    elif sub == "eng":
        task = "translate"
    else:
        return None, f"Whisper only translates into English, not {langs.name(sub)}"
    pick = _subgen_track_pick(tracks, lang3)
    if pick is not track:
        return None, (f"subgen would transcribe audio track {pick['a_index'] + 1} (tagged {pick.get('tag') or 'und'}), "
                      f"not the {langs.name(lang3)} track {track['a_index'] + 1} - fix its tag first")
    if not manual:
        if not actions.resub_failed_before(item["id"]):
            return None, "Bazarr's search hasn't failed for this show yet - a resub comes first"
        if _too_new(f):
            return None, "a new episode - Bazarr gets a week to find a subtitle first"
    model = ("Whisper " + ((settings.state_get("subgen_info") or {}).get("model") or "")).strip()
    return {"file_id": f["id"], "label": f.get("label") or "", "season": _season(f), "path": subgen_path(f),
            "a_index": track["a_index"], "language": lang1, "lang3": lang3, "task": task, "tag": track.get("tag") or "",
            "overridden": langs.norm(track.get("tag")) != lang3,
            "backend": (f"Parakeet if enabled, else {model}" if task == "transcribe" and lang3 == "eng"
                        else f"{model} ({task})"),
            "duration": f.get("duration") or 0, "manual": manual, "status": "waiting"}, ""


def explain(file_id: int, manual: bool = True) -> tuple[dict | None, str]:
    f = db.q1("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        return None, "file not found"
    item = db.q1("SELECT * FROM items WHERE id=?", (f["item_id"],))
    codes = {r["code"] for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (file_id,))}
    if not configured():
        return None, "subgen isn't set up (Settings -> Subgen -> url)"
    return plan_file(f, item, codes, manual=manual)


# ====================================================================== proposals

def _manual() -> set[int]:
    return set(settings.state_get("subgen_manual", []) or [])


def request(file_ids: list[int]) -> dict:
    """A person asked for subgen on these files: they skip the "Bazarr failed first" rule."""
    ok, why = [], {}
    for fid in file_ids:
        plan, reason = explain(int(fid), manual=True)
        if plan:
            ok.append(int(fid))
        else:
            why[int(fid)] = reason
    if ok:
        settings.state_set("subgen_manual", sorted(_manual() | set(ok)))
        for iid in {r["item_id"] for r in db.q(f"SELECT item_id FROM files WHERE id IN ({','.join('?' * len(ok))})", ok)}:
            refresh_item(iid)
    return {"proposed": ok, "not_possible": why}


def wanted(file_id: int) -> bool:
    """propose_for_file asks this: True means subgen covers the file, so no resub/Whisper proposal."""
    if not configured():
        return False
    plan, _ = explain(file_id, manual=file_id in _manual())
    return bool(plan)


def _reason(item: dict, season, files: list[dict]) -> str:
    tasks = sorted({f"{'translate' if e['task'] == 'translate' else 'transcribe'} {langs.name(e['lang3'])}" for e in files})
    where = item["title"] + (f" Season {season}" if season is not None else "")
    return (f"{where}: generate English subtitles with subgen for {len(files)} file(s) ({', '.join(tasks)}). "
            f"Sent one at a time; each result is checked (language, cue density) and renamed to .en.srt.")


def refresh_item(item_id: int) -> dict:
    made = {"created": 0, "updated": 0, "withdrawn": 0}
    if not configured():
        return made
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        return made
    manual, busy = _manual(), _busy_files()
    groups: dict = {}
    for f in db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item_id,)):
        codes = {r["code"] for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (f["id"],))}
        if not codes & TRIGGER:
            continue
        plan, _ = plan_file(f, item, codes, manual=f["id"] in manual, busy=busy)
        if plan:
            groups.setdefault(plan["season"], []).append(plan)
    existing = {}
    for a in db.q("SELECT * FROM actions WHERE kind=? AND status='proposed'", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == item_id:
            existing[p.get("season")] = a
    now = time.time()
    for season, files in groups.items():
        files.sort(key=lambda e: e["label"])
        payload = {"item_id": item_id, "season": season, "family": "subgen", "files": files}
        a = existing.pop(season, None)
        if a:
            db.x("UPDATE actions SET reason=?, payload=?, file_id=?, updated_at=? WHERE id=?",
                 (_reason(item, season, files), db.j(payload), files[0]["file_id"], now, a["id"]))
            made["updated"] += 1
        else:
            db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                 "VALUES (?,?,?,?, 'proposed', 0, ?, ?, ?)", (files[0]["file_id"], KIND, _reason(item, season, files),
                                                              db.j(sorted(TRIGGER)), db.j(payload), now, now))
            made["created"] += 1
        ids = [e["file_id"] for e in files]
        made["withdrawn"] += db.n(
            f"UPDATE actions SET status='withdrawn', result='Withdrawn: replaced by a subgen proposal', updated_at=? "
            f"WHERE status='proposed' AND kind IN ('resub','whisper_subtitle') AND file_id IN ({','.join('?' * len(ids))})",
            [now, *ids])
    for a in existing.values():
        db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: no file in this group needs subgen anymore', "
             "updated_at=? WHERE id=?", (now, a["id"]))
        made["withdrawn"] += 1
    return made


def refresh_all() -> int:
    if not configured():
        return 0
    ids = {r["item_id"] for r in db.q(
        "SELECT DISTINCT f.item_id FROM issues x JOIN files f ON f.id=x.file_id WHERE x.status='open' AND f.missing=0 "
        "AND x.code IN ('subs_missing','subs_signs_only')")}
    for i in ids:
        try:
            refresh_item(i)
        except Exception:  # noqa: BLE001
            log.exception("subgen refresh failed for item %s", i)
    return len(ids)


# ====================================================================== result check

def find_output(local_video: str, since: float) -> str | None:
    """The .srt subgen wrote next to the video: <stem>.*.srt, modified after the submit."""
    stem = os.path.splitext(local_video)[0]
    hits = [p for p in glob.glob(glob.escape(stem) + ".*srt") if p.lower().endswith(".srt")
            and os.path.getmtime(p) >= since - 30]
    return max(hits, key=os.path.getmtime) if hits else None


def english_score(texts: list[str]) -> float:
    """Share of words that are common English words (Korean/Japanese output scores ~0)."""
    words = re.findall(r"[^\W\d_]+'?[^\W\d_]*", " ".join(texts).lower())
    return sum(w in _ENGLISH for w in words) / len(words) if words else 0.0


def check_output(path: str, duration: float, lang3: str) -> tuple[bool, dict]:
    """Independent checks (not a text match against Anotherr's own Whisper): cue density, spread, language."""
    import pysubs2

    try:
        subs = [e for e in pysubs2.load(path) if e.text.strip()]
    except Exception as e:  # noqa: BLE001
        return False, {"why": f"can't read the subtitle: {e}"}
    dur_min = max(duration, 60) / 60
    per_min = round(len(subs) / dur_min, 1)
    span = (subs[-1].end - subs[0].start) / 1000 if subs else 0
    texts = [e.plaintext for e in subs[:120]]
    score = round(english_score(texts), 2)
    ev = {"cues": len(subs), "per_min": per_min, "span_s": round(span), "english_score": score}
    lo = float(cfg().get("min_cues_per_min", 3))
    if len(subs) < 10 or per_min < lo:
        return False, {**ev, "why": f"too few cues ({len(subs)}, {per_min}/min; a dialogue track is usually 10-20/min)"}
    if per_min > 60:
        return False, {**ev, "why": f"implausibly many cues ({per_min}/min)"}
    if duration and span < 0.4 * duration:
        return False, {**ev, "why": f"cues only cover {round(100 * span / duration)}% of the running time"}
    if lang3 == "eng" or _sub_lang() == "eng":
        if score < float(cfg().get("min_english_score", 0.2)):
            return False, {**ev, "why": f"the text isn't English (common-word score {score}) - subgen probably "
                                        "transcribed instead of translating"}
    return True, ev


def _rename_targets(f: dict, local_video: str) -> list[str]:
    from .sync import map_path

    dirs = []
    share = map_path(f["arr_path"])
    for d in (os.path.dirname(share), os.path.dirname(local_video)):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def finalize(f: dict, local_video: str, out: str) -> str:
    """Rename subgen's file to <stem>.<lang1>.srt so Plex/Bazarr/Anotherr read it as an English subtitle."""
    lang1 = langs.ISO1.get(_sub_lang(), "en")
    name = os.path.splitext(os.path.basename(local_video))[0] + f".{lang1}.srt"
    if os.path.basename(out) == name:
        return out
    errors = []
    for d in _rename_targets(f, local_video):
        src, dst = os.path.join(d, os.path.basename(out)), os.path.join(d, name)
        if not os.path.exists(src):
            continue
        if os.path.exists(dst):
            raise SubgenError(f"{name} already exists - left subgen's {os.path.basename(out)} alone")
        try:
            os.rename(src, dst)
            return dst
        except OSError as e:
            errors.append(f"{d}: {e.strerror or e}")
    raise SubgenError(f"couldn't rename {os.path.basename(out)} to {name} ({'; '.join(errors) or 'not found'}) - "
                      "Anotherr needs a read/write path for the media folder (same as retagging)")


# ====================================================================== worker

STATUS: dict = {"state": "idle", "action_id": None, "file": None, "since": None, "subgen": None, "error": None,
                "in_flight": False}
_wake = threading.Event()
_thread: threading.Thread | None = None
_start_lock = threading.Lock()


def paused() -> bool:
    return bool(settings.state_get("subgen_paused", False))


def set_paused(v: bool):
    settings.state_set("subgen_paused", bool(v))
    if not v:
        kick()


def in_flight() -> bool:
    return bool(STATUS.get("in_flight"))


def kick():
    """Start the single worker thread (or wake it)."""
    global _thread
    with _start_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="anotherr-subgen", daemon=True)
            _thread.start()
    _wake.set()


def approve(action_id: int) -> dict:
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not configured():
        return {"ok": False, "error": "subgen.url is not set (Settings -> Subgen)"}
    p = db.unj(a["payload"], {}) or {}
    p["approved_at"] = time.time()
    for e in p.get("files", []):
        if e.get("status") not in ("done", "failed"):
            e["status"] = "waiting"
    n = db.n("UPDATE actions SET status='queued', payload=?, result=?, updated_at=? WHERE id=? AND status IN ('proposed','failed')",
             (db.j(p), f"queued: 0 of {len(p.get('files', []))} done", time.time(), action_id))
    if not n:
        return {"ok": False, "error": "action not runnable"}
    db.log(f"subgen batch {action_id} approved ({len(p.get('files', []))} file(s)) - they run one at a time")
    kick()
    return {"ok": True, "result": f"queued {len(p.get('files', []))} file(s); subgen gets them one at a time"}


def control(action_id: int, op: str) -> dict:
    """pause | resume | cancel one batch."""
    a = db.q1("SELECT * FROM actions WHERE id=? AND kind=?", (action_id, KIND))
    if not a:
        return {"ok": False, "error": "not a subgen action"}
    p = db.unj(a["payload"], {}) or {}
    if op == "pause":
        p["paused"] = True
    elif op == "resume":
        p.pop("paused", None)
        p.pop("cancel", None)
        p.pop("stopped", None)
        for e in p.get("files", []):
            if e.get("status") == "not attempted":
                e["status"], e["outcome"] = "waiting", ""
        status = "queued" if any(e.get("status") in ACTIVE_FILE for e in p.get("files", [])) else a["status"]
        db.x("UPDATE actions SET status=?, payload=?, updated_at=? WHERE id=?", (status, db.j(p), time.time(), action_id))
        kick()
        return {"ok": True}
    elif op == "cancel":
        p["cancel"] = True
        if STATUS.get("action_id") != action_id:  # not the one being worked on: settle it here
            for e in p.get("files", []):
                if e.get("status") in ACTIVE_FILE:
                    e["status"], e["outcome"] = "not attempted", "cancelled"
            db.x("UPDATE actions SET status='failed', result=?, payload=?, updated_at=? WHERE id=?",
                 (_progress(p, "Cancelled"), db.j(p), time.time(), action_id))
            return {"ok": True}
    else:
        return {"ok": False, "error": "op must be pause | resume | cancel"}
    db.x("UPDATE actions SET payload=?, updated_at=? WHERE id=?", (db.j(p), time.time(), action_id))
    _wake.set()
    return {"ok": True}


def _fmt(sec: float) -> str:
    return f"{sec / 3600:.1f} h" if sec >= 5400 else f"{max(1, round(sec / 60))} min"


def _ratios() -> list[float]:
    return settings.state_get("subgen_ratios", []) or []


def _progress(p: dict, head: str = "") -> str:
    fs = p.get("files", [])
    done = sum(e.get("status") == "done" for e in fs)
    failed = sum(e.get("status") == "failed" for e in fs)
    cur = next((e for e in fs if e.get("status") in ("submitted", "running")), None)
    nxt = next((e for e in fs if e.get("status") == "waiting"), None)
    parts = [f"{done} of {len(fs)} done" + (f", {failed} failed" if failed else "")]
    if cur:
        since = cur.get("started_at") or cur.get("submitted_at") or time.time()
        parts.append(f"now {cur['label']} ({_fmt(time.time() - since)})")
    if nxt:
        parts.append(f"next {nxt['label']}")
    left = [e for e in fs if e.get("status") in ACTIVE_FILE]
    r = _ratios()
    if left and r:
        med = statistics.median(r)
        parts.append("about " + _fmt(sum(med * (e.get("duration") or 1500) for e in left)) + " left")
    return (head + ": " if head else "") + "; ".join(parts)


def _save(action_id: int, p: dict, result: str | None = None, status: str | None = None):
    cur = _reload(action_id)  # keep pause/cancel flags set from the UI meanwhile
    for k in ("paused", "cancel"):
        if cur.get(k) and not p.get(k):
            p[k] = cur[k]
    sql, args = "UPDATE actions SET payload=?, updated_at=?", [db.j(p), time.time()]
    if result is not None:
        sql, args = sql + ", result=?", args + [result]
    if status:
        sql, args = sql + ", status=?", args + [status]
    db.x(sql + " WHERE id=?", args + [action_id])


def _reload(action_id: int) -> dict:
    """Pick up pause/cancel flags set from the UI while keeping the worker's file states."""
    a = db.q1("SELECT payload FROM actions WHERE id=?", (action_id,))
    return db.unj(a["payload"], {}) if a else {}


def _next_action() -> dict | None:
    rows = db.q("SELECT * FROM actions WHERE kind=? AND status IN ('running','queued')", (KIND,))
    rows = [(r, db.unj(r["payload"], {}) or {}) for r in rows]
    rows = [(r, p) for r, p in rows if not p.get("paused")]
    rows.sort(key=lambda rp: (rp[0]["status"] != "running", rp[1].get("approved_at") or rp[0]["updated_at"]))
    return rows[0][0] if rows else None


def _loop():
    while True:
        _wake.clear()
        try:
            a = None if paused() else _next_action()
            if a:
                run_action(a["id"])
                continue
            STATUS.update(state="paused" if paused() else "idle", action_id=None, file=None, in_flight=False)
        except Exception as e:  # noqa: BLE001
            log.exception("subgen worker failed")
            STATUS.update(state="error", error=str(e), in_flight=False)
            db.log(f"subgen worker error: {e}", "error")
            time.sleep(30)
        _wake.wait(300)


def _sleep(sec: float):
    _wake.wait(sec)
    _wake.clear()


def run_action(action_id: int):
    from . import trackedit

    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    p = db.unj(a["payload"], {}) or {}
    item = db.q1("SELECT * FROM items WHERE id=?", (p.get("item_id"),))
    db.x("UPDATE actions SET status='running', updated_at=? WHERE id=?", (time.time(), action_id))
    STATUS.update(state="working", action_id=action_id, error=None)
    fails_in_row, since_refresh = 0, 0
    max_fails = int(cfg().get("max_consecutive_failures", 3))
    while True:
        flags = _reload(action_id)
        if flags.get("cancel") or flags.get("paused") or paused():
            break
        e = next((x for x in p["files"] if x.get("status") in ("submitted", "running")), None) or \
            next((x for x in p["files"] if x.get("status") == "waiting"), None)
        if not e:
            break
        STATUS.update(file=e["label"], since=time.time())
        t0 = time.time()
        try:
            process_file(action_id, p, e, item)
        except _Interrupted:
            break
        except Exception as ex:  # noqa: BLE001
            log.exception("subgen file failed")
            e.update(status="failed", outcome=f"crashed: {ex}")
        took = round(time.time() - t0)
        e["took_s"] = took
        _save(action_id, p, _progress(p))
        label = f"{item['title'] if item else ''} {e['label']}".strip()
        if e["status"] == "done":
            fails_in_row = 0
            since_refresh += 1
            db.log(f"subgen: {label} done in {_fmt(took)} - {e.get('outcome', '')}")
        elif e["status"] == "failed":
            fails_in_row += 1
            db.log(f"subgen: {label} failed after {_fmt(took)} - {e.get('outcome', '')}", "warning")
        if e["status"] in ("done", "failed"):
            db.x("INSERT OR IGNORE INTO queue(file_id, profile, source, created_at) VALUES (?,?,?,?)",
                 (e["file_id"], "standard", "subgen", time.time()))
        if item and since_refresh >= int(cfg().get("refresh_every", 10)):
            trackedit.refresh_apps(item, bazarr=True)
            since_refresh = 0
        if fails_in_row >= max_fails:
            for x in p["files"]:
                if x.get("status") == "waiting":
                    x.update(status="not attempted", outcome=f"stopped after {max_fails} failures in a row")
            p["stopped"] = f"stopped after {max_fails} failures in a row - check the reasons, then Resume"
            db.log(f"subgen batch {action_id}: {p['stopped']}", "warning")
            break
        if e["status"] == "done" and not settings.state_get("subgen_pilot_done", False):
            settings.state_set("subgen_pilot_done", True)
            p["paused"] = True
            p["pilot"] = (f"Pilot: check {e.get('srt') or 'the new subtitle'} in Plex and Bazarr, then press Resume "
                          "to run the rest")
            db.log(f"subgen batch {action_id}: first file done - paused for a check ({e.get('srt')})")
            break
        _sleep(float(cfg().get("pause_between_s", 5)))
    flags = _reload(action_id)
    if flags.get("cancel"):
        for x in p["files"]:
            if x.get("status") in ACTIVE_FILE:
                x.update(status="not attempted", outcome="cancelled")
        p["cancel"] = True
    for k in ("paused",):
        if flags.get(k) and not p.get(k):
            p[k] = flags[k]
    left = any(x.get("status") in ACTIVE_FILE for x in p["files"])
    if p.get("cancel"):
        status, head = "failed", "Cancelled"
    elif p.get("stopped"):
        status, head = "failed", p["stopped"]
    elif left:
        status, head = "queued", p.get("pilot") or ("Paused" if p.get("paused") or paused() else "")
    else:
        failed = any(x.get("status") == "failed" for x in p["files"])
        status, head = ("failed" if failed else "done"), "Finished"
        if item:
            trackedit.refresh_apps(item, bazarr=True)
    _save(action_id, p, _progress(p, head), status)
    STATUS.update(state="idle", action_id=None, file=None, in_flight=False)


class _Interrupted(Exception):
    pass


def _budget(e: dict) -> float:
    c = cfg()
    dur = float(e.get("duration") or 1500)
    factor = float(c.get("timeout_factor", 4))
    r = _ratios()
    if len(r) >= 3:
        factor = max(factor, 2 * statistics.median(r))
    return max(float(c.get("timeout_min_s", 600)), dur * factor)


def _wait_turn(action_id: int, client: Client, path: str) -> dict:
    """Block until subgen is idle (or already has our file). Returns the last /queue snapshot."""
    from . import scheduler, stt

    poll = float(cfg().get("poll_s", 15))
    unreachable_since = None
    while True:
        flags = _reload(action_id)
        if flags.get("cancel") or flags.get("paused") or paused():
            raise _Interrupted()
        try:
            q = client.queue()
            unreachable_since = None
            settings.state_set("subgen_info", {k: q.get(k) for k in ("version", "model", "compute_type")})
            STATUS["subgen"] = {k: q.get(k) for k in ("idle", "queued_count", "processing_count", "version", "model")}
        except SubgenError as ex:
            unreachable_since = unreachable_since or time.time()
            STATUS["error"] = str(ex)
            if time.time() - unreachable_since > float(cfg().get("unreachable_fail_s", 900)):
                raise
            q = None
        if q is not None:
            if path in _paths(q, "queued") | _paths(q, "processing"):
                return q
            if q.get("idle"):
                if cfg().get("gpu"):
                    STATUS["in_flight"] = True  # stops new Anotherr scans (scheduler pause reason)
                    w = scheduler.WORKER
                    if w is not None and w.active:
                        STATUS["state"] = "waiting for Anotherr's scans to finish (GPU)"
                        _heartbeat(action_id, "waiting for Anotherr's scans to finish before using the GPU")
                        _sleep(poll)
                        continue
                    stt.WHISPER.unload()
                return q
            STATUS["state"] = "waiting for subgen (busy with another job)"
        _heartbeat(action_id, STATUS.get("state") if q is not None else f"subgen unreachable: {STATUS.get('error')}")
        _sleep(poll)


def _heartbeat(action_id: int, note: str | None = None):
    db.x("UPDATE actions SET updated_at=? WHERE id=?", (time.time(), action_id))
    if note:
        STATUS["note"] = note


def process_file(action_id: int, p: dict, e: dict, item: dict):
    from .scanner import resolve_path

    client = Client()
    f = db.q1("SELECT * FROM files WHERE id=?", (e["file_id"],))
    if not f or f["missing"]:
        e.update(status="failed", outcome="no longer in the library")
        return
    local = resolve_path(f)
    if not local:
        e.update(status="failed", outcome="file not found on disk")
        return
    path = e.get("path") or subgen_path(f)
    stem = os.path.splitext(local)[0]
    lang1 = langs.ISO1.get(_sub_lang(), "en")
    if e.get("status") == "waiting" and os.path.exists(f"{stem}.{lang1}.srt"):
        e.update(status="done", outcome=f"an {langs.name(_sub_lang())} subtitle already exists ({os.path.basename(stem)}.{lang1}.srt)")
        return
    poll = float(cfg().get("poll_s", 15))
    while True:  # one retry when subgen loses the job
        q = _wait_turn(action_id, client, path)
        STATUS["in_flight"] = True
        if path not in _paths(q, "queued") | _paths(q, "processing"):
            if e.get("status") in ("submitted", "running") and e.get("submitted_at"):
                out = find_output(local, e["submitted_at"])  # finished while Anotherr was down
                if out:
                    return _read_back(f, e, local, out)
                if e.get("retried"):
                    e.update(status="failed", outcome="failed (lost): subgen no longer has the job and wrote no "
                                                      "subtitle - already retried once")
                    return
                e.update(retried=True, outcome="lost (restart) - retrying once")
                db.log(f"subgen: {e['label']} was lost from subgen's queue (restart?) - retrying once", "warning")
            code, body = client.submit(path, e["language"], e["task"])
            if code == 403:
                e.update(status="failed", outcome=f"subgen refused the path {path}: {body.get('error') or body} "
                                                  "(check subgen.path_map and SUBGEN_PATH_ALLOWLIST)")
                return
            if code == 404:
                e.update(status="failed", outcome=f"subgen found no file at {path} (check subgen.path_map)")
                return
            if code != 200:
                e.update(status="failed", outcome=f"subgen /batch -> {code}: {str(body)[:200]}")
                return
            if not (body.get("queued") or body.get("already_in_queue")):
                why = [k for k in ("skipped", "no_audio", "pending_language_detect") if body.get(k)]
                e.update(status="failed", outcome=f"subgen didn't queue it ({', '.join(why) or body})")
                return
            e.update(status="submitted", submitted_at=time.time(), started_at=None)
            _save(action_id, p, _progress(p))
            db.log(f"subgen: submitted {item['title'] if item else ''} {e['label']} ({e['task']} {e['language']})")
        else:
            e.setdefault("submitted_at", time.time())
            e["status"] = e.get("status") if e.get("status") in ("submitted", "running") else "submitted"
        budget, worked = _budget(e), float(e.get("worked_s") or 0)
        last = time.time()
        gone = False
        while True:
            _sleep(poll)
            flags = _reload(action_id)
            now = time.time()
            try:
                q = client.queue()
            except SubgenError as ex:
                STATUS["error"] = str(ex)
                _heartbeat(action_id, f"subgen unreachable: {ex}")
                last = now
                continue
            if flags.get("cancel"):
                r = client.cancel(path)
                e.update(status="failed", outcome="cancelled" + ("" if r.get("cancelled") else
                                                                 " (subgen can't stop a running job; it finishes on its own)"))
                raise _Interrupted()
            processing, queued = path in _paths(q, "processing"), path in _paths(q, "queued")
            if processing:
                worked += now - last
                if e["status"] != "running":
                    e.update(status="running", started_at=now)
                    _save(action_id, p, _progress(p))
            last = now
            e["worked_s"] = round(worked)
            _heartbeat(action_id)
            db.x("UPDATE actions SET result=? WHERE id=?", (_progress(p), action_id))
            if not processing and not queued:
                gone = True
                break
            if worked > budget:
                r = client.cancel(path)
                e.update(status="failed", outcome=f"timeout after {_fmt(worked)} (budget {_fmt(budget)})" +
                         ("" if r.get("cancelled") else
                          " - subgen can't stop a running job, so the next file waits until it finishes"))
                return
        if gone:
            out = find_output(local, e["submitted_at"])
            if out:
                return _read_back(f, e, local, out)
            if e.get("retried"):
                e.update(status="failed", outcome="failed (lost): subgen finished or dropped the job without "
                                                  "writing a subtitle - twice (see subgen's log)")
                return
            e.update(retried=True, status="waiting", outcome="lost once - retrying", worked_s=0)
            db.log(f"subgen: {e['label']} vanished from subgen's queue without a subtitle - retrying once", "warning")


def _read_back(f: dict, e: dict, local: str, out: str):
    dur = float(f.get("duration") or e.get("duration") or 0)
    ok, ev = check_output(out, dur, e["lang3"])
    e["check"] = ev
    if not ok:
        e.update(status="failed", outcome=f"result rejected: {ev['why']} (left as {os.path.basename(out)})")
        return
    try:
        final = finalize(f, local, out)
    except SubgenError as ex:
        e.update(status="failed", outcome=str(ex))
        return
    took = (time.time() - (e.get("started_at") or e["submitted_at"]))
    if dur:
        r = (_ratios() + [round(took / dur, 2)])[-5:]
        settings.state_set("subgen_ratios", r)
    info = settings.state_get("subgen_info") or {}
    e.update(status="done", srt=os.path.basename(final), machine_generated=True,
             model=info.get("model"), backend=e.get("backend"),
             outcome=f"{os.path.basename(final)}: {ev['cues']} cues, {ev['per_min']}/min, English score {ev['english_score']}")
