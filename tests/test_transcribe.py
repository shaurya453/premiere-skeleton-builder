import tempfile
import unittest
from unittest.mock import patch

from transcribe_words import ensure_model_downloaded


class ModelDownloadProgressTests(unittest.TestCase):
    """These stay offline on purpose (no real Hugging Face download in CI); the real network
    path (small model, real progress lines, real WhisperModel load) was verified manually —
    see the PR/commit description."""

    def test_an_already_local_directory_is_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as model_dir:
            self.assertEqual(ensure_model_downloaded(model_dir, "unused"), model_dir)

    def test_any_failure_falls_back_to_the_bare_model_name(self):
        # faster_whisper.WhisperModel downloads a bare model name itself (silently, but
        # reliably) if we hand it back unchanged, so any failure here must be non-fatal.
        with patch("huggingface_hub.snapshot_download", side_effect=RuntimeError("offline")), \
             patch("huggingface_hub.HfApi.model_info", side_effect=RuntimeError("offline")):
            self.assertEqual(ensure_model_downloaded("tiny.en", "/nonexistent"), "tiny.en")

    def test_progress_is_reported_and_reaches_100_percent(self):
        reported = []

        class FakeSibling:
            def __init__(self, name, size):
                self.rfilename, self.size = name, size

        class FakeInfo:
            siblings = [FakeSibling("model.bin", 1_000_000)]

        def fake_snapshot_download(repo_id, allow_patterns=None, cache_dir=None,
                                   local_files_only=False, tqdm_class=None):
            if local_files_only:
                raise RuntimeError("not cached yet")
            bar = tqdm_class(total=1_000_000, unit="B", desc="Downloading bytes")
            bar.update(400_000)
            bar.update(600_000)
            bar.close()
            return "/fake/model/dir"

        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download), \
             patch("huggingface_hub.HfApi.model_info", return_value=FakeInfo()):
            result = ensure_model_downloaded("tiny.en", "/tmp/models",
                                             progress=lambda pct, done, total: reported.append(pct))
        self.assertEqual(result, "/fake/model/dir")
        self.assertEqual(reported[-1], 100)
        self.assertEqual(reported, sorted(reported))  # monotonic, no regressions


if __name__ == "__main__":
    unittest.main()
