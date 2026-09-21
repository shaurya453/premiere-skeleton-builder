# Development handoff

## Current behavior

This is a local Python/Tk desktop app that turns a bookmarked DOCX script, Premiere transcript CSV, and continuous voiceover into an editable FCP7 XML timeline. No paid API is used. It generates Skeleton_full.xml, a 45-second test sequence, Source_Selects.xml, Review.html, timing CSVs, and a manifest.

Recent changes:

- Complete YouTube source videos retained up to 90 minutes. Longer sources use a continuous window from 10 minutes before the earliest selection through 10 minutes after the latest selection. Metadata is checked before downloading; partial downloads and complete downloads have separate cache identities.
- Persistent desktop run history and logs. A detached supervisor continues new builds after the window closes. Runs live under output/, with run.json, run.log, and result/ in each new run directory. Previous flat output folders are still recognized.
- Image bookmark links can use ordinary phrases rather than IMG labels.
- Visible timestamp ranges are matched to any overlapping YouTube hyperlink, including ranges split across several hyperlink/text runs. Duplicate parts pointing to the same video become one selection. Missing or conflicting links are flagged, not guessed.
- Short misrecognized names may be timed between immediate matched neighboring words within four seconds, explicitly flagged for review. Adjacent image/video cues share their narration passage and are also flagged.
- A Mac launcher installer creates an app for the current checkout location; no machine-specific app binary is committed.

## Where to work

- app.py: desktop controls, run history, folder opening.
- jobs.py / job_worker.py: persistent state, background process and logs.
- skeleton_builder.py: DOCX parsing, transcript/audio alignment, XML generation.
- youtube_media.py: YouTube selection, caching, source preparation and clip placement.
- transcribe_words.py: local faster-whisper word timing.
- tests/: parser, timing, XML, download policy and job-lifecycle tests.

See README.md for setup. Run `python -m unittest discover -s tests -v` inside the project's virtual environment. Input documents, voiceovers, generated output, downloaded video, and models are not in Git; transfer test inputs separately. Rebuild on each computer so XML paths point to that computer's media.

## Validation and remaining work

21 automated tests passed on the source snapshot before handoff. Real YouTube downloads and a partial-download test were run on macOS. An earlier generated timeline was imported and visually spot-checked in Premiere. The latest parser repair was checked through XML validation and actual sample inputs; the rebuilt timeline was not re-imported during that repair.

The FNAF sample rebuilt with 23 image placements, 25 video placements in the main edit, and 26 complete requested selections in Source_Selects.xml. Full source selections may be longer than narration; the main edit trims or leaves guide-card gaps while Source_Selects preserves available ranges.

Known limitations and next checks:

- Editorial pacing and acoustic cut boundaries still require human review. This is an editable skeleton, not a finished video.
- A single timestamp without an end cannot define a selection. A timestamp linked to an image bookmark cannot identify a YouTube source. These are reported in warnings.
- Four embedded images in the last FNAF example were not placed by a recognized script cue; they remain in media/images.
- Input parsing assumes English narration, parenthetical editorial notes, embedded images, and Google Docs-style bookmarks. Broader document formats need regression fixtures.
- Job continuation survives closing the interface, not machine shutdown. Interrupted runs require a fresh run; downloads/word timings are cached, but prepared video conversion is normally repeated.
- Long full-video conversion can take substantial time. Progress currently includes logs plus the latest media filename/size; more detailed conversion progress and cancellation would be useful.
- Windows process/locking paths are implemented but this revision needs a real Windows end-to-end test.
- This snapshot was prepared from the original repository ZIP, so development changes were not made in a local Git clone. Use a fresh clone for further collaboration.
