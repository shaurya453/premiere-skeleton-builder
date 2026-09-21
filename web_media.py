"""Fetch web images and classify video links (YouTube, other sites, direct files, Google Drive)."""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from drive_audio import download_drive_file, is_drive_url

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
MAX_IMAGE_BYTES = 60 * 1024 ** 2


def clean_url(url: str) -> str:
    """Unwrap google.com/url?q=... redirect links and trim whitespace."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if (parsed.hostname or "").lower().endswith("google.com") and parsed.path == "/url":
        inner = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
        if inner:
            return inner
    return url


def is_http(url: str) -> bool:
    return urllib.parse.urlparse(clean_url(url)).scheme in ("http", "https")


def _suffix(url: str) -> str:
    return Path(urllib.parse.unquote(urllib.parse.urlparse(clean_url(url)).path)).suffix.lower()


def is_direct_image_url(url: str) -> bool:
    return is_http(url) and _suffix(url) in IMAGE_EXT


def is_direct_video_url(url: str) -> bool:
    return is_http(url) and _suffix(url) in VIDEO_EXT


def identify_source(url: str):
    """Return (kind, key, url) for a video link; kind is youtube | drive | file | site."""
    url = clean_url(url)
    if not is_http(url):
        raise ValueError("Video links must be http(s) URLs")
    from youtube_media import youtube_id
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "youtube", youtube_id(url), url
    digest = hashlib.sha1(url.encode()).hexdigest()[:12]
    if is_drive_url(url):
        from drive_audio import extract_drive_id
        file_id = extract_drive_id(url)
        if not file_id:
            raise ValueError("Could not find a file ID in that Google Drive link")
        return "drive", f"drive_{file_id[:16]}", url
    if is_direct_video_url(url):
        return "file", f"file_{digest}", url
    return "site", f"web_{digest}", url


# ---------------------------------------------------------------- images
def _get(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                    "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
            kind = response.headers.get_content_type()
            final = response.geturl()
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"HTTP {err.code} from {urllib.parse.urlparse(url).hostname}") from err
    except urllib.error.URLError as err:
        raise ConnectionError(f"network error: {err.reason}") from err
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("file is larger than 60 MB")
    return data, kind, final


META = re.compile(r"<meta\b[^>]*>", re.I)


def page_image_url(html_text: str, base: str) -> str | None:
    """Find the main image a page advertises (og:image, twitter:image, image_src)."""
    for tag in META.findall(html_text):
        if re.search(r'(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']', tag, re.I):
            found = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if found:
                return urllib.parse.urljoin(base, found.group(1).replace("&amp;", "&"))
    link = re.search(r'<link\b[^>]*rel\s*=\s*["\']image_src["\'][^>]*href\s*=\s*["\']([^"\']+)["\']', html_text, re.I)
    return urllib.parse.urljoin(base, link.group(1)) if link else None


def fetch_image(url: str, dest_dir: str | Path, progress=lambda m: print(m, flush=True)) -> dict:
    """Download an image (direct URL, page with a main image, or Drive file) as a PNG/JPEG asset."""
    url = clean_url(url)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = "web_" + hashlib.sha1(url.encode()).hexdigest()[:10]
    for cached in dest_dir.glob(stem + ".*"):
        with Image.open(cached) as image:
            width, height = image.size
        return _asset(cached, width, height, url)
    if is_drive_url(url):
        raw = download_drive_file(url, dest_dir / "_download", label="image", progress=progress)
        data = raw.read_bytes()
        raw.unlink(missing_ok=True)
    else:
        progress(f"Downloading image: {url}")
        data, kind, final = _get(url)
        if kind in ("text/html", "application/xhtml+xml"):
            target = page_image_url(data.decode("utf-8", errors="replace"), final)
            if not target:
                raise ValueError("the page does not advertise an image (no og:image)")
            progress(f"Downloading page image: {target}")
            data, kind, _ = _get(target)
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:
        raise ValueError(f"not a readable image ({error})") from error
    upright = ImageOps.exif_transpose(image)
    keep_bytes = image.format in ("JPEG", "PNG") and upright is image
    if keep_bytes:
        extension, payload = (".jpg" if image.format == "JPEG" else ".png"), data
    else:
        if upright.mode not in ("RGB", "RGBA"):
            upright = upright.convert("RGBA" if "transparency" in upright.info or upright.mode in ("LA", "PA") else "RGB")
        buffer = io.BytesIO()
        upright.save(buffer, "PNG")
        extension, payload = ".png", buffer.getvalue()
    target = dest_dir / (stem + extension)
    target.write_bytes(payload)
    width, height = upright.size
    return _asset(target, width, height, url)


def _asset(path: Path, width: int, height: int, url: str) -> dict:
    return {"path": str(path.resolve()), "width": width, "height": height, "bookmarks": [],
            "source_part": url, "source_url": url, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


# ---------------------------------------------------------------- video files
def download_direct_video(kind: str, key: str, url: str, cache: Path,
                          progress=lambda m: print(m, flush=True)):
    """Fetch a whole video file (direct link or Google Drive) into cache; return (path, metadata)."""
    from youtube_media import probe
    cache.mkdir(parents=True, exist_ok=True)
    found = next(iter(sorted(cache.glob(key + ".*"))), None)
    found = found if found and found.suffix.lower() in VIDEO_EXT else None
    if not found:
        if kind == "drive":
            saved = download_drive_file(url, cache / "_download", label="video", default_ext=".mp4", progress=progress)
            found = cache / (key + (saved.suffix.lower() if saved.suffix.lower() in VIDEO_EXT else ".mp4"))
            saved.replace(found)
        else:
            found = cache / (key + _suffix(url))
            progress(f"Downloading video file: {url}")
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=60) as response, found.open("wb") as out:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
            except (urllib.error.URLError, OSError) as error:
                found.unlink(missing_ok=True)
                raise RuntimeError(f"could not download {url}: {error}") from error
    info = probe(found)
    title = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).stem or key
    return found, {"id": key, "title": title, "duration": info["duration"], "source_url": url,
                   "download_offset_seconds": 0, "download_end_seconds": info["duration"]}
