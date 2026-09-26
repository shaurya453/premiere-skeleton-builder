"""Shared retry helper for the single-shot HTTP downloads (Google Docs, Drive, direct video
URLs) that otherwise fail outright on one dropped connection - unlike youtube_media.py's
yt-dlp-based downloads, which already get yt-dlp's own retries/fragment_retries."""
from __future__ import annotations

import time


def with_retries(fn, attempts=3, backoff=2.0, retry_on=(ConnectionError, TimeoutError)):
    """Call fn() and return its result; retry up to `attempts` times total on a `retry_on`
    exception, waiting `backoff * attempt_number` seconds between tries. Re-raises the last
    error once attempts are exhausted; anything not in `retry_on` propagates immediately."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on:
            if attempt == attempts:
                raise
            time.sleep(backoff * attempt)
