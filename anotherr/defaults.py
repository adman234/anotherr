"""set_default_audio / set_default_subtitle: propose making the right track the file's default.

Rules (Nov 5 spec):
  audio     - open default_audio_not_preferred; a track in the preferred language (override, else original) that
              was analysed, is confident and agreed in every window; not commentary/description. Pick the one with
              the most channels, then the lowest index. Never promote a track on its tag alone.
  subtitle  - open default_subs_signs; judged against the default audio AFTER any audio fix:
                default audio in your subtitle language -> no proposal (none, or the forced track, is right)
                default audio foreign                   -> the full embedded track in your subtitle language:
                                                           text over image, non-SDH over SDH, lowest index
              External subtitle files can't carry flags: no proposal.
A file needing both gets ONE proposal (one header write). Proposals are grouped per series + season + identical
change, approved manually, and executed by trackedit (which also clears the other defaults of the same type).
"""
from __future__ import annotations

import re
import time

from . import db, langs, settings, trackedit

AUDIO, SUBS = "set_default_audio", "set_default_subtitle"
BLOCKING = {"wrong_content", "truncated", "unreadable", "duration_short", "duration_short_warn", "double_length",
            "file_not_found", "decode_errors_severe", "decode_stopped_early"}
_NOT_MAIN = re.compile(r"comment|descri|\bad\b|narrat", re.I)


def _same(a, b) -> bool:
    from .scanner import lang_match

    return lang_match(a or "", b or "")


FIX_CODES = {"default_audio_not_preferred", "default_subs_signs", "default_subs_foreign"}


def current_default_audio(tracks: list[dict]) -> dict | None:
    """The track players start with: the flagged default, or the first track when none is flagged."""
    if not tracks:
        return None
    return next((t for t in tracks if t.get("default")), tracks[0])


def pick_audio(tracks: list[dict], pref: str) -> dict | None:
    cands = [t for t in tracks if _same(t.get("effective"), pref) and t.get("confident") and t.get("all_agree")
             and t.get("windows") and not t.get("commentary") and not _NOT_MAIN.search(t.get("title") or "")]
    return sorted(cands, key=lambda t: (-(t.get("channels") or 0), t["a_index"]))[0] if cands else None


def pick_full_sub(subs: list[dict], sub_lang: str) -> dict | None:
    cands = [s for s in subs if not s.get("external") and s.get("s_index") is not None and _same(s.get("lang"), sub_lang)
             and s.get("kind") == "full" and not s.get("forced")]
    return sorted(cands, key=lambda s: (not s.get("text"), bool(s.get("hearing_impaired")), s["s_index"]))[0] if cands else None


def plan_file(tracks: list[dict], subs: list[dict], codes: set[str], pref: str, sub_lang: str,
              fallback: str = "", subs_all: list[dict] | None = None) -> dict:
    """What a file needs. Returns {"ops": [...], "audio": {...}|None, "sub": {...}|None, "notes": [...]}.
    fallback: the language that should play when the file has no track in the preferred one (English)."""
    out = {"ops": [], "audio": None, "sub": None, "notes": []}
    cur = current_default_audio(tracks)
    final_lang = (cur or {}).get("effective")
    if fallback and pref and not any(_same(t.get("effective"), pref) for t in tracks):
        pref = fallback
    if "default_audio_not_preferred" in codes and pref and cur and (cur.get("commentary")
                                                                  or not _same(cur.get("effective"), pref)):
        tgt = pick_audio(tracks, pref)
        if tgt:
            out["ops"].append({"type": "a", "index": tgt["a_index"], "default": True, "expect_lang": tgt.get("tag") or ""})
            out["audio"] = {"to": tgt["a_index"], "to_lang": pref, "from": cur["a_index"], "from_lang": cur.get("effective"),
                            "channels": tgt.get("channels")}
            final_lang = pref
        else:
            out["notes"].append(f"no confidently detected {langs.name(pref)} track - a flag change can't fix this")
    if "default_subs_signs" in codes:
        if _same(final_lang, sub_lang):
            out["notes"].append("default audio is in your subtitle language - no default subtitle change")
        else:
            tgt = pick_full_sub(subs, sub_lang)
            dflt = next((s for s in subs if s.get("default") and not s.get("external")), None)
            if tgt and (not dflt or dflt["s_index"] != tgt["s_index"]):
                out["ops"].append({"type": "s", "index": tgt["s_index"], "default": True})
                out["sub"] = {"to": tgt["s_index"], "title": tgt.get("title") or tgt.get("codec"),
                              "image": not tgt.get("text"), "sdh": bool(tgt.get("hearing_impaired")),
                              "from": dflt["s_index"] if dflt else None}
            elif not tgt and any(s.get("external") and s.get("kind") == "full" and _same(s.get("lang"), sub_lang) for s in subs):
                out["notes"].append("cannot change: the full subtitle track is an external file")
            elif not tgt:
                out["notes"].append(f"no full embedded {langs.name(sub_lang)} subtitle track")
    if "default_subs_foreign" in codes and not out["sub"]:
        foreign = next((s for s in subs_all or [] if s.get("default") and s.get("lang")
                        and not _same(s["lang"], sub_lang) and not _same(s["lang"], pref)), None)
        tgt = None if _same(final_lang, sub_lang) else pick_full_sub(subs, sub_lang)
        if foreign and tgt:
            out["ops"].append({"type": "s", "index": tgt["s_index"], "default": True})
            out["sub"] = {"to": tgt["s_index"], "title": tgt.get("title") or tgt.get("codec"),
                          "image": not tgt.get("text"), "sdh": bool(tgt.get("hearing_impaired")),
                          "from": foreign["s_index"], "from_lang": foreign["lang"]}
        elif foreign:
            # the audio is already in your language (or there is no full track to move to): just drop the flag
            out["ops"].append({"type": "s", "index": foreign["s_index"], "default": False})
            out["sub"] = {"to": None, "clear": foreign["s_index"], "from": foreign["s_index"],
                          "from_lang": foreign["lang"], "title": "", "image": False, "sdh": False}
    return out


