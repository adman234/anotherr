"""Read title cards with Tesseract and match them against the series' episode titles."""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import time

from . import arr, db

_cache: dict[int, tuple[float, list[dict]]] = {}


def available() -> bool:
    return shutil.which("tesseract") is not None


def read_text(image: bytes) -> str:
    """OCR one (greyscale) frame with two page modes: 3 = auto layout, 11 = sparse text over artwork.
    Colour frames confuse Tesseract on title cards, so pass media.extract_ocr_frame() output."""
    texts = []
    for psm in ("3", "11"):
        try:
            p = subprocess.run(["tesseract", "stdin", "stdout", "--psm", psm, "-l", "eng"], input=image,
                               capture_output=True, timeout=30)
            texts.append(p.stdout.decode("utf-8", "replace"))
        except (subprocess.TimeoutExpired, OSError):
            pass
    return "\n".join(texts)


def confident_words(image: bytes, min_conf: float = 80) -> list[str]:
    """Words Tesseract is confident about. Textures and edges produce plenty of low-confidence junk
    ("Pane", "aoe", "See eee"); real subtitle text comes back at high confidence."""
    try:
        p = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng", "tsv"], input=image,
                           capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    out = []
    for line in p.stdout.decode("utf-8", "replace").splitlines()[1:]:
        f = line.split("\t")
        if len(f) == 12 and f[11].strip():
            word = f[11].strip().strip(".,!?;:\"'")
            try:
                conf = float(f[10])
            except ValueError:
                continue
            if conf >= min_conf and len(word) >= 3 and word.isalpha():
                out.append(word)
    return out


def looks_like_subtitle(words: list[str]) -> bool:
    return len(words) >= 2 or any(len(w) >= 5 for w in words)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def title_score(title: str, text: str) -> float:
    """Best fuzzy match of the title anywhere in the OCR text (0..1)."""
    t, x = norm(title), norm(text)
    if len(t) < 4 or not x:
        return 0.0
    if t in x:
        return 1.0
    n, best = len(t), 0.0
    for i in range(0, max(1, len(x) - n + 1)):
        for w in (n - 1, n, n + 1):
            r = difflib.SequenceMatcher(None, t, x[i:i + w]).ratio()
            if r > best:
                best = r
    return best


def threshold(title: str) -> float:
    return 0.95 if len(norm(title)) < 8 else 0.85  # short titles must be near-exact


def match(texts: list[str], episodes: list[dict], series_title: str = "") -> list[dict]:
    """Episodes whose title appears in any frame's text.

    The series' own logo is on screen in most episodes, so its name is removed from the OCR text first,
    and an episode whose title is (part of) the series name - e.g. the film "The Clone Wars" (S00E01) for
    "Star Wars: The Clone Wars" - is never matched.
    """
    st = norm(series_title)
    if st:
        texts = [norm(t).replace(st, " ") for t in texts]
    out = []
    for ep in episodes:
        if not ep.get("title"):
            continue
        if st and norm(ep["title"]) in st:
            continue
        best = max(((title_score(ep["title"], tx), i) for i, tx in enumerate(texts)), default=(0.0, -1))
        if best[0] >= threshold(ep["title"]):
            out.append({"id": ep["id"], "season": ep.get("season"), "episode": ep.get("episode"),
                        "title": ep["title"], "score": round(best[0], 3), "frame": best[1]})
    return out


def series_episodes(item: dict) -> list[dict]:
    """All episode titles of the series (Sonarr when available, else what the library holds)."""
    hit = _cache.get(item["id"])
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    eps: list[dict] = []
    s = arr.Sonarr()
    if item.get("source") == "sonarr" and s.configured:
        try:
            eps = [{"id": e["id"], "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
                    "title": e.get("title", "")} for e in s.episodes(item["arr_id"]) or []]
        except arr.ServiceError:
            eps = []
    if not eps:
        seen = {}
        for f in db.q("SELECT episodes FROM files WHERE item_id=?", (item["id"],)):
            for e in db.unj(f["episodes"], []) or []:
                seen[e["id"]] = e
        eps = list(seen.values())
    _cache[item["id"]] = (time.time(), eps)
    return eps
