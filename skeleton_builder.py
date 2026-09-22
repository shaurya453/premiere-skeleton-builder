"""Build an editable Premiere/FCP7 XML skeleton from a bookmarked DOCX.

All media processing is local. Word timings come from local speech recognition
(faster-whisper) run on the voiceover; no transcript export from Premiere is needed.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import html
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlparse
from zipfile import ZipFile

from lxml import etree as ET
from PIL import Image, ImageDraw, ImageFont, ImageOps

from google_docs import is_google_doc_url, download_google_doc
from paths import NO_WINDOW, cache_dir, ffmpeg_exe, local_cache_dir

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
FPS = 30000 / 1001
TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?")


def q(prefix, name):
    return "{" + NS[prefix] + "}" + name


def tokens(text):
    return [(re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", m.group()).lower()),
             m.start(), m.end()) for m in TOKEN.finditer(text)]


def clock(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def bookmark_id(target):
    found = re.search(r"(?:bookmark=)(?:id\.)?([^&#]+)", target)
    return unquote(found.group(1)) if found else target.removeprefix("id.")



def visual_links(raw, links, bookmark_images, warnings, fetch_image=None):
    """Recover complete range text even when only part is a hyperlink.

    Video cue: a time range (1:20-1:45) overlapping a link to YouTube, another video
    site, a direct video file or a Google Drive video. Image cue: a bookmark link to an
    embedded picture, or an image URL / page link labelled IMG. `fetch_image(url)`
    downloads web images; without it (script inspection) they are only counted.
    """
    from youtube_media import source_range
    from web_media import identify_source, is_direct_image_url, is_http
    def video_link(target):
        return is_http(target) and not is_direct_image_url(target)
    ranges = list(re.finditer(r"\d+:\d{2}(?::\d{2})?(?:\.\d+)?\s*[-–—]\s*\d+:\d{2}(?::\d{2})?(?:\.\d+)?", raw))
    result = []
    used = set()
    for match in ranges:
        overlapping = [l for l in links if l['start'] < match.end() and l['end'] > match.start() and video_link(l['target'])]
        if not overlapping:
            warnings.append(f"Timestamp has no overlapping video link: {match.group()}")
            continue
        try:
            ids = {identify_source(l['target'])[1] for l in overlapping}
            source_range(match.group())
            if len(ids) != 1:
                raise ValueError('multiple different videos overlap this range')
        except ValueError as error:
            warnings.append(f"Invalid timestamp {match.group()}: {error}")
            continue
        result.append({**overlapping[0], 'start':match.start(), 'end':match.end(), 'kind':'video', 'asset':None})
        used.update(id(l) for l in overlapping)
    for link in links:
        label = raw[link['start']:link['end']]
        if not label.strip() or any(m.start() < link['end'] and m.end() > link['start'] for m in ranges):
            continue
        target = link['target']
        asset = bookmark_images.get(bookmark_id(target))
        explicit = bool(re.search(r'\bIMG(?:\s*\d+)?\s*$', label, re.I) or
                        (label.strip().isdigit() and 'IMG' in raw[max(0,link['start']-15):link['start']]))
        remote = not asset and is_http(target) and (is_direct_image_url(target) or explicit)
        if remote:
            if fetch_image is None:
                asset = {"path": None, "remote": target}
            else:
                try:
                    asset = fetch_image(target)
                except Exception as error:
                    warnings.append(f"Could not download image {target}: {error}")
        if asset or explicit or remote:
            result.append({**link, 'kind':'image', 'asset':asset, 'inline':bool(asset and not explicit and not remote)})
        elif id(link) not in used and video_link(target) and re.search(r'\d+:\d{2}',label):
            warnings.append(f"Video timestamp needs a complete start/end range: {label}")
    return sorted(result, key=lambda l:l['start'])


def read_docx(path, assets_dir, fetch_web=True):
    """Read bookmarks in document order, including body-level bookmark nodes."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    web_fetch = (lambda url: __import__('web_media').fetch_image(url, assets_dir)) if fetch_web else None
    warnings = []
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        relationships = {e.get("Id"): e.get("Target") for e in
                         ET.fromstring(archive.read("word/_rels/document.xml.rels"))}
        bookmark_images, embedded, pending = {}, [], []
        for node in root.iter():
            if node.tag == q("w", "bookmarkStart"):
                pending.append(node.get(q("w", "name")))
            elif node.tag == q("a", "blip"):
                relation = relationships.get(node.get(q("r", "embed")))
                if not relation:
                    continue
                archive_path = "word/" + relation
                try:
                    data = archive.read(archive_path)
                    filename = f"asset_{len(embedded)+1:02d}" + Path(relation).suffix.lower()
                    target = assets_dir / filename
                    target.write_bytes(data)
                    with Image.open(target) as image:
                        image.load()
                        width, height = image.size
                    asset = {"path": str(target.resolve()), "width": width, "height": height,
                             "bookmarks": list(pending), "source_part": archive_path,
                             "sha256": hashlib.sha256(data).hexdigest()}
                    embedded.append(asset)
                    for name in pending:
                        bookmark_images[name] = asset
                except Exception as error:
                    # A format PIL can't open (e.g. an embedded WMF/EMF) shouldn't sink the
                    # whole build; skip this one picture and let the normal "Missing
                    # bookmark/image" warning below cover its cue instead.
                    warnings.append(f"Skipped unreadable embedded image {archive_path}: {error}")
                pending = []

        paragraphs = []
        for p in root.findall(".//w:p", NS):
            pieces, links = [], []

            def visit(n):
                start = sum(map(len, pieces))
                if n.tag == q("w", "t"):
                    pieces.append(n.text or "")
                else:
                    for child in n:
                        visit(child)
                if n.tag == q("w", "hyperlink"):
                    target = relationships.get(n.get(q("r", "id"))) or n.get(q("w", "anchor"), "")
                    links.append({"start": start, "end": sum(map(len, pieces)), "target": target})

            visit(p)
            text = "".join(pieces)
            if text.strip():
                paragraphs.append({"text": text, "links": links})

    all_script_tokens, cues = [], []
    for paragraph in paragraphs:
        raw = paragraph["text"]
        clean = list(raw)
        # Parenthetical material is an editorial/pronunciation note in this input format.
        for match in re.finditer(r"\([^()]*\)", raw):
            clean[match.start():match.end()] = " " * len(match.group())
        clean = "".join(clean)
        ptokens = tokens(clean)
        base = len(all_script_tokens)
        all_script_tokens.extend(t[0] for t in ptokens)
        previous = None
        for link in visual_links(raw, paragraph['links'], bookmark_images, warnings, web_fetch):
            label = raw[link['start']:link['end']]
            asset = link['asset']
            kind = link['kind']
            is_image = kind == 'image'
            if is_image and not asset:
                warnings.append(f"Missing bookmark/image for {label}: {link['target']}")
            # Cues separated only by punctuation or other notes share the same passage.
            anchor = link["end"] if link.get("inline") else link["start"]
            end_index = sum(1 for _, _, b in ptokens if b <= anchor)
            if previous is not None and previous["local_end"] == end_index and previous["kind"] == kind:
                previous["refs"].append({"label": label, "target": link["target"], "asset": asset})
                continue
            prefix = clean[:anchor].rstrip()
            # Exclude trailing sentence punctuation: a cue after a full stop belongs to that sentence.
            trimmed = re.sub(r"[\s.!?\"'’”]+$", "", prefix)
            boundaries = list(re.finditer(r"[.!?][\"'’”]*(?=\s+[A-Z])", trimmed))
            begin_char = boundaries[-1].end() if boundaries else 0
            start_index = sum(1 for _, _, b in ptokens if b <= begin_char)
            if kind == "video":
                # A video link describes the paragraph up to the cue, or the portion
                # since the previous visual cue when several occur in one paragraph.
                start_index = 0
            if previous is not None:
                start_index = (previous["script_start"] - base if previous["local_end"] == end_index
                               else max(start_index, previous["local_end"]))
            if start_index >= end_index:
                warnings.append(f"No preceding narration for {label}")
                continue
            passage = clean[ptokens[start_index][1]:ptokens[end_index-1][2]]
            cue = {"kind": kind, "script_start": base + start_index, "script_end": base + end_index,
                   "local_end": end_index, "passage": re.sub(r"\s+", " ", passage).strip(),
                   "refs": [{"label": label, "target": link["target"], "asset": asset}]}
            cues.append(cue)
            previous = cue
    return all_script_tokens, cues, embedded, warnings


