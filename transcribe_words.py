"""Local word timing refinement; no audio is sent to a service."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from paths import cache_dir


def ensure_model_downloaded(model_name, download_root, progress=None):
    """Download a faster-whisper model with visible progress, reported via `progress(pct,
    done_bytes, total_bytes)`. faster_whisper's own downloader hard-disables tqdm (passes
    tqdm_class=disabled_tqdm internally), so on a slow first run there is otherwise no
    feedback at all for several minutes. Returns the local model directory to hand to
    WhisperModel (which then skips its own downloader), or the original model_name
    unchanged if anything here fails — WhisperModel then falls back to downloading it
    itself, silently but reliably.
    """
    if os.path.isdir(model_name):
        return model_name
    try:
        import fnmatch
        import huggingface_hub
        from faster_whisper.utils import _MODELS
        from tqdm import tqdm
        repo_id = _MODELS.get(model_name, model_name)
        patterns = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
        cache_root = str(download_root)
        Path(cache_root).mkdir(parents=True, exist_ok=True)
        try:  # already fully cached: skip the metadata round-trip and go straight to the path
            return huggingface_hub.snapshot_download(repo_id, allow_patterns=patterns,
                                                       cache_dir=cache_root, local_files_only=True)
        except Exception:
            pass
        info = huggingface_hub.HfApi().model_info(repo_id, files_metadata=True)
        total_bytes = sum(s.size for s in info.siblings if s.size and
                          any(fnmatch.fnmatch(s.rfilename, p) for p in patterns))
        report = progress or (lambda pct, done, total: None)

        class ProgressTqdm(tqdm):
            """Real tqdm subclass (huggingface_hub pokes .total/.n directly), with its own
            terminal rendering suppressed and progress relayed via `report` instead. Bytes
            are summed across concurrent per-file bars against a size precomputed up front,
            so percentage is monotonic instead of dipping as new files start mid-download."""
            _done = {}
            _last_pct = [-1]

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._id = id(self)

            def display(self, *args, **kwargs):
                pass

            def update(self, n=1):
                result = super().update(n)
                if total_bytes and self.unit == "B" and (self.desc or "").lower().startswith("download"):
                    ProgressTqdm._done[self._id] = self.n
                    done = sum(ProgressTqdm._done.values())
                    pct = min(100, round(done * 100 / total_bytes))
                    if pct != ProgressTqdm._last_pct[0]:
                        ProgressTqdm._last_pct[0] = pct
                        report(pct, done, total_bytes)
                return result

            def close(self):
                ProgressTqdm._done.pop(self._id, None)
                super().close()

        return huggingface_hub.snapshot_download(repo_id, allow_patterns=patterns,
                                                  cache_dir=cache_root, tqdm_class=ProgressTqdm)
    except Exception:
        return model_name


def pick_device(device="auto"):
    """Return (device, compute_type); auto uses the GPU when CTranslate2 can see one."""
    if device in ("auto", "cuda"):
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:
            pass
        if device == "cuda":
            raise RuntimeError("No CUDA GPU available for speech recognition.")
    return "cpu", "int8"


def transcribe(audio, destination, model_name="small.en", device="auto", models_dir=None):
    from faster_whisper import WhisperModel
    audio, destination = Path(audio), Path(destination)
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    if destination.exists():
        cached = json.loads(destination.read_text(encoding="utf-8"))
        if cached.get("audio_sha256") == digest and cached.get("model") == model_name:
            print("Using cached word timings.", flush=True)
            return cached
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    print(f"Loading {model_name} locally (first run downloads the model)...", flush=True)
    dev, compute = pick_device(device)
    root = str(models_dir or cache_dir() / "models")
    Path(root).mkdir(parents=True, exist_ok=True)
    model_path = ensure_model_downloaded(model_name, root,
        progress=lambda pct, done, total: print(f"MODEL_DL {pct} {done} {total}", flush=True))
    try:
        model = WhisperModel(model_path, device=dev, compute_type=compute, cpu_threads=6, download_root=root)
    except Exception as error:
        if dev == "cpu":
            raise
        print(f"GPU unavailable ({error}); falling back to CPU.", flush=True)
        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=6, download_root=root)
    def recognise(active):
        segments, info = active.transcribe(str(audio), language="en", beam_size=5,
                                           word_timestamps=True, vad_filter=True)
        found = []
        for s in segments:
            found.extend({"word": w.word, "start": w.start, "end": w.end,
                          "probability": w.probability} for w in s.words or [])
            print(f"Timed through {s.end:.1f}s", flush=True)
        return found, info

    try:
        words, info = recognise(model)
    except Exception as error:
        if dev == "cpu":
            raise
        # CUDA libraries can be missing until the first real inference; retry on the CPU.
        print(f"GPU failed during recognition ({error}); retrying on CPU.", flush=True)
        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=6, download_root=root)
        words, info = recognise(model)
    result = {"audio_sha256": digest, "model": model_name, "duration": info.duration,
              "method": "local ASR word timestamps; review approximate boundaries", "words": words}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("output")
    p.add_argument("--model", default="small.en")
    p.add_argument("--device", default="auto")
    p.add_argument("--models-dir")
    a = p.parse_args()
    transcribe(a.audio, a.output, a.model, a.device, a.models_dir)
