"""retag_audio: write a detected audio language into the file's track tag (with approval).

Two cases:
  untagged  - track has no tag (or `und`); detection unanimous and confident
  wrong_tag - track is tagged, but every window says another language, which is the series' own language

Proposals are batched per series + season (+ case). Nothing is ever written automatically; editing needs
`retag.enabled`, a writable media mount, and the file under one of `retag.allowed_paths`.
Matroska/WebM files are edited in place with mkvpropedit (header only); anything else, or hardlinked files
when `break_hardlinks` is on, goes through an ffmpeg stream copy that atomically replaces the original.
Every change is logged first (rollback record) and verified with ffprobe afterwards; undo restores old tags.
"""
from __future__ import annotations

import logging
import statistics
import time

from . import db, langs, settings, trackedit

log = logging.getLogger("anotherr.retag")

KIND = "retag_audio"
UNTAGGED, WRONG = "untagged", "wrong_tag"
# a file that's about to be replaced (or can't be trusted) is not worth editing
BLOCKING = {"wrong_content", "truncated", "unreadable", "duration_short", "duration_short_warn", "double_length",
            "no_dialogue", "file_not_found", "decode_errors_severe", "decode_stopped_early"}


# ====================================================================== evidence & rules

def _assess(windows: list[dict]) -> dict:
    langs_ = [langs.norm(w.get("lang")) for w in windows]
    probs = [float(w.get("prob") or 0) for w in windows]
    if not langs_:
        return {"det": "", "n": 0, "agree": 0, "all_agree": False, "conf": 0.0}
    det = max(set(langs_), key=langs_.count)
    agree = langs_.count(det)
    conf = statistics.mean(p for l, p in zip(langs_, probs) if l == det)
    return {"det": det, "n": len(langs_), "agree": agree, "all_agree": agree == len(langs_), "conf": round(conf, 3)}


def _evidence(f: dict) -> dict | None:
    ev = db.unj(f.get("audio_evidence"))
    if ev:
        return ev
    # scans from before the evidence column existed
    if not f.get("last_scan_id"):
        return None
    s = db.q1("SELECT started_at, result FROM scans WHERE id=?", (f["last_scan_id"],))
    res = db.unj(s["result"], {}) if s else {}
    audio = (res.get("streams") or {}).get("audio") or []
    by_idx = {t["a_index"]: t for t in res.get("audio_tracks") or []}
    if not audio:
        return None
    return {"scan_started": s["started_at"], "tracks": [
        {"a_index": a["a_index"], "tag": a.get("lang", ""), "title": a.get("title", ""),
         "commentary": bool(a.get("commentary")), "forced": bool(a.get("forced")),
         "hi": bool(a.get("hearing_impaired")), "speech_s": by_idx.get(a["a_index"], {}).get("speech_s"),
         "windows": by_idx.get(a["a_index"], {}).get("windows", [])} for a in audio]}


def _season(f: dict) -> int | None:
    eps = db.unj(f.get("episodes"), []) or []
    s = [e.get("season") for e in eps if e.get("season") is not None]
    return min(s) if s else None


def strong_untagged(a: dict) -> bool:
    return a["n"] >= 2 and a["all_agree"] and a["conf"] >= 0.90


def strong_wrong(a: dict) -> bool:
    return a["n"] >= 3 and a["all_agree"] and a["conf"] >= 0.95


