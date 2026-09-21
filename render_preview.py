"""Render a small proof video from a generated manifest, independently of Premiere."""
import argparse
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageOps
from skeleton_builder import FPS


def render(folder, duration=45):
    folder = Path(folder).resolve()
    report = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    frames = min(round(duration*FPS), round(report["duration_seconds"]*FPS))
    output = folder / "Preview_first_45s.mp4"
    process = subprocess.Popen([
        "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "960x540",
        "-r", "30000/1001", "-i", "pipe:0", "-i", str(folder/"media"/"voiceover.wav"),
        "-frames:v", str(frames), "-t", str(frames/FPS), "-c:v", "libx264", "-preset", "fast",
        "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output)], stdin=subprocess.PIPE)
    try:
        items = sorted(report["clips"] + report["gaps"], key=lambda c: c["start_frame"])
        for item in items:
            start, end = item["start_frame"], min(frames, item["end_frame"])
            if start >= frames:
                break
            if item.get("kind") == "video":
                decoder = subprocess.Popen([
                    "ffmpeg", "-v", "error", "-ss", str(item["in_frame"]/FPS), "-i", item["path"],
                    "-an", "-frames:v", str(end-start), "-vf",
                    "scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], stdout=subprocess.PIPE)
                try:
                    for _ in range(end-start):
                        pixels = decoder.stdout.read(960*540*3)
                        if len(pixels) != 960*540*3:
                            raise RuntimeError(f"Video source ended early: {item['name']}")
                        process.stdin.write(pixels)
                    decoder.stdout.close()
                    if decoder.wait() != 0:
                        raise RuntimeError("Video preview decoding failed")
                finally:
                    if decoder.poll() is None:
                        decoder.kill()
                continue
            with Image.open(item["path"]) as source:
                image = ImageOps.contain(source.convert("RGB"), (960, 540))
                canvas = Image.new("RGB", (960, 540))
                canvas.paste(image, ((960-image.width)//2, (540-image.height)//2))
                pixels = canvas.tobytes()
            for _ in range(end-start):
                process.stdin.write(pixels)
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Preview rendering failed")
    finally:
        if process.poll() is None:
            process.kill()
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    args = parser.parse_args()
    render(args.folder)