def inspect_docx(path):
    """Fast pre-flight check of a DOCX script or Google Docs URL without altering output."""
    import tempfile
    str_path = str(path).strip()
    if is_google_doc_url(str_path):
        try:
            with tempfile.TemporaryDirectory() as tmp_fetch:
                downloaded = download_google_doc(str_path, destination_dir=tmp_fetch)
                with tempfile.TemporaryDirectory() as tmp:
                    words, cues, embedded, warnings = read_docx(downloaded, Path(tmp), fetch_web=False)
                    image_cues = sum(1 for c in cues if c.get("kind") == "image")
                    video_cues = sum(1 for c in cues if c.get("kind") == "video")
                    return {
                        "valid": True,
                        "word_count": len(words),
                        "cues_count": len(cues),
                        "image_cues": image_cues,
                        "video_cues": video_cues,
                        "embedded_images": len(embedded),
                        "warnings": warnings,
                        "is_gdoc": True,
                        "downloaded_filename": downloaded.name,
                    }
        except Exception as e:
            return {"valid": False, "error": f"Google Docs error: {e}"}

    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".docx":
        return {"valid": False, "error": "Not a valid .docx file"}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            words, cues, embedded, warnings = read_docx(path, Path(tmp), fetch_web=False)
            image_cues = sum(1 for c in cues if c.get("kind") == "image")
            video_cues = sum(1 for c in cues if c.get("kind") == "video")
            return {
                "valid": True,
                "word_count": len(words),
                "cues_count": len(cues),
                "image_cues": image_cues,
                "video_cues": video_cues,
                "embedded_images": len(embedded),
                "warnings": warnings
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def preview_cues(path):
    """Lightweight cue-by-cue preview for the UI: no network access, no speech recognition —
    just the same doc parsing / cue matching `inspect_docx` uses. `path` must already be a
    local .docx file (resolve a Google Doc link first). Returns (cues, warnings) where each
    cue is {kind, passage, targets, asset_path}; asset_path is set only for embedded images
    (extracted directly from the docx, so no network access is needed to know their path).
    Embedded images are cached under local_cache_dir()/preview/<doc hash> so the returned
    paths stay valid after this call returns (unlike a temp directory, which would be
    deleted)."""
    path = Path(path)
    assets_dir = local_cache_dir() / "preview" / hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    _, cues, _, warnings = read_docx(path, assets_dir, fetch_web=False)
    preview = [{
        "kind": cue["kind"],
        "passage": cue["passage"],
        "targets": [ref["target"] for ref in cue["refs"]],
        "asset_path": next((ref["asset"]["path"] for ref in cue["refs"]
                             if ref.get("asset") and ref["asset"].get("path")), None),
    } for cue in cues]
    return preview, warnings


def token_mapping(source, target):
    mapping = {}
    for block in SequenceMatcher(None, source, target, autojunk=False).get_matching_blocks():
        mapping.update({block.a + i: block.b + i for i in range(block.size)})
    return mapping


def timed_tokens(words_path, audio):
    """Load per-word timings produced by local ASR for exactly this audio file."""
    if not words_path:
        raise ValueError("Word timings are required; run speech recognition on the voiceover first.")
    try:
        data = json.loads(Path(words_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Word timing cache at {words_path} is missing or corrupted ({error}); "
                          "delete it and rebuild.") from error
    if data.get("audio_sha256") != hashlib.sha256(Path(audio).read_bytes()).hexdigest():
        raise ValueError("Word timing cache belongs to a different audio file.")
    fine = []
    for word in data["words"]:
        ts = tokens(word["word"])
        for j, t in enumerate(ts):
            fine.append({"token": t[0], "start": word["start"] + (word["end"]-word["start"])*j/len(ts),
                         "end": word["start"] + (word["end"]-word["start"])*(j+1)/len(ts)})
    if not fine:
        raise ValueError("No speech was detected in the voiceover.")
    return fine, "local word timestamps (faster-whisper)", []


def align_cues(script_tokens, cues, narration):
    mapping = token_mapping(script_tokens, [w["token"] for w in narration])
    coverage = len(mapping) / max(1, len(script_tokens))
    if coverage < .65:
        raise ValueError(f"Script/audio token match is only {coverage:.0%}; refusing to guess a whole timeline.")
    mapped = sorted(mapping)
    for cue in cues:
        a, b = cue["script_start"], cue["script_end"]
        matches = [i for i in range(a, b) if i in mapping]
        if len(matches) < min(3, b - a):
            # Short linked names can be misrecognized by ASR. Only interpolate
            # when both immediate surrounding script words are matched nearby.
            before, after = mapping.get(a-1), mapping.get(b)
            if b-a <= 3 and before is not None and after is not None:
                left, right = narration[before]['end'], narration[after]['start']
                if 0.04 <= right-left <= 4:
                    cue.update(start=round(left,3), end=round(right,3), match=0,
                               review=['Short linked phrase timing estimated between matched neighboring words; review this cut'])
                    continue
            cue.update(start=None, end=None, match=0, review=["Passage could not be aligned reliably"])
            continue
        first, last = matches[0], matches[-1]
        start, end = narration[mapping[first]]["start"], narration[mapping[last]]["end"]
        # Recover short unmatched boundary words using their neighbors, with an explicit review flag.
        review = []
        if first != a:
            idx = bisect.bisect_left(mapped, a)-1
            if idx >= 0:
                lower = mapped[idx]
                left = narration[mapping[lower]]["end"]
                start = left + (start-left) * (a-lower-1) / max(1, first-lower-1)
            review.append("Opening words estimated between recognized words")
        if last != b-1:
            idx = bisect.bisect_right(mapped, b-1)
            if idx < len(mapped):
                upper = mapped[idx]
                right = narration[mapping[upper]]["start"]
                end = end + (right-end) * (b-last-1) / max(1, upper-last-1)
            review.append("Ending words estimated between recognized words")
        ratio = len(matches) / (b-a)
        if ratio < .8:
            review.append("Script and narration wording differ; check this cue")
        cue.update(start=round(start, 3), end=round(end, 3), match=round(ratio, 3), review=review)
    groups = {}
    for cue in cues:
        if cue['start'] is not None:
            groups.setdefault((cue['script_start'], cue['script_end']), []).append(cue)
    for group in groups.values():
        if len(group) > 1:
            start, end = group[0]['start'], group[0]['end']
            for index, cue in enumerate(group):
                cue['start'] = round(start + (end-start)*index/len(group), 3)
                cue['end'] = round(start + (end-start)*(index+1)/len(group), 3)
                cue['review'].append('Adjacent image/video cues share this narration passage; time divided equally')
    return coverage


def sub(parent, tag, value=None, **attrs):
    e = ET.SubElement(parent, tag, **attrs)
    if value is not None:
        e.text = str(value)
    return e


def rate(parent):
    r = sub(parent, "rate")
    sub(r, "timebase", 30)
    sub(r, "ntsc", "TRUE")


def sample(parent, width, height):
    s = sub(parent, "samplecharacteristics")
    rate(s)
    sub(s, "width", width)
    sub(s, "height", height)
    sub(s, "anamorphic", "FALSE")
    sub(s, "pixelaspectratio", "square")
    sub(s, "fielddominance", "none")
    return s


def file_url(path):
    # Adobe's FCP7 importer understands file://localhost/C:/... URLs.
    return Path(path).resolve().as_uri().replace("file:///", "file://localhost/")


def motion(clip, scale):
    f = sub(clip, "filter")
    effect = sub(f, "effect")
    for name, value in [("name", "Basic Motion"), ("effectid", "basic"), ("effectcategory", "motion"),
                        ("effecttype", "motion"), ("mediatype", "video")]:
        sub(effect, name, value)
    parameter = sub(effect, "parameter")
    sub(parameter, "parameterid", "scale")
    sub(parameter, "name", "Scale")
    sub(parameter, "value", f"{scale:.6f}")


def xml_sequence(path, name, clips, gaps, cues, audio_path, duration, width, height, limit=None,
                 source_audio_enabled=False):
    frames = math.ceil(duration * FPS) if limit is None else min(round(limit * FPS), math.ceil(duration * FPS))
    root = ET.Element("xmeml", version="5")
    seq = sub(root, "sequence", id="sequence-1")
    sub(seq, "name", name)
    sub(seq, "duration", frames)
    rate(seq)
    tc = sub(seq, "timecode")
    rate(tc)
    sub(tc, "string", "00:00:00;00")
    sub(tc, "frame", 0)
    sub(tc, "displayformat", "DF")
    media = sub(seq, "media")
    video = sub(media, "video")
    sample(sub(video, "format"), width, height)

    source_audio = []
    for track_index, items in enumerate((gaps, clips)):
        track = sub(video, "track")
        for index, item in enumerate(items):
            start, end = item["start_frame"], min(item["end_frame"], frames)
            if start >= frames or end <= start:
                continue
            cid = f"clip-{track_index}-{index}"
            clip = sub(track, "clipitem", id=cid)
            is_video = item.get("kind") == "video"
            source_duration = item["source_duration_frames"] if is_video else frames
            source_in = item.get("in_frame", 0)
            sub(clip, "name", item["name"])
            sub(clip, "enabled", "TRUE")
            sub(clip, "duration", source_duration)
            rate(clip)
            for key, value in [("start", start), ("end", end), ("in", source_in), ("out", source_in+end-start)]:
                sub(clip, key, value)
            if not is_video:
                sub(clip, "stillframe", "TRUE")
            sub(clip, "anamorphic", "FALSE")
            sub(clip, "pixelaspectratio", "square")
            f = sub(clip, "file", id=f"file-{track_index}-{index}")
            sub(f, "name", Path(item["path"]).name)
            sub(f, "pathurl", file_url(item["path"]))
            rate(f)
            sub(f, "duration", source_duration)
            m = sub(f, "media")
            v = sub(m, "video")
            if not is_video:
                sub(v, "stillframe", "TRUE")
            sample(v, item["width"], item["height"])
            if is_video and item.get("has_audio"):
                am = sub(m, "audio")
                ac = sub(am, "samplecharacteristics")
                sub(ac, "depth", 16)
                sub(ac, "samplerate", 48000)
                sub(am, "channelcount", 2)
                source_audio.append((clip, item, start, end, cid, f.get("id"), track_index+1, index+1))
            motion(clip, min(width/item["width"], height/item["height"]) * 100)
            logging = sub(clip, "logginginfo")
            sub(logging, "description", item.get("passage", "Temporary guide; replace with footage"))
        sub(track, "enabled", "TRUE")
        sub(track, "locked", "FALSE")

    audio = sub(media, "audio")
    sub(audio, "channelcount", 2)
    audformat = sub(audio, "format")
    ac = sub(audformat, "samplecharacteristics")
    sub(ac, "depth", 16)
    sub(ac, "samplerate", 48000)
    if audio_path:
        track = sub(audio, "track")
        clip = sub(track, "clipitem", id="voiceover")
        sub(clip, "name", "Voiceover - continuous")
        sub(clip, "enabled", "TRUE")
        sub(clip, "duration", math.ceil(duration*FPS))
        rate(clip)
        for key, value in [("start", 0), ("end", frames), ("in", 0), ("out", frames)]:
            sub(clip, key, value)
        f = sub(clip, "file", id="voiceover-file")
        sub(f, "name", Path(audio_path).name)
        sub(f, "pathurl", file_url(audio_path))
        rate(f)
        sub(f, "duration", math.ceil(duration*FPS))
        am = sub(sub(f, "media"), "audio")
        ac = sub(am, "samplecharacteristics")
        sub(ac, "depth", 16)
        sub(ac, "samplerate", 48000)
        sub(am, "channelcount", 1)
        source = sub(clip, "sourcetrack")
        sub(source, "mediatype", "audio")
        sub(source, "trackindex", 1)
        sub(track, "enabled", "TRUE")
        sub(track, "locked", "FALSE")
    # Keep stereo source sound linked but disabled under narration. It is enabled
    # in the exact source-selects sequence where there is no competing voiceover.
    for channel in (1, 2):
        if not source_audio:
            break
        track = sub(audio, "track")
        for ai, (video_clip, item, start, end, cid, fid, vi, vc) in enumerate(source_audio):
            audio_id = f"{cid}-audio-{channel}"
            clip = sub(track, "clipitem", id=audio_id)
            sub(clip, "name", item["name"] + f" - source audio {channel}")
            sub(clip, "enabled", "TRUE" if source_audio_enabled else "FALSE")
            sub(clip, "duration", item["source_duration_frames"])
            rate(clip)
            for key, value in [("start", start), ("end", end), ("in", item["in_frame"]),
                               ("out", item["in_frame"]+end-start)]:
                sub(clip, key, value)
            sub(clip, "file", id=fid)
            source = sub(clip, "sourcetrack")
            sub(source, "mediatype", "audio")
            sub(source, "trackindex", channel)
            related = [(cid, "video", vi, vc),
                       (cid+"-audio-1", "audio", (1 if audio_path else 0)+1, ai+1),
                       (cid+"-audio-2", "audio", (1 if audio_path else 0)+2, ai+1)]
            owners = [clip, video_clip] if channel == 1 else [clip]
            for owner in owners:
                for linked_id, kind, ti, ci in related:
                    link = sub(owner, "link")
                    sub(link, "linkclipref", linked_id)
                    sub(link, "mediatype", kind)
                    sub(link, "trackindex", ti)
                    sub(link, "clipindex", ci)
                    if kind == "audio":
                        sub(link, "groupindex", 1)
        sub(track, "enabled", "TRUE")
        sub(track, "locked", "FALSE")
        sub(track, "outputchannelindex", channel)
    for cue in cues:
        if cue["start"] is None:
            continue
        start = round(cue["start"] * FPS)
        end = min(frames, max(start+1, round(cue["end"]*FPS)))
        if start >= frames:
            continue
        marker = sub(seq, "marker")
        label = " / ".join(r["label"] for r in cue["refs"])
        prefix = "IMAGE | "
        if cue["kind"] == "video":
            prefix = "VIDEO | " if all(r.get("video_asset") for r in cue["refs"]) else "VIDEO NEEDED | "
        sub(marker, "name", prefix + label)
        comment = cue["passage"] + "\n" + "\n".join(r["target"] for r in cue["refs"])
        if cue["review"]:
            comment += "\nREVIEW: " + "; ".join(cue["review"])
        sub(marker, "comment", comment)
        sub(marker, "in", start)
        sub(marker, "out", end)
    ET.ElementTree(root).write(str(path), xml_declaration=True, encoding="UTF-8",
                              pretty_print=True, doctype="<!DOCTYPE xmeml>")


def make_guide(path, width, height):
    image = Image.new("RGB", (width, height), "#151922")
    draw = ImageDraw.Draw(image)
    font_paths = []
    if sys.platform == 'win32':
        font_paths.append('C:/Windows/Fonts/segoeui.ttf')
    elif sys.platform == 'darwin':
        font_paths += ['/System/Library/Fonts/Helvetica.ttc',
                       '/System/Library/Fonts/SFNSText.ttf',
                       '/Library/Fonts/Arial.ttf']
    else:
        font_paths += ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                       '/usr/share/fonts/TTF/DejaVuSans.ttf']
    font, small = None, None
    for fp in font_paths:
        try:
            font = ImageFont.truetype(fp, round(height*.046))
            small = ImageFont.truetype(fp, round(height*.026))
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size=round(height*.046))
        small = ImageFont.load_default(size=round(height*.026))
    draw.text((width/2, height*.44), "VISUAL TO ADD", fill="#c7d1e1", font=font, anchor="mm")
    draw.text((width/2, height*.53), "Temporary guide • see sequence markers", fill="#7f8da3", font=small, anchor="mm")
    image.save(path)