def fix_text_audio(tracks: list[dict], pref: str) -> str:
    """'Default audio track 3 is German; English is track 1. Fix available: make track 1 the default.'"""
    cur = current_default_audio(tracks)
    tgt = pick_audio(tracks, pref)
    if not cur:
        return ""
    head = f"Default audio track {cur['a_index'] + 1} is {langs.name(cur.get('effective'))}"
    if tgt:
        return f"{head}; {langs.name(pref)} is track {tgt['a_index'] + 1}. Fix available: make track {tgt['a_index'] + 1} the default."
    return f"{head}, not {langs.name(pref)}, and no confidently detected {langs.name(pref)} track exists (a flag change can't fix it)."


# ====================================================================== proposals

def _signature(plan: dict) -> str:
    parts = []
    if plan["audio"]:
        parts.append(f"audio {langs.name(plan['audio']['to_lang'])} <- {langs.name(plan['audio']['from_lang'])}")
    if plan["sub"] and plan["sub"].get("clear") is not None:
        parts.append(f"subtitle clear {langs.name(plan['sub']['from_lang'])}")
    elif plan["sub"]:
        parts.append("subtitle full" + (" (image)" if plan["sub"]["image"] else ""))
    return " + ".join(parts)


def _reason(item: dict, season, plans: list[dict]) -> str:
    where = item["title"] + (f" Season {season}" if season is not None else "")
    p0, n = plans[0]["plan"], len(plans)
    bits = []
    if p0["audio"]:
        tos = sorted({p["plan"]["audio"]["to"] + 1 for p in plans})
        froms = sorted({p["plan"]["audio"]["from"] + 1 for p in plans})
        bits.append(f"make audio track {'/'.join(map(str, tos))} ({langs.name(p0['audio']['to_lang'])}) the default on "
                    f"{n} file(s). Was {langs.name(p0['audio']['from_lang'])} (track {'/'.join(map(str, froms))})")
    if p0["sub"] and p0["sub"].get("clear") is not None:
        idx = sorted({p["plan"]["sub"]["clear"] + 1 for p in plans})
        bits.append(f"{'and ' if bits else ''}clear the default flag on the {langs.name(p0['sub']['from_lang'])} "
                    f"subtitle track s{'/s'.join(map(str, idx))}{'' if bits else f' on {n} file(s)'}")
    elif p0["sub"]:
        was = (f"the {langs.name(p0['sub']['from_lang'])} track" if p0["sub"].get("from_lang")
               else "the signs-only track")
        tos = sorted({p["plan"]["sub"]["to"] + 1 for p in plans})
        extra = " - image-based, needs burn-in on some clients" if p0["sub"]["image"] else ""
        bits.append(f"{'and ' if bits else ''}make the full subtitle track s{'/s'.join(map(str, tos))} the default"
                    f"{'' if bits else f' on {n} file(s)'}, instead of {was}{extra}")
    return f"{where}: " + "; ".join(bits) + ". Other defaults of the same type are cleared; forced flags are kept."


