# Script to Premiere prototype

Converts a bookmarked Google Doc script and its continuous voiceover into an editable
FCP7 XML timeline for Premiere. Speech timing comes from local faster-whisper
(uses the NVIDIA GPU automatically when available); no Premiere transcript export is needed.

Open **Open Skeleton Builder.cmd** (Windows) or **Skeleton Builder.app** (Mac), then:

1. **Paths & Options** (once): projects folder, media download folder, Premiere Pro location.
2. **Build**: paste the Google Doc link, drop the voiceover (or paste a Google Drive link).
   **Preview cues** shows which script passages matched which image/video before running
   anything — no download or speech recognition, just the doc parsing, so it's instant.
3. Click **Build Skeleton**. If a build is already running, the new one is added to the
   **Queue** and starts automatically as soon as the current one finishes — submit several
   in a row without waiting. The first-run speech model download shows live progress
   ("Downloading speech model — 42% (320 MB / 760 MB)") instead of sitting silently.
   **Stop** cancels whichever run is currently active — there's no undo, but partial
   output stays on disk.
4. **Copy Skeleton Path & Open Premiere** launches Premiere and copies the XML path to your
   clipboard. Premiere Pro has no supported way to import an XML automatically from outside
   the app, so finish it there: File > Import (Ctrl/Cmd+I), then paste the path into the
   filename box and press Enter.

By default, `Projects`, `Models` and `Cache` folders are created next to the app itself
(alongside `SkeletonBuilder.exe`/`Premiere Skeleton Builder.app`, or the project root when
run from source) — nothing is written to Documents unless you point a location there
yourself. `Cache` holds regenerable working data (a script pasted as a Google Doc link,
cue-preview images, cached YouTube sources); only small app state (settings, the build
queue) stays in the OS profile folder mentioned below. Changing a location in
**Paths & Options** offers to move the existing files across; runs are picked up from
wherever `Projects folder` currently points.

Each run gets its own self-contained folder, `<projects folder>/<Google Doc title>/`, laid
out so the two files you actually read are right at the top and everything else is grouped
by what it's for:

```
<Google Doc title>/
├─ START HERE.txt      – what to do next, in order
├─ Review.html         – open in a browser to audition every cue against the audio
├─ run.json / run.log  – machine state and the full build log (also shown in the Runs tab)
├─ Script/              the .docx (downloaded, if it was a Google Doc link)
├─ Audio/                the voiceover, plus the converted voiceover.wav used in the XML
├─ Media/                everything the timeline points at
│  ├─ Images/            pictures extracted from the script or downloaded from the web
│  └─ Videos/            prepared/trimmed video clips
└─ Timeline/             the Premiere deliverables
   ├─ Skeleton_full.xml       – import this: the complete sequence
   ├─ Skeleton_test_45s.xml   – a quick first-45-seconds sanity check
   ├─ Source_Selects.xml      – exact requested video excerpts (only if videos were fetched)
   └─ Data/                    manifest.json + the timing CSVs (for scripting/inspection, not import)
```

Media defaults to living inside each project folder as shown above. Point **Media download
folder** (Paths & Options) at a folder of your own only if you'd rather keep media separately
— useful for putting large downloads on a bigger/faster drive — in which case it's organized
as `<that folder>/<project name>/` instead.

Runs continue independently when the window is closed; reopen the app to see progress.
(The queue itself only advances while the app is open — closing it pauses the queue, and
reopening resumes it, same as an in-progress run.)
Download and speech caches are reused. Keep the computer awake while processing.
If a run fails (e.g. a network hiccup while fetching the script or voiceover), select it in
**Runs** and use **Retry script & audio fetch** to try again with the same inputs.

## Packaged apps (no Python needed)

Download the zip for your computer from the GitHub **Actions** tab (workflow "Build apps"):

- **Windows:** unzip, run `SkeletonBuilder.exe` (keep it inside its folder).
- **macOS:** unzip, drag `Premiere Skeleton Builder.app` to Applications. It has no Apple
  Developer ID (that needs a paid account), so macOS still flags it as downloaded; the first time,
  right-click > Open (or `xattr -cr "/Applications/Premiere Skeleton Builder.app"`) instead of a
  plain double-click. If you instead see **"is damaged and can't be opened, move it to the Trash"**
  with no Open option at all, that's a stale build from before this app started ad-hoc signing the
  bundle - re-download the latest build, or run
  `xattr -cr "/Applications/Premiere Skeleton Builder.app"` in Terminal, which fixes it either way.
  One build covers both Apple Silicon and Intel Macs; on an Intel Mac it runs under Rosetta 2
  (macOS installs this automatically the first time it's needed).
  Actually drag the `.app` into `/Applications` with Finder before opening it — launching it
  straight from the unzipped Downloads folder can trigger macOS "App Translocation", which runs
  it from a randomized read-only path and can throw a "read only" error on first launch. The
  app now falls back to a writable folder automatically if this happens, but moving it to
  Applications first avoids it entirely.

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
