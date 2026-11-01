"""Language normalisation between Sonarr/Radarr names, ISO 639-1 (Whisper) and ISO 639-2 (stream tags)."""
from __future__ import annotations

# canonical 639-2 code, English name, 639-1 code, aliases
_TABLE = [
    ("eng", "English", "en", ["english"]),
    ("jpn", "Japanese", "ja", ["japanese", "jp"]),
    ("fre", "French", "fr", ["fra", "french"]),
    ("ger", "German", "de", ["deu", "german"]),
    ("spa", "Spanish", "es", ["spanish", "spanish (latino)", "latino", "es-419", "spa-la"]),
    ("ita", "Italian", "it", ["italian"]),
    ("por", "Portuguese", "pt", ["portuguese", "portuguese (brazil)", "pt-br", "brazilian"]),
    ("rus", "Russian", "ru", ["russian"]),
    ("chi", "Chinese", "zh", ["zho", "chinese", "mandarin", "cmn", "zh-cn", "zh-tw", "chs", "cht"]),
    ("yue", "Cantonese", "yue", ["cantonese"]),
    ("kor", "Korean", "ko", ["korean"]),
    ("hin", "Hindi", "hi", ["hindi"]),
    ("ara", "Arabic", "ar", ["arabic"]),
    ("dut", "Dutch", "nl", ["nld", "dutch", "flemish"]),
    ("swe", "Swedish", "sv", ["swedish"]),
    ("nor", "Norwegian", "no", ["nob", "nno", "norwegian", "nb", "nn"]),
    ("dan", "Danish", "da", ["danish"]),
    ("fin", "Finnish", "fi", ["finnish"]),
    ("pol", "Polish", "pl", ["polish"]),
    ("tur", "Turkish", "tr", ["turkish"]),
    ("tha", "Thai", "th", ["thai"]),
    ("vie", "Vietnamese", "vi", ["vietnamese"]),
    ("ind", "Indonesian", "id", ["indonesian"]),
    ("may", "Malay", "ms", ["msa", "malay"]),
    ("heb", "Hebrew", "he", ["hebrew", "iw"]),
    ("gre", "Greek", "el", ["ell", "greek"]),
    ("cze", "Czech", "cs", ["ces", "czech"]),
    ("slo", "Slovak", "sk", ["slk", "slovak"]),
    ("hun", "Hungarian", "hu", ["hungarian"]),
    ("rum", "Romanian", "ro", ["ron", "romanian"]),
    ("ukr", "Ukrainian", "uk", ["ukrainian"]),
    ("bul", "Bulgarian", "bg", ["bulgarian"]),
    ("hrv", "Croatian", "hr", ["croatian"]),
    ("srp", "Serbian", "sr", ["serbian"]),
    ("tgl", "Tagalog", "tl", ["fil", "tagalog", "filipino"]),
    ("per", "Persian", "fa", ["fas", "persian", "farsi"]),
    ("tam", "Tamil", "ta", ["tamil"]),
    ("tel", "Telugu", "te", ["telugu"]),
    ("ben", "Bengali", "bn", ["bengali"]),
    ("mal", "Malayalam", "ml", ["malayalam"]),
    ("kan", "Kannada", "kn", ["kannada"]),
    ("mar", "Marathi", "mr", ["marathi"]),
    ("urd", "Urdu", "ur", ["urdu"]),
    ("ice", "Icelandic", "is", ["isl", "icelandic"]),
    ("cat", "Catalan", "ca", ["catalan"]),
    ("est", "Estonian", "et", ["estonian"]),
    ("lav", "Latvian", "lv", ["latvian"]),
    ("lit", "Lithuanian", "lt", ["lithuanian"]),
    ("slv", "Slovenian", "sl", ["slovenian"]),
]

_LOOKUP: dict[str, str] = {}
NAMES: dict[str, str] = {}
ISO1: dict[str, str] = {}
for code2, name, code1, aliases in _TABLE:
    NAMES[code2] = name
    ISO1[code2] = code1
    for a in [code2, name.lower(), code1, *aliases]:
        _LOOKUP.setdefault(a.lower(), code2)

UNKNOWN = {"", "und", "unk", "mis", "mul", "zxx", "unknown", "any", "original"}


def norm(value: str | None) -> str:
    """Return canonical ISO 639-2 code, or '' when unknown/undetermined."""
    if not value:
        return ""
    v = str(value).strip().lower().replace("_", "-")
    if v in UNKNOWN:
        return ""
    if v in _LOOKUP:
        return _LOOKUP[v]
    base = v.split("-")[0].split(" ")[0]
    if base in _LOOKUP:
        return _LOOKUP[base]
    return v if len(v) == 3 and v.isalpha() else ""


def name(code: str | None) -> str:
    c = norm(code)
    return NAMES.get(c, c or "unknown")


def iso1(code: str | None) -> str:
    c = norm(code)
    return ISO1.get(c, c[:2] if c else "")


def same(a: str | None, b: str | None) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na) and na == nb
