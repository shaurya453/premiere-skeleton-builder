"""Local word timing refinement; no audio is sent to a service."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from paths import cache_dir


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
    try:
        model = WhisperModel(model_name, device=dev, compute_type=compute, cpu_threads=6, download_root=root)
    except Exception as error:
        if dev == "cpu":
            raise
        print(f"GPU unavailable ({error}); falling back to CPU.", flush=True)
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=6, download_root=root)
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
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=6, download_root=root)
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