def refresh_item(item_id: int) -> dict:
    from . import retag

    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        return {}
    cfg = settings.get()
    pref = langs.norm(item.get("language_override")) or langs.norm(item.get("original_language"))
    sub_lang = langs.norm(cfg["languages"]["subtitle_language"]) or "eng"
    groups: dict[tuple, list] = {}
    busy = set()
    for a in db.q("SELECT payload FROM actions WHERE kind IN (?, ?) AND status IN ('running','done') AND updated_at > ?",
                  (AUDIO, SUBS, time.time() - 7 * 86400)):
        for e in (db.unj(a["payload"], {}) or {}).get("files", []):
            if e.get("status") == "edited" and not e.get("verified"):
                busy.add(e["file_id"])
    holds = trackedit.edit_holds()
    for f in db.q("SELECT * FROM files WHERE item_id=? AND missing=0", (item_id,)):
        codes = {r["code"] for r in db.q("SELECT code FROM issues WHERE file_id=? AND status='open'", (f["id"],))}
        if not codes & FIX_CODES or codes & BLOCKING or f["id"] in busy:
            continue
        ev = retag._evidence(f) or {}
        if trackedit.held(holds, f["id"], ev.get("scan_started")):
            continue  # an edit already ran (or is running) since this evidence was gathered
        plan = plan_file(ev.get("tracks") or [], ev.get("subs") or [], codes, pref, sub_lang, fallback="eng",
                         subs_all=ev.get("subs_all"))
        if not plan["ops"]:
            continue
        kind = AUDIO if plan["audio"] else SUBS
        key = (kind, retag._season(f), _signature(plan))
        groups.setdefault(key, []).append({"file": f, "plan": plan, "scan_started": ev.get("scan_started"),
                                           "codes": codes})
    existing = {}
    for a in db.q("SELECT * FROM actions WHERE kind IN (?, ?) AND status='proposed'", (AUDIO, SUBS)):
        p = db.unj(a["payload"], {}) or {}
        if p.get("item_id") == item_id:
            existing[tuple(p.get("key") or ())] = a
    now = time.time()
    made = {"created": 0, "updated": 0, "withdrawn": 0}
    for key, grp in groups.items():
        kind, season, sig = key
        grp.sort(key=lambda g: g["file"].get("label") or "")
        files = []
        for g in grp:
            f = g["file"]
            files.append({"file_id": f["id"], "label": f.get("label") or "", "ops": g["plan"]["ops"],
                          "audio": g["plan"]["audio"], "sub": g["plan"]["sub"], "notes": g["plan"]["notes"],
                          "scan_started": g["scan_started"], "status": "pending",
                          "why": _signature(g["plan"]), "conf": 1.0,
                          **trackedit.preview(f.get("local_path") or f["arr_path"], g["plan"]["ops"], cfg)})
        payload = {"item_id": item_id, "key": list(key), "family": "defaults", "season": season, "files": files,
                   "series_language": pref}
        reason = _reason(item, season, grp)
        sub_codes = sorted({c for g in grp for c in g["codes"] & {"default_subs_signs", "default_subs_foreign"}})
        codes = ["default_audio_not_preferred"] if kind == AUDIO else sub_codes or ["default_subs_signs"]
        a = existing.pop(tuple(key), None)
        if a:
            db.x("UPDATE actions SET reason=?, payload=?, file_id=?, updated_at=? WHERE id=?",
                 (reason, db.j(payload), grp[0]["file"]["id"], now, a["id"]))
            made["updated"] += 1
        else:
            db.x("INSERT INTO actions(file_id, kind, reason, codes, status, auto, payload, created_at, updated_at) "
                 "VALUES (?,?,?,?, 'proposed', 0, ?, ?, ?)", (grp[0]["file"]["id"], kind, reason, db.j(codes),
                                                              db.j(payload), now, now))
            made["created"] += 1
    for a in existing.values():
        db.x("UPDATE actions SET status='withdrawn', result='Withdrawn: no files in this group need the change anymore', "
             "updated_at=? WHERE id=?", (now, a["id"]))
        made["withdrawn"] += 1
    return made


def refresh_all() -> int:
    ids = {r["item_id"] for r in db.q(
        "SELECT DISTINCT f.item_id FROM issues x JOIN files f ON f.id=x.file_id WHERE x.status='open' AND f.missing=0 "
        "AND x.code IN ('default_audio_not_preferred','default_subs_signs','default_subs_foreign')")}
    ids |= {(db.unj(a["payload"], {}) or {}).get("item_id") for a in db.q(
        "SELECT payload FROM actions WHERE kind IN (?, ?) AND status='proposed'", (AUDIO, SUBS))}
    for i in ids - {None}:
        try:
            refresh_item(i)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("anotherr.defaults").exception("default-track refresh failed for item %s", i)
    return len(ids)