def write_report(out, report, voiceover):
    esc = html.escape
    rows = []
    for cue in report["cues"]:
        label = " / ".join(r["label"] for r in cue["refs"])
        start = cue["start"]
        pictures = []
        for ref in cue["refs"]:
            if ref["asset"]:
                src = Path(ref["asset"]["path"]).as_uri()
                pictures.append(f'<img src="{esc(src)}" alt="{esc(ref["label"])}">')
        for ref in cue["refs"]:
            if ref.get("video_asset"):
                src = Path(ref["video_asset"]["path"]).as_uri()
                source_in = ref["in_frame"] / FPS
                source_out = ref["out_frame"] / FPS
                pictures.append(f'<div><video controls preload="metadata" src="{esc(src)}#t={source_in:.3f},{source_out:.3f}" style="width:210px;height:160px"></video><small>Source {esc(ref["label"])} · handles retained</small></div>')
        picture = '<div>' + ''.join(pictures) + '</div>' if pictures else '<div class="video">VIDEO NOT DOWNLOADED</div>'
        sources = " ".join(f'<a href="{esc(r["target"])}">{esc(r["label"])}</a>' for r in cue["refs"] if r["target"].startswith("https://"))
        status = "; ".join(cue["review"]) or "Matched; review cut by ear"
        button = f'<button data-time="{start}">{clock(start)} → {clock(cue["end"])}</button>' if start is not None else "Unmatched"
        rows.append(f'<article>{picture}<div><h2>{esc(label)}</h2>{button}<p>{esc(cue["passage"])}</p><small>{esc(status)} · text match {cue["match"]:.0%}</small><p>{sources}</p></div></article>')
    warnings = "".join(f"<li>{esc(w)}</li>" for w in report["warnings"])
    page = """<!doctype html><html><head><meta charset="utf-8"><title>Skeleton review</title>
<style>body{font:16px/1.5 system-ui;background:#10141d;color:#eef1f6;max-width:1080px;margin:36px auto;padding:0 24px}h1{font-size:36px;margin-bottom:6px}h2{font-size:20px;margin:0 0 10px}p{max-width:850px}a{color:#91bcff}header{position:sticky;top:0;background:#10141df5;padding:14px 0;z-index:1}audio{width:100%}article{display:grid;grid-template-columns:210px 1fr;gap:28px;padding:24px 0;border-bottom:1px solid #30394a}article img{width:210px;height:190px;object-fit:contain;background:#080a0f}small{color:#aeb9cb}button{background:#283d5b;color:white;border:0;padding:8px 12px;border-radius:6px;cursor:pointer}.video{display:grid;place-items:center;background:#1a2434;color:#90a3bf}li{margin:8px 0}</style></head><body>
<h1>Script → timeline</h1><p>Editable Premiere skeleton · 1920 × 1080 · 29.97 fps · approximate word boundaries</p>
<p>Import <b>Timeline/Skeleton_full.xml</b> into Premiere. V2 contains images and any prepared video clips; V1 contains removable guide cards in uncovered gaps; A1 contains narration. Prepared source audio is linked but disabled on A2/A3. <b>Timeline/Source_Selects.xml</b>, when present, contains exact requested video excerpts with source sound enabled. Click a timing to listen to the narration.</p>
<header><audio id="player" controls src="__VO__"></audio></header>
""" + f'<p>Timing: {esc(report["timing_method"])}.</p><ul>{warnings}</ul>' + "".join(rows) + """
<script>document.querySelectorAll('button[data-time]').forEach(b=>b.addEventListener('click',()=>{const a=document.getElementById('player');a.currentTime=Number(b.dataset.time);a.play()}));</script></body></html>"""
    page = page.replace("__VO__", Path(voiceover).resolve().as_uri())
    (out / "Review.html").write_text(page, encoding="utf-8")