def assess_item(item: dict) -> list[dict]:
    """Every track of the series that qualifies for a retag, with the reasons."""
    orig = langs.norm(item.get("language_override")) or langs.norm(item.get("original_language"))
    files = db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item["id"],))
    info = {}
    for f in files:
        ev = _evidence(f)
        if not ev:
            continue
        codes = {r["code"]: r["status"] for r in db.q("SELECT code, status FROM issues WHERE file_id=? AND "
                                                       "status IN ('open','ignored')", (f["id"],))}
        info[f["id"]] = {"file": f, "ev": ev, "codes": codes, "season": _season(f),
                         "group": f.get("release_group") or "",
                         "tracks": {t["a_index"]: dict(t, a=_assess(t.get("windows") or [])) for t in ev["tracks"]}}

    def siblings(fid: int, a_index: int, det: str) -> list[str]:
        me = info[fid]
        out = []
        for oid, o in info.items():
            if oid == fid or o["season"] != me["season"]:
                continue
            if me["group"] and o["group"] and me["group"] != o["group"]:
                continue  # another release
            t = o["tracks"].get(a_index)
            if t and t["a"]["det"] == det and strong_untagged(t["a"]):
                out.append(o["file"].get("label") or str(oid))
        return out

    out = []
    for fid, x in info.items():
        if set(x["codes"]) & BLOCKING and any(x["codes"][c] == "open" for c in set(x["codes"]) & BLOCKING):
            continue
        rows = []
        for idx, t in x["tracks"].items():
            a = t["a"]
            if t.get("commentary") or t.get("forced") or t.get("hi"):
                continue
            if (t.get("speech_s") or 0) < 10 or not a["det"] or a["det"] not in langs.NAMES:
                continue
            tag = langs.norm(t.get("tag"))
            if not tag:
                if x["codes"].get("audio_untagged") == "ignored":
                    continue
                sib = siblings(fid, idx, a["det"])
                if strong_untagged(a):
                    why = f"{a['agree']}/{a['n']} windows"
                elif a["agree"] * 2 > a["n"] and len(sib) >= 2:
                    why = f"{a['agree']}/{a['n']} windows, confirmed by {len(sib)} other files of the release"
                else:
                    continue
                rows.append({"case": UNTAGGED, "a_index": idx, "old": "", "new": a["det"], **a, "why": why,
                             "siblings": sib[:5], "mismatch": bool(orig) and a["det"] != orig})
            elif tag != a["det"]:
                if x["codes"].get("audio_mislabeled") == "ignored" or not orig or a["det"] != orig:
                    continue  # a wrong tag is only corrected toward the series' own language
                sib = siblings(fid, idx, a["det"])
                if strong_wrong(a) and len(sib) >= 2:
                    rows.append({"case": WRONG, "a_index": idx, "old": tag, "new": a["det"], **a,
                                 "why": f"{a['agree']}/{a['n']} windows at {a['conf']:.0%}, {len(sib)} other files agree",
                                 "siblings": sib[:5], "mismatch": False})
        # a file whose tracks don't match the series' language needs a person to look at the whole file
        review = any(r["mismatch"] for r in rows if r["case"] == UNTAGGED)
        for r in rows:
            r.update(file_id=fid, label=x["file"].get("label") or "", season=x["season"],
                     review=review and r["case"] == UNTAGGED, scan_started=x["ev"].get("scan_started"))
            out.append(r)
    return out


# ====================================================================== proposals

def _ops(row: dict) -> list[dict]:
    return [{"type": "a", "index": row["a_index"], "language": row["new"], "old": row["old"]}]


def _plan(f: dict, row: dict, cfg: dict) -> dict:
    return trackedit.preview(f.get("local_path") or f.get("arr_path"), _ops(row), cfg)


def _reason(item: dict, case: str, season, review: bool, rows: list[dict]) -> str:
    where = f"{item['title']}" + (f" Season {season}" if season is not None else "")
    tracks = sorted({r["a_index"] + 1 for r in rows})
    news = sorted({langs.name(r["new"]) for r in rows})
    files = len({r["file_id"] for r in rows})
    conf = statistics.mean(r["conf"] for r in rows)
    every = all(r["all_agree"] for r in rows)
    if case == WRONG:
        olds = ", ".join(sorted({langs.name(r["old"]) for r in rows}))
        return (f"{where}: set audio track {'/'.join(map(str, tracks))} to {', '.join(news)} on {files} file(s). "
                f"Was tagged {olds}. {', '.join(news)} detected in every window, average confidence {conf:.2f}. "
                "Manual approval only.")
    lead = "Manual review - detected language differs from the series' language. " if review else ""
    return (f"{lead}{where}: set audio track {'/'.join(map(str, tracks))} to {', '.join(news)} on {files} file(s). "
            f"Was untagged. {', '.join(news)} detected in "
            + ("every window" if every else "most windows (confirmed by sibling files)")
            + f", average confidence {conf:.2f}.")


