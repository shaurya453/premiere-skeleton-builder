"""Download Google Docs scripts directly via export URL."""
from __future__ import annotations

import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

DOC_ID_REGEX = re.compile(r"(?:/document/(?:u/\d+/)?d/|^)([a-zA-Z0-9_-]{25,})")


def is_google_doc_url(url_or_id: str) -> bool:
    """Check whether a string represents a Google Docs URL or valid doc ID."""
    text = (url_or_id or "").strip()
    if "docs.google.com/document" in text:
        return True
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]{25,}", text))


def extract_google_doc_id(url_or_id: str) -> str | None:
    """Extract Google Doc ID from URL or return the ID if already clean."""
    text = (url_or_id or "").strip()
    match = DOC_ID_REGEX.search(text)
    return match.group(1) if match else None


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename for Windows, macOS, and Linux."""
    cleaned = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    if not cleaned.lower().endswith(".docx"):
        cleaned += ".docx"
    return cleaned or "Google_Doc_Script.docx"


def _extract_filename_from_headers(headers, fallback: str) -> str:
    """Extract filename from Content-Disposition header if available."""
    disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    if not disposition:
        return fallback

    # Check for UTF-8 encoded filename
    utf8_match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", disposition, re.IGNORECASE)
    if utf8_match:
        return _sanitize_filename(urllib.parse.unquote(utf8_match.group(1)))

    # Check for standard filename="name"
    std_match = re.search(r'filename="?([^";\r\n]+)"?', disposition, re.IGNORECASE)
    if std_match:
        return _sanitize_filename(std_match.group(1))

    return fallback


def download_google_doc(
    url_or_id: str,
    destination_dir: str | Path | None = None,
    filename: str | None = None,
    timeout: int = 180,
    progress=lambda m: None,
) -> Path:
    """Download a Google Doc as a .docx file using Google's direct export endpoint.

    Parameters:
        url_or_id: The Google Docs link (e.g. https://docs.google.com/document/d/.../edit)
                   or a document ID.
        destination_dir: Directory where the .docx should be saved.
                         Defaults to `inputs/` or `.cache/gdocs/`.
        filename: Optional explicit filename. If omitted, uses the document title.
        timeout: Network request timeout in seconds. Google renders the .docx export
                 on the fly, which alone can take 20-30+ seconds for a doc with large
                 embedded images before any bytes arrive, so this is generous on purpose.
        progress: Called with a short human-readable string as bytes arrive.

    Returns:
        Path to the saved .docx file.

    Raises:
        ValueError: If the link is invalid or content is not a DOCX.
        PermissionError: If the doc is private and requires Google login.
        FileNotFoundError: If the doc does not exist (404).
    """
    doc_id = extract_google_doc_id(url_or_id)
    if not doc_id:
        raise ValueError(
            f"Could not extract a valid Google Doc ID from: {url_or_id!r}\n"
            "Expected format: https://docs.google.com/document/d/<DOCUMENT_ID>/edit"
        )

    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=docx"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }

    req = urllib.request.Request(export_url, headers=headers)
    progress("Contacting Google Docs — this can take a while for docs with large images…")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            # If redirected to Google login, document is private
            if "accounts.google.com" in final_url:
                raise PermissionError(
                    f"Google Doc {doc_id} is private.\n\n"
                    "Please set sharing permissions in Google Docs to:\n"
                    "'Anyone with the link can view' (Viewer access),\n"
                    "or download the .docx file manually via File > Download > Microsoft Word (.docx)."
                )

            resp_headers = resp.headers
            total = int(resp_headers.get("Content-Length") or 0)
            chunks, done, next_report = [], 0, 5 * 1024 * 1024
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                if done >= next_report:
                    progress(f"Downloading script from Google Docs: {done / 1024**2:.0f}"
                             + (f" / {total / 1024**2:.0f}" if total else "") + " MB")
                    next_report = done + 5 * 1024 * 1024
            data = b"".join(chunks)

    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise PermissionError(
                f"Access denied to Google Doc (HTTP {err.code}).\n"
                "Please verify the document is shared as 'Anyone with the link can view'."
            ) from err
        if err.code == 404:
            raise FileNotFoundError(
                f"Google Doc not found (HTTP 404).\n"
                f"Please verify the URL or ID: {doc_id}"
            ) from err
        raise RuntimeError(f"Failed to fetch Google Doc (HTTP {err.code}): {err.reason}") from err
    except urllib.error.URLError as err:
        raise ConnectionError(f"Network error while connecting to Google Docs: {err.reason}") from err

    # Validate that we got a valid DOCX (which is a ZIP package starting with PK\x03\x04)
    if not data.startswith(b"PK\x03\x04"):
        # Check if returned HTML login page
        preview = data[:500].decode("utf-8", errors="replace")
        if "accounts.google.com" in preview or "ServiceLogin" in preview or "<html" in preview.lower():
            raise PermissionError(
                "This Google Doc is private and requires Google login.\n\n"
                "Please set sharing in Google Docs to 'Anyone with the link can view',\n"
                "or download the .docx manually."
            )
        raise ValueError("Google did not return a valid DOCX document. Please check the document link.")

    # Determine destination folder
    if destination_dir is None:
        from paths import local_cache_dir
        destination_dir = local_cache_dir() / "inputs"
    dest_path = Path(destination_dir)
    dest_path.mkdir(parents=True, exist_ok=True)

    # Determine filename
    default_name = f"gdoc_{doc_id[:12]}.docx"
    final_name = filename or _extract_filename_from_headers(resp_headers, default_name)
    out_file = dest_path / _sanitize_filename(final_name)

    out_file.write_bytes(data)

    # Sanity verify with zipfile
    if not zipfile.is_zipfile(str(out_file)):
        out_file.unlink(missing_ok=True)
        raise ValueError("Downloaded file is corrupted or not a valid Word document.")

    return out_file