def validate_xml(path):
    """Catch missing media and invalid edit ranges before offering an import file."""
    tree = ET.parse(str(path))
    sequence = tree.find("sequence")
    duration = int(sequence.findtext("duration"))
    for track in sequence.findall("media/video/track") + sequence.findall("media/audio/track"):
        previous_end = 0
        for clip in track.findall("clipitem"):
            start, end = int(clip.findtext("start")), int(clip.findtext("end"))
            if not (0 <= start < end <= duration and previous_end <= start):
                raise ValueError(f"Invalid clip range in {path}: {clip.findtext('name')}")
            if int(clip.findtext("out")) - int(clip.findtext("in")) != end-start:
                raise ValueError(f"Source and timeline durations differ in {path}")
            if int(clip.findtext("in")) < 0 or int(clip.findtext("out")) > int(clip.findtext("duration")):
                raise ValueError(f"Clip extends beyond available source media in {path}")
            previous_end = end
    for node in tree.findall(".//pathurl"):
        parsed = unquote(urlparse(node.text).path)
        # Windows file URLs begin /C:/; POSIX absolute paths keep their leading slash.
        if re.match(r"^/[A-Za-z]:/", parsed):
            parsed = parsed[1:]
        if not Path(parsed).is_file():
            raise ValueError(f"Timeline media is missing: {parsed}")


