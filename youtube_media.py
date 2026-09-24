"""Download public YouTube sources, retain handles, and prepare frame-accurate edits."""
from __future__ import annotations

import json
import os
import math
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from paths import NO_WINDOW, ffmpeg_exe, local_cache_dir, node_exe
from web_media import identify_source, download_direct_video

FPS = 30000 / 1001


def youtube_id(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Expected a YouTube HTTPS URL")
    if host in ("youtu.be", "www.youtu.be"):
        value = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        parts = parsed.path.strip("/").split("/")
        value = (parts[1] if len(parts) == 2 and parts[0] in ("shorts", "embed", "live")
                 else parse_qs(parsed.query).get("v", [""])[0])
    else:
        raise ValueError("Only YouTube video URLs are supported")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise ValueError("YouTube video ID is missing or invalid")
    return value


def source_range(label):
    pattern = r"(\d+:\d{2}(?::\d{2})?(?:\.\d+)?)\s*[-–—]\s*(\d+:\d{2}(?::\d{2})?(?:\.\d+)?)"
    match = re.search(pattern, label)
    if not match:
        raise ValueError(f"An explicit start/end range is required: {label}")
    def seconds(value):
        parts = [float(v) for v in value.split(":")]
        if any(not (0 <= v < 60) for v in parts[1:]):
            raise ValueError(f"Invalid source time: {value}")
        result = 0
        for v in parts:
            result = result * 60 + v
        return result
    start, end = map(seconds, match.groups())
    if end <= start:
        raise ValueError(f"Source range ends before it begins: {label}")
    return start, end


def source_window(ranges, duration, handles=600, full=None, full_limit=5400):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Source duration must be known and positive")
    if not math.isfinite(full_limit) or full_limit <= 0:
        raise ValueError("Full-video limit must be positive")
    if not ranges:
        raise ValueError("At least one timestamp range is required")
    if handles < 0 or not math.isfinite(handles):
        raise ValueError("Handles must be a finite nonnegative number")
    for a, b in ranges:
        if a < 0 or b <= a or b > duration + .1:
            raise ValueError(f"Requested range {a:g}-{b:g}s exceeds source duration {duration:g}s")
    if full is True or (full is None and duration <= full_limit):
        return 0.0, duration
    return max(0.0, min(a for a, _ in ranges)-handles), min(duration, max(b for _, b in ranges)+handles)


def probe(path):
    """Duration, size, frame rate and audio presence via PyAV (no ffprobe needed)."""
    import av
    with av.open(str(path)) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        if video is None:
            raise ValueError("The file has no video stream")
        if video.duration:
            duration = float(video.duration * video.time_base)
        elif container.duration:
            duration = container.duration / av.time_base
        else:
            raise ValueError("Could not determine the video's duration")
        rate = video.average_rate
        return {"duration": duration, "width": video.codec_context.width, "height": video.codec_context.height,
                "frames": video.frames or None,
                "fps": f"{rate.numerator}/{rate.denominator}" if rate else "0/1",
                "has_audio": any(s.type == "audio" for s in container.streams)}


def download_source(video_id, cache, ranges, handles=600, full=None, full_limit=5400, url=None):
    """Inspect duration first; download only a section for sources above the limit.

    video_id is the cache key (the YouTube ID, or a hash for other sites); url defaults to YouTube."""
    from yt_dlp import YoutubeDL
    cache.mkdir(parents=True, exist_ok=True)
    existing = cache / f"{video_id}.mp4"
    metadata = cache / f"{video_id}.json"
    if existing.exists() and metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        actual = probe(existing)
        data.update(duration=actual["duration"], download_offset_seconds=0)
        print(f"Using cached full YouTube source: {video_id}", flush=True)
        return existing, data
    node = node_exe()
    options = {
        "noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False,
        "format": "bv*[width<=1920][height<=1920][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*[width<=1920][height<=1920]+ba/b",
        "merge_output_format": "mp4", "outtmpl": str(cache / f"{video_id}.%(ext)s"),
        "socket_timeout": 30, "retries": 2, "fragment_retries": 2,
        "js_runtimes": {"node": {"path": node}} if node else {},
        "ffmpeg_location": ffmpeg_exe(),
    }
    url = url or f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("is_live") or not info.get("duration"):
        raise ValueError("A finished video with a known duration is required")
    duration = float(info["duration"])
    a, b = source_window([(start, min(end, duration)) for start, end in ranges],
                         duration, handles, full, full_limit)
    partial = a > 0 or b < duration
    if partial:
        # Separate cache identity prevents a partial download being reused as a full video.
        key = f"{video_id}_section_{a:.3f}_{b:.3f}"
        existing, metadata = cache / f"{key}.mp4", cache / f"{key}.json"
        if existing.exists() and metadata.exists():
            data = json.loads(metadata.read_text(encoding="utf-8"))
            probe(existing)
            return existing, data
        options.update(outtmpl=str(cache / (key + ".%(ext)s")),
                       download_ranges=lambda *_: [{"start_time": a, "end_time": b}],
                       force_keyframes_at_cuts=True)
    print(f"Downloading {video_id}: " + (f"section {a:.2f}-{b:.2f}s" if partial else "complete video"), flush=True)
    with YoutubeDL(options) as ydl:
        downloaded = ydl.extract_info(url, download=True)
        source = existing if existing.exists() else Path(ydl.prepare_filename(downloaded)).with_suffix(".mp4")
    if not source.is_file():
        raise RuntimeError("Downloader did not produce the expected source file")
    actual = probe(source)
    if partial and abs(actual["duration"] - (b-a)) > 1:
        raise ValueError("Downloaded section duration differs from the requested window")
    data = {"id": video_id, "title": info.get("title", video_id),
            "duration": duration if partial else actual["duration"], "source_url": url,
            "download_offset_seconds": a if partial else 0,
            "download_end_seconds": b if partial else actual["duration"]}
    metadata.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return source, data


def prepare_sources(cues, output, handles=600, full=None, cache=None, full_limit=5400):
    """One cached download and one editable source window per distinct video source.

    Downloads every recognized video reference regardless of whether its cue could be aligned
    against the narration - alignment only decides *where on the timeline* a clip lands
    (place_video_clips, confirmed track only), not whether the source is fetched at all. An
    unaligned reference still gets `ref["video_asset"]` set here, so it's available for the
    caller to place on the unconfirmed track instead of being silently skipped."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache = Path(cache or local_cache_dir()/"youtube")
    groups, failures, sources = {}, [], {}
    for cue in cues:
        if cue["kind"] != "video":
            continue
        for ref in cue["refs"]:
            try:
                kind, identifier, url = identify_source(ref["target"])
                a, b = source_range(ref["label"])
                ref.update(video_id=identifier, source_start=a, source_end=b)
                sources[identifier] = (kind, url)
                groups.setdefault(identifier, []).append((cue, ref))
            except ValueError as error:
                ref["video_error"] = str(error)
                cue["review"].append(str(error))
                failures.append(str(error))
    prepared = []
    for identifier, refs in groups.items():
        try:
            kind, url = sources[identifier]
            if kind in ("file", "drive"):
                original, data = download_direct_video(kind, identifier, url, cache)
            else:
                original, data = download_source(identifier, cache,
                    [(ref["source_start"], ref["source_end"]) for _, ref in refs], handles, full, full_limit, url=url)
            for cue, ref in refs:
                if ref["source_start"] >= data["duration"]:
                    raise ValueError(f"Requested start {ref['source_start']:g}s is beyond the end of the video")
                if ref["source_end"] > data["duration"]:
                    note = f"{ref['label']}: source ends at {data['duration']:.2f}s; excerpt clamped to actual end"
                    cue["review"].append(note)
                    ref["source_warning"] = note
            a, b = source_window([(ref["source_start"], min(ref["source_end"], data["duration"])) for _, ref in refs],
                                 data["duration"], handles, full, full_limit)
            target = output / f"{identifier}_source.mp4"
            print(f"Preparing {identifier}: source {a:.2f}-{b:.2f}s, 29.97 fps with editable handles...", flush=True)
            subprocess.run([
                ffmpeg_exe(), "-v", "error", "-nostdin", "-ss", str(max(0, a-data.get("download_offset_seconds", 0))), "-i", str(original), "-t", str(b-a),
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", "fps=30000/1001,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", str(target)], check=True, **NO_WINDOW)
            actual = probe(target)
            frame_count = actual["frames"] or round(actual["duration"]*FPS)
            asset = {**data, **actual, "path": str(target), "source_offset_seconds": a,
                     "source_window_end_seconds": b, "source_duration_frames": frame_count,
                     "requested_handle_seconds": handles, "full_source": a == 0 and b == data["duration"],
                     "original_duration_seconds": data["duration"], "full_video_limit_seconds": full_limit, "kind": "video"}
            prepared.append(asset)
            for cue, ref in refs:
                ref["video_asset"] = asset
                ref["in_frame"] = round((ref["source_start"]-a)*FPS)
                ref["out_frame"] = min(frame_count, round((ref["source_end"]-a)*FPS))
                if ref["out_frame"] <= ref["in_frame"]:
                    raise ValueError("Selected excerpt is empty after conversion")
        except Exception as error:
            message = f"Video {sources[identifier][1]}: {error}"
            print(message, flush=True)
            failures.append(message)
            for cue, ref in refs:
                ref.pop("video_asset", None)
                ref["video_error"] = message
                cue["review"].append("Download/preparation failed; source link preserved")
    return prepared, failures


def place_video_clips(cues, images, sequence_frames):
    """Normal speed; cap main edit at its passage. Full selections remain separate.

    Only places refs whose cue aligned against the narration (the confirmed track) - a ref
    with a `video_asset` but no alignment is left for the caller to place on the unconfirmed
    track instead (its `video_asset` was already set by prepare_sources regardless)."""
    edits, selects, cursor = [], [], 0
    all_cue_starts = sorted(round(c["start"]*FPS) for c in cues if c["start"] is not None)
    for cue in cues:
        if cue["kind"] != "video" or cue["start"] is None:
            continue
        position = round(cue["start"]*FPS)
        passage_end = min(sequence_frames, round(cue["end"]*FPS))
        future = [v for v in all_cue_starts if v > position]
        if future:
            passage_end = min(passage_end, future[0])
        for ref in cue["refs"]:
            asset = ref.get("video_asset")
            if not asset:
                continue
            source_in, source_out = ref["in_frame"], ref["out_frame"]
            length = source_out-source_in
            base = {**asset, "name": f"VIDEO {ref['label']} | {asset['title']}",
                    "in_frame": source_in, "source_url": ref["target"], "passage": cue["passage"],
                    "doc_order": cue.get("doc_order", 0),
                    "requested_source_start": ref["source_start"], "requested_source_end": ref["source_end"],
                    "source_notes": [ref["source_warning"]] if ref.get("source_warning") else []}
            selects.append({**base, "start_frame": cursor, "end_frame": cursor+length})
            cursor += length
            take = min(length, passage_end-position)
            if take > 0:
                edits.append({**base, "start_frame": position, "end_frame": position+take})
                ref["timeline_start_frame"], ref["timeline_end_frame"] = position, position+take
                position += take
            if take < length:
                cue["review"].append(f"{ref['label']}: full excerpt is longer than available narration; main edit trimmed, exact range in Source_Selects.xml")
        if position < passage_end:
            cue["review"].append("Excerpt is shorter than narration; remaining passage left for editor")
    return edits, selects
