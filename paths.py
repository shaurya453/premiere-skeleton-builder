"""Locations that work both from source and from a packaged (PyInstaller) app."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import shutil
import sys

FROZEN = bool(getattr(sys, "frozen", False))
# Read-only resources: the source folder, or PyInstaller's bundle folder.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent
APP_NAME = "Premiere Skeleton Builder"


def _fix_macos_tls_certificates():
    """Frozen macOS builds don't reliably find a CA bundle through the system trust
    store the way Homebrew/python.org Python does, so every HTTPS request fails with
    "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate". Point Python's
    ssl module at certifi's bundled cacert.pem (PyInstaller packages it automatically)
    instead. Safe to call on every platform; only macOS/frozen builds need it in practice."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except Exception:
        pass


_fix_macos_tls_certificates()


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


def app_dir() -> Path:
    """Directory the app lives in: sibling of the .exe on Windows (next to its "_internal"
    folder), sibling of the .app bundle on macOS, or the project root when run from source.
    The default Projects/Media/Models folders are created here."""
    if not FROZEN:
        return ROOT
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for ancestor in exe.parents:
            if ancestor.suffix == ".app":
                return ancestor.parent
    return exe.parent


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".skeletonbuilder_write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


@lru_cache(maxsize=1)
def default_data_root() -> Path:
    """Where Projects/Media/Models/Cache default to: next to the app normally, or the
    per-user app-data folder if that turns out to be read-only. macOS Gatekeeper "App
    Translocation" runs an unmoved, still-quarantined .app copy from a randomized
    read-only path under /private/var/folders/.../AppTranslocation/...; a real Finder
    drag of the .app into /Applications fixes that for good. Until then, this keeps the
    app usable instead of silently failing to create its default folders."""
    candidate = app_dir()
    return candidate if _is_writable(candidate) else user_data_dir()


def local_cache_dir() -> Path:
    """Bulk, regenerable working data (downloaded script inputs, cue-preview assets,
    YouTube source cache). Kept next to the app like Projects/Media/Models rather than
    in the OS profile, so "everything lives in the app folder" actually holds — unlike
    cache_dir(), which stays in the OS profile for small persistent app state (settings,
    the build queue) that should survive even if the app folder itself gets moved."""
    return default_data_root() / "Cache"


def temp_dir() -> Path:
    """Scratch space for short-lived work (e.g. a doc pre-flight check's TemporaryDirectory)
    that would otherwise default to the OS temp folder. Kept next to the app for the same
    "everything lives in the app folder" reason as local_cache_dir(); unlike Cache, nothing
    here is meant to be reused between calls."""
    return default_data_root() / "Temp"


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