def build(docx, audio, out, words=None, width=1920, height=1080,
          download_videos=False, handles=600, full_videos=None, full_video_limit=5400,
          media_dir=None, audio_dir=None):
    out = Path(out).resolve()
    # Match the GUI/job_worker layout (Media/ and Audio/ as siblings of Timeline/, not nested
    # inside it) even when run standalone from the CLI without --media-dir/--audio-dir.
    media_dir = Path(media_dir).resolve() if media_dir else out.parent / "Media"
    audio_dir = Path(audio_dir).resolve() if audio_dir else out.parent / "Audio"
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"Output folder is not empty: {out}. Choose a new folder to preserve previous runs.")
    out.mkdir(parents=True, exist_ok=True)
    # Check if docx input is a Google Docs link
    str_docx = str(docx).strip()
    if is_google_doc_url(str_docx):
        print(f"Downloading Google Doc script: {str_docx}...", flush=True)
        inputs_dir = out / "inputs"
        docx_path = download_google_doc(str_docx, destination_dir=inputs_dir,
                                         progress=lambda m: print(m, flush=True))
        docx = str(docx_path)
    else:
        docx_path = Path(docx)

    # Validate inputs before starting the long build.
    if not docx_path.is_file():
        raise ValueError(f"Script file not found: {docx}")
    if docx_path.suffix.lower() != '.docx':
        raise ValueError(f"Expected a .docx file, got {docx_path.suffix!r}. RTF and .doc formats are not supported; export from Google Docs as DOCX.")
    try:
        from zipfile import is_zipfile
        if not is_zipfile(str(docx_path)):
            raise ValueError(f"File is not a valid DOCX (Word XML) document: {docx}")
    except Exception:
        raise ValueError(f"Cannot read {docx}; ensure it is a valid .docx file.")
    audio_path_check = Path(audio)
    if not audio_path_check.is_file():
        raise ValueError(f"Audio file not found: {audio}")
    print("Extracting bookmarked images and narration cues...", flush=True)
    script, cues, embedded, warnings = read_docx(docx, media_dir / "Images")
    narration, method, timing_warnings = timed_tokens(words, audio)
    warnings.extend(timing_warnings)
    coverage = align_cues(script, cues, narration)
    import av
    with av.open(str(audio)) as container:
        duration = container.duration / av.time_base
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav = audio_dir / "voiceover.wav"
    ffmpeg = ffmpeg_exe()
    subprocess.run([ffmpeg, "-v", "error", "-i", str(audio), "-ar", "48000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(wav)], check=True, **NO_WINDOW)
    guide = media_dir / "visual-to-add.png"
    make_guide(guide, width, height)
    frames = math.ceil(duration*FPS)
    clips, used = [], set()
    for cue in cues:
        if cue["start"] is None or cue["kind"] != "image":
            continue
        refs = cue["refs"]
        first, last = round(cue["start"] * FPS), min(frames, round(cue["end"] * FPS))
        for index, ref in enumerate(refs):
            if not ref["asset"]:
                continue
            asset = ref["asset"]
            start = first + round((last-first)*index/len(refs))
            end = first + round((last-first)*(index+1)/len(refs))
            if end <= start:
                warnings.append(f"Skipped zero-length cue {ref['label']}")
                continue
            label = ref["label"] if "IMG" in ref["label"].upper() else "IMG " + ref["label"]
            clips.append({**asset, "name": label, "start_frame": start, "end_frame": end,
                          "passage": cue["passage"], "source_url": ref["target"]})
            used.add(asset["path"])
            if (end-start) / FPS < 1.2:
                cue["review"].append(f"{label} is under 1.2 seconds; consider extending")
    video_assets, video_edits, video_selects = [], [], []
    if download_videos:
        from youtube_media import prepare_sources, place_video_clips
        video_assets, failures = prepare_sources(cues, media_dir / "Videos", handles, full_videos, full_limit=full_video_limit)
        warnings.extend(failures)
        video_edits, video_selects = place_video_clips(cues, clips, frames)
        clips.extend(video_edits)
    clips.sort(key=lambda c: c["start_frame"])
    # Bridge only tiny speech pauses. Preserve sentence end points across substantive gaps.
    for left, right in zip(clips, clips[1:]):
        gap = right["start_frame"] - left["end_frame"]
        if gap < 0:
            raise ValueError("Aligned image passages overlap; inspect cue mapping.")
        if left.get("kind") != "video" and 0 < gap <= round(.4 * FPS):
            left["end_frame"] = right["start_frame"]
    gaps, cursor = [], 0
    for clip in clips + [{"start_frame": frames, "end_frame": frames}]:
        if cursor < clip["start_frame"]:
            gaps.append({"name": "GUIDE - visual to add", "path": str(guide), "width": width, "height": height,
                         "start_frame": cursor, "end_frame": clip["start_frame"]})
        cursor = clip["end_frame"]
    unused = [a for a in embedded if a["path"] not in used]
    if unused:
        warnings.append(f"{len(unused)} embedded image(s) have no placed script cue; preserved in the image folder.")
    if any(c["start"] is None for c in cues):
        warnings.append("Some cues could not be aligned; see unmatched entries in this report.")
    # Timeline/ holds just the XML deliverables; supporting data and the human-facing
    # write-up live alongside it / at the project root so they're easy to tell apart.
    run_root = out.parent
    data_dir = out / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)
    report = {"duration_seconds": duration, "width": width, "height": height, "fps": "30000/1001",
              "timing_method": method, "script_token_match": round(coverage, 3),
              "sources": {"docx": str(Path(docx).resolve()), "audio": str(Path(audio).resolve())},
              "warnings": warnings, "cues": cues, "clips": clips, "gaps": gaps,
              "unused_assets": unused, "embedded_assets": embedded,
              "video_assets": video_assets, "video_selects": video_selects,
              "video_options": {"enabled": download_videos, "handles_seconds": handles, "full_sources": full_videos, "full_video_limit_seconds": full_video_limit}}
    (data_dir / "manifest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    with (data_dir / "image-timings.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Image", "Start seconds", "End seconds", "Start frame", "End frame", "Passage", "File", "Source"])
        for c in clips:
            if c.get("kind") == "video":
                continue
            writer.writerow([c["name"], round(c["start_frame"]/FPS, 3), round(c["end_frame"]/FPS, 3),
                             c["start_frame"], c["end_frame"], c["passage"], c["path"], c.get("source_url", "")])
    xml_sequence(out / "Skeleton_full.xml", "Script skeleton - full", clips, gaps, cues, wav, duration, width, height)
    xml_sequence(out / "Skeleton_test_45s.xml", "Script skeleton - first 45 seconds", clips, gaps, cues, wav, duration, width, height, 45)
    validate_xml(out / "Skeleton_full.xml")
    validate_xml(out / "Skeleton_test_45s.xml")
    if video_selects:
        selects_cues = [{"kind": "video", "start": c["start_frame"]/FPS, "end": c["end_frame"]/FPS,
                         "passage": c["passage"], "review": c.get("source_notes", []),
                         "refs": [{"label": c["name"], "target": c["source_url"], "video_asset": c}]}
                        for c in video_selects]
        xml_sequence(out / "Source_Selects.xml", "Source selects - exact YouTube excerpts", video_selects,
                     [], selects_cues, None, video_selects[-1]["end_frame"]/FPS, width, height,
                     source_audio_enabled=True)
        validate_xml(out / "Source_Selects.xml")
        with (data_dir / "video-timings.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Clip", "Timeline start seconds", "Timeline end seconds", "Requested YouTube start",
                             "Requested YouTube end", "Media in frame", "Media out frame", "Left handle seconds",
                             "Right handle seconds", "File"])
            for c in video_edits:
                media_out = c["in_frame"] + c["end_frame"]-c["start_frame"]
                writer.writerow([c["name"], round(c["start_frame"]/FPS,3), round(c["end_frame"]/FPS,3),
                                 c["requested_source_start"], c["requested_source_end"], c["in_frame"], media_out,
                                 round(c["in_frame"]/FPS,3), round((c["source_duration_frames"]-media_out)/FPS,3), c["path"]])
    write_report(run_root, report, wav)
    (run_root / "START HERE.txt").write_text(
        "PREMIERE SKELETON TEST\n\n1. In Premiere use File > Import and select Timeline/Skeleton_test_45s.xml.\n"
        "2. Open the imported sequence. Check image timing, fit and narration.\n"
        "3. Import Timeline/Skeleton_full.xml for the complete sequence.\n\n"
        "V2: editable images and prepared videos. V1: temporary guide cards in uncovered gaps. A1: narration.\n"
        "A2/A3: linked source-video sound, disabled so it does not compete with narration.\n"
        "Enable those audio clips in Premiere if source sound is wanted.\n"
        "Timeline/Source_Selects.xml (when present): exact full requested excerpts, source sound enabled.\n"
        "Video URLs and requested ranges are also in sequence markers. Failed downloads stay as markers.\n"
        "Main video placements start at the preceding paragraph/portion, play at normal speed, and\n"
        "are trimmed if too long for the passage. Shorter excerpts leave guide-card gaps.\n"
        "Extend video edges to use the retained handles; consult Timeline/Data/video-timings.csv and Review.html.\n"
        "The spoken title at the start has no linked visual and remains a guide card.\n"
        "Timing uses approximate local speech-recognition word boundaries; check the cuts.\n"
        "Review.html lets you audition each cue; Timeline/Data/manifest.json records every mapping and warning.\n"
        "Keep the Media folder. XML paths are absolute; if moving this package, relink media in Premiere.\n"
        "These XMLs have been structurally checked; import must still be tested in Premiere.\n", encoding="utf-8")
    print(f"Built {len(clips)-len(video_edits)} images, {len(video_edits)} video edits and {len(video_selects)} exact source selects.\n{run_root}", flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--docx", type=str, required=True, help="Path to local .docx file OR a Google Docs URL")
    p.add_argument("--audio", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--words", type=Path)
    p.add_argument("--media-dir", type=Path, help="Where images and downloaded videos are stored")
    p.add_argument("--audio-dir", type=Path, help="Where the prepared voiceover WAV is stored")
    p.add_argument("--whisper-model", default="small.en", help="faster-whisper model name")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--models-dir", type=Path, help="Where speech-recognition models are stored")
    p.add_argument("--download-videos", action="store_true")
    p.add_argument("--handles", type=float, default=600, help="Buffer seconds for videos above the full-video limit")
    p.add_argument("--full-video-limit", type=float, default=5400, help="Download complete videos up to this duration in seconds (default 90 minutes)")
    p.add_argument("--width", type=int, default=1920, help="Sequence width in pixels (default 1920)")
    p.add_argument("--height", type=int, default=1080, help="Sequence height in pixels (default 1080)")
    policy = p.add_mutually_exclusive_group()
    policy.add_argument("--full-videos", dest="full_videos", action="store_const", const=True, default=None, help="Override the duration limit and keep every video in full")
    policy.add_argument("--bounded-videos", dest="full_videos", action="store_const", const=False, help="Use buffered sections even for short videos")
    args = p.parse_args()
    if not args.words:
        from transcribe_words import transcribe
        key = hashlib.sha256(args.audio.read_bytes()).hexdigest()[:16]
        args.words = cache_dir() / f"{key}-words.json"
        transcribe(args.audio, args.words, args.whisper_model, args.device, args.models_dir)
    build(args.docx, args.audio, args.output, args.words,
          width=args.width, height=args.height,
          download_videos=args.download_videos, handles=args.handles, full_videos=args.full_videos, full_video_limit=args.full_video_limit,
          media_dir=args.media_dir, audio_dir=args.audio_dir)


if __name__ == "__main__":
    main()
