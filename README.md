# Script to Premiere prototype

Converts a bookmarked Google Doc script and its continuous voiceover into an editable
FCP7 XML timeline for Premiere. Speech timing comes from local faster-whisper
(uses the NVIDIA GPU automatically when available); no Premiere transcript export is needed.

Open **Open Skeleton Builder.cmd** (Windows) or **Skeleton Builder.app** (Mac), then:

1. **Paths & Options** (once): projects folder, media download folder, Premiere Pro location.
2. **Build**: paste the Google Doc link, drop the voiceover (or paste a Google Drive link), click **Build Skeleton**.
3. **Run Premiere Pro with Skeleton** opens the finished timeline in Premiere.

Each run creates `<projects folder>/<Google Doc title>/` with `Script/`, `Audio/`,
`Timeline/` (the XML files), `Media/` (images and downloaded videos, or
`<media folder>/<title>/` when a media folder is set), plus `run.json` and `run.log`.
Runs continue independently when the window is closed; reopen the app to see progress.
Download and speech caches are reused. Keep the computer awake while processing.

## Packaged apps (no Python needed)

Download the zip for your computer from the GitHub **Actions** tab (workflow "Build apps"):

- **Windows:** unzip, run `SkeletonBuilder.exe` (keep it inside its folder).
- **macOS:** unzip, drag `Premiere Skeleton Builder.app` to Applications. It is not signed, so the
  first time use right-click > Open (or `xattr -cr "/Applications/Premiere Skeleton Builder.app"`).
  Use the Apple Silicon build on M-series Macs and the Intel build on older Macs.

They include FFmpeg, speech recognition and the YouTube/web downloaders. The speech model
(0.5 GB for the default) downloads on first use into the Speech model folder. On Windows with an
NVIDIA card the app uses the GPU when the CUDA libraries are installed, otherwise the CPU; Macs use the CPU.
Settings and caches are stored in `%LOCALAPPDATA%\Premiere Skeleton Builder` (Windows) or
`~/Library/Application Support/Premiere Skeleton Builder` (macOS).

To build locally: `pip install -r requirements.txt pyinstaller`, then `python build_app.py`
(add `--node <path to node>` to bundle Node.js for YouTube). Build on each OS you target.

## Running from source

Python 3.12 with Tkinter (on macOS: `brew install python@3.12 python-tk@3.12`):

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     (Windows)
.venv/bin/python -m pip install -r requirements.txt         (macOS)
.venv/Scripts/python app.py                                 (or Open Skeleton Builder.cmd / .command)
```

FFmpeg comes from the `imageio-ffmpeg` package; install Node.js separately for YouTube downloads.
