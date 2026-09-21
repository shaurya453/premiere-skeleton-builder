"""Download a voiceover from a shared Google Drive link."""
from __future__ import annotations

from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

DRIVE_ID = re.compile(r"(?:/file/(?:u/\d+/)?d/|[?&]id=)([A-Za-z0-9_-]{20,})")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
AUDIO_TYPES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm")


def is_drive_url(text: str) -> bool:
    text = (text or "").strip()
    return "drive.google.com" in text or "drive.usercontent.google.com" in text


def extract_drive_id(text: str) -> str | None:
    found = DRIVE_ID.search((text or "").strip())
    return found.group(1) if found else None


def _header_filename(headers, fallback: str) -> str:
    disposition = headers.get("Content-Disposition") or ""
    found = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if found:
        return urllib.parse.unquote(found.group(1).strip())
    found = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
    return found.group(1).strip() if found else fallback


def _safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name or "").strip().rstrip(".")
    return cleaned or fallback


def download_drive_file(url: str, destination_dir: str | Path, label: str = "file", default_ext: str = ".bin",
                        progress=lambda m: print(m, flush=True), timeout: int = 60) -> Path:
    """Save a shared Drive file into destination_dir and return its path.

    The file must be shared as 'Anyone with the link can view'. Video files are
    accepted too; FFmpeg extracts the sound later when the build prepares audio.
    """
    file_id = extract_drive_id(url)
    if not file_id:
        raise ValueError("Could not find a file ID in that Google Drive link.")
    endpoint = "https://drive.usercontent.google.com/download?" + urllib.parse.urlencode(
        {"id": file_id, "export": "download", "confirm": "t"})
    request = urllib.request.Request(endpoint, headers={"User-Agent": USER_AGENT})
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403, 404):
            raise PermissionError("Google Drive refused access. Share the file as 'Anyone with the link can view'.") from err
        raise RuntimeError(f"Google Drive download failed (HTTP {err.code}).") from err
    except urllib.error.URLError as err:
        raise ConnectionError(f"Network error while contacting Google Drive: {err.reason}") from err
    with response:
        kind = response.headers.get("Content-Type", "")
        if "text/html" in kind:
            raise PermissionError("Google Drive returned a web page instead of the file. "
                                  "Share it as 'Anyone with the link can view' and check the link.")
        name = _header_filename(response.headers, f"drive_{file_id[:10]}{default_ext}")
        target = destination_dir / _safe_name(name, f"drive_{file_id[:10]}{default_ext}")
        total = int(response.headers.get("Content-Length") or 0)
        done, next_report = 0, 0
        with target.open("wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    progress(f"Downloading {label} from Drive: {done / 1024**2:.0f}"
                             + (f" / {total / 1024**2:.0f}" if total else "") + " MB")
                    next_report = done + 10 * 1024**2
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise ValueError("Google Drive returned an empty file.")
    return target


def download_drive_audio(url: str, destination_dir: str | Path, progress=lambda m: print(m, flush=True), timeout: int = 60) -> Path:
    """Voiceover download; the label keeps the 'Downloading voiceover' log text the app watches."""
    return download_drive_file(url, destination_dir, label="voiceover", default_ext=".mp3", progress=progress, timeout=timeout)
