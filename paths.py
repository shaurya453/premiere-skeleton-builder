"""Locations that work both from source and from a packaged (PyInstaller) app."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

FROZEN = bool(getattr(sys, "frozen", False))
# Read-only resources: the source folder, or PyInstaller's bundle folder.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent
APP_NAME = "Premiere Skeleton Builder"


# Stops console windows flashing up when a windowed app runs ffmpeg and friends on Windows.
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


def user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def cache_dir() -> Path:
    """Writable app data (settings, word-timing and download caches)."""
    return user_data_dir() if FROZEN else ROOT / ".cache"


def ffmpeg_exe() -> str:
    """ffmpeg bundled with the app (imageio-ffmpeg), else the one on PATH."""
    try:
        import imageio_ffmpeg
        found = imageio_ffmpeg.get_ffmpeg_exe()
        if found and Path(found).is_file():
            return found
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg or use the packaged app.")
    return found


def node_exe() -> str | None:
    """Node.js for yt-dlp's YouTube signature solving: bundled copy first, then PATH."""
    name = "node.exe" if os.name == "nt" else "node"
    bundled = BUNDLE / "tools" / name
    if bundled.is_file():
        return str(bundled)
    return shutil.which("node")