def refresh_item(item_id: int) -> dict:
    """Rebuild this series' retag proposals from the latest evidence; withdraw ones nothing supports anymore."""
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        return {}
    cfg = settings.get()
    rows = assess_item(item)
    # files already edited recently are waiting for their read-back rescan
    busy = set()
    for a in db.q("SELECT payload FROM actions WHERE kind=? AND status IN ('running','done') AND updated_at > ?",
                  (KIND, time.time() - 7 * 86400)):
        for f in (db.unj(a["payload"], {}) or {}).get("files", []):
            if f.get("status") == "edited" and not f.get("verified"):
                busy.add((f["file_id"], f["a_index"]))
    rows = [r for r in rows if (r["file_id"], r["a_index"]) not in busy]
    paths = {f["id"]: f.get("local_path") or f.get("arr_path")
             for f in db.q("SELECT id, local_path, arr_path FROM files WHERE item_id=?", (item_id,))}
    rows = [r for r in rows if trackedit.can_edit(paths.get(r["file_id"]))]  # AVI can't hold a language tag
    holds = trackedit.edit_holds()
    if holds:
        fs = {f["id"]: f for f in db.q("SELECT id, audio_evidence, last_scan_id FROM files WHERE item_id=?", (item_id,))}
        rows = [r for r in rows if not trackedit.held(holds, r["file_id"],
                                                       (_evidence(fs.get(r["file_id"]) or {}) or {}).get("scan_started"))]
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["case"], r["season"], r["review"]), []).append(r)
    existing = {}
    for a in db.q("SELECT * FROM actions WHERE kind=? AND status='proposed'", (KIND,)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == item_id:
            existing[tuple(p.get("key") or ())] = a
    now = time.time()
    made = {"created": 0, "updated": 0, "withdrawn": 0}
    files = {f["id"]: f for f in db.q("SELECT * FROM files WHERE item_id=?", (item_id,))}
    for key, grp in groups.items():
        case, season, review = key
        grp.sort(key=lambda r: (r["label"], r["a_index"]))
        payload = {"item_id": item_id, "key": list(key), "case": case, "season": season, "review": review,
                   "series_language": langs.norm(item.get("language_override")) or langs.norm(item.get("original_language")),
                   "files": [dict(r, **_plan(files[r["file_id"]], r, cfg), ops=_ops(r),
                                  path=files[r["file_id"]].get("local_path"), status="pending") for r in grp]}
        reason = _reason(item, case, season, review, grp)
        codes = ["audio_mislabeled" if case == WRONG else "audio_untagged"]
        a = existing.pop(tuple(key), None) or existing.pop(tuple(list(key)), None)
        if a:
            db.x("UPDATE actions SET reason=?, payload=?, file_id=?, updated_at=? WHERE id=?",
                 (reason, db.j(payload), grp[0]["file_id"], now, a["id"]))
            made["updated"] += 1
        else:
            db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                 "VALUES (?,?,?,?, 'proposed', 0, ?, ?, ?)", (grp[0]["file_id"], KIND, reason, db.j(codes), db.j(payload),
                                                              now, now))
            made["created"] += 1
    for a in existing.values():
        db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: no files in this group qualify anymore', "
             "updated_at=? WHERE id=?", (now, a["id"]))
        made["withdrawn"] += 1
    return made


def refresh_all() -> int:
    ids = [r["item_id"] for r in db.q(
        "SELECT DISTINCT f.item_id FROM issues x JOIN files f ON f.id=x.file_id WHERE x.status IN ('open','ignored') "
        "AND x.code IN ('audio_untagged','audio_mislabeled') AND f.missing=0")]
    ids += [r["item_id"] for r in db.q(
        "SELECT DISTINCT f.item_id FROM actions a JOIN files f ON f.id=a.file_id WHERE a.kind=? AND a.status='proposed'",
        (KIND,))]
    for i in set(ids):
        try:
            refresh_item(i)
        except Exception:  # noqa: BLE001
            log.exception("retag refresh failed for item %s", i)
    return len(set(ids))


# ====================================================================== execution (shared track-edit executor)

def start(action_id: int) -> dict:
    return trackedit.start(action_id)


def _run(action_id: int):
    trackedit.run(action_id)


def edit_one(entry: dict, cfg: dict, action_id: int) -> dict:
    return trackedit.edit_file(entry, cfg, action_id)


def write_path(f: dict, resolved: str) -> str | None:
    return trackedit.write_path(f, resolved)


def after_scan(file_id: int, info: dict, scan_id: int | None = None):
    trackedit.after_scan(file_id, info)


def undo(action_id: int) -> dict:
    return trackedit.undo(action_id)
