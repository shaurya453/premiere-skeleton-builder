"""The one list of characters that must never appear in a name this app creates.

Every project folder and every file the app writes or copies goes through clean_name() or
clean_filename(), so a script called "CASE 5&6" becomes the folder "CASE 5-6".
"""
import os
import re
from pathlib import Path

# Windows refuses these outright, and they are path or URL syntax everywhere else.
RESERVED = '<>:"/\\|?*'
# Legal on disk, but they are markup, URL or shell syntax. "&" is confirmed to make Premiere
# reject the XML ("the project appears to be damaged"): the path in the XML is %-escaped, and
# Premiere fails on the "&" once it unescapes it. The rest are the same family, kept out as a
# precaution because the XML paths pass through URLs. "~" is left alone because Windows short
# paths (PROGRA~1) contain it.
RISKY = "&%#+;=,'`!$@^[]{}"
RESTRICTED = frozenset(RESERVED + RISKY)
REPLACEMENT = "-"

# Windows treats these as devices whatever the extension.
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def _is_restricted(char):
    return char in RESTRICTED or ord(char) < 32 or ord(char) == 127


def clean_name(name, limit=None, fallback="Untitled"):
    """Replace every restricted character with a dash; collapse runs and trim the ends."""
    text = "".join(REPLACEMENT if _is_restricted(c) else c for c in str(name or ""))
    text = re.sub(r"-{2,}", "-", text).strip(" .-")
    if limit:
        text = text[:limit].strip(" .-")
    if text.split(".")[0].upper() in _DEVICE_NAMES:
        text += REPLACEMENT
    return text or fallback


def clean_filename(name, fallback="file", limit=None):
    """clean_name() for a file name: a plain extension such as .docx or .mp3 is kept as it is."""
    stem, suffix = os.path.splitext(str(name or ""))
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        stem, suffix = str(name or ""), ""
    return clean_name(stem, limit=limit, fallback=fallback) + suffix.lower()


def restricted_characters(path):
    """Restricted characters inside a path, ignoring the drive and root (E:\\ and / are fine).

    Used on folders the app did not name (the Projects and Media locations the user chose)."""
    path = Path(path)
    parts = path.parts[1:] if path.anchor else path.parts
    found = {c for part in parts for c in part if c in RESTRICTED}
    return "".join(sorted(found))
