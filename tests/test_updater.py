import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import updater


class VersionCheckTests(unittest.TestCase):
    def _response(self, body_dict, headers=None):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body_dict).encode("utf-8")
        mock_resp.headers = headers or {}
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        return mock_resp

    def test_latest_release_parses_commit_and_matching_asset(self):
        release = {"body": "Automated build\ncommit:abc1234def5678\n",
                   "assets": [{"name": "SkeletonBuilder-Windows.zip", "browser_download_url": "https://x/win.zip", "size": 100},
                              {"name": "SkeletonBuilder-macOS.zip", "browser_download_url": "https://x/mac.zip", "size": 200}]}
        with patch("urllib.request.urlopen", return_value=self._response(release)):
            info = updater.latest_release()
        self.assertEqual(info["commit"], "abc1234def5678")
        self.assertEqual(info["asset_url"], f"https://x/{'win' if os.name == 'nt' else 'mac'}.zip")

    def test_latest_release_rejects_missing_commit_or_asset(self):
        with patch("urllib.request.urlopen", return_value=self._response({"body": "no commit line here", "assets": []})):
            with self.assertRaisesRegex(ValueError, "commit"):
                updater.latest_release()
        with patch("urllib.request.urlopen", return_value=self._response({"body": "commit:abc123", "assets": []})):
            with self.assertRaisesRegex(ValueError, "asset"):
                updater.latest_release()

    def test_update_available_reports_error_without_raising(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            info = updater.update_available()
        self.assertIn("error", info)
        self.assertIn("current", info)

    def test_update_available_true_only_when_commit_differs(self):
        release = {"body": "commit:newsha", "assets": [{"name": updater.ASSET_NAME, "browser_download_url": "https://x/a.zip", "size": 1}]}
        with patch.object(updater, "current_commit", return_value="oldsha"), \
             patch("urllib.request.urlopen", return_value=self._response(release)):
            info = updater.update_available()
        self.assertTrue(info["available"])
        self.assertEqual(info["latest"], "newsha")

        with patch.object(updater, "current_commit", return_value="newsha"), \
             patch("urllib.request.urlopen", return_value=self._response(release)):
            info = updater.update_available()
        self.assertFalse(info["available"])


class CurrentCommitTests(unittest.TestCase):
    def test_reads_bundled_version_file_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            version_file = Path(temp) / "VERSION.txt"
            version_file.write_text("deadbeef123\n")
            with patch.object(updater, "BUNDLE", Path(temp)):
                self.assertEqual(updater.current_commit(), "deadbeef123")

    def test_falls_back_to_git_when_not_frozen_and_no_version_file(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(updater, "BUNDLE", Path(temp)), patch.object(updater, "FROZEN", False):
                commit = updater.current_commit()
        self.assertTrue(commit == "unknown" or len(commit) == 40)


class StageWindowsHelperTests(unittest.TestCase):
    def test_stages_only_app_files_outside_install_dir(self):
        # The staged helper must never run out of install_dir itself (see updater.py's
        # module docstring for why - Windows demand-pages a running process's own code from
        # its backing files) and must never copy Projects/Media/etc, which can be large.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "SkeletonBuilder"
            (install_dir / "_internal").mkdir(parents=True)
            (install_dir / "_internal" / "a.dll").write_text("dll")
            (install_dir / "SkeletonBuilder.exe").write_text("exe")
            (install_dir / "Projects").mkdir()
            (install_dir / "Projects" / "user_data.txt").write_text("must not be copied")

            with patch.object(updater, "cache_dir", return_value=root / "cache"):
                helper_exe = updater._stage_windows_helper(install_dir)

            self.assertEqual(helper_exe, root / "cache" / "update_helper" / "SkeletonBuilder.exe")
            self.assertEqual(helper_exe.read_text(), "exe")
            self.assertEqual((helper_exe.parent / "_internal" / "a.dll").read_text(), "dll")
            self.assertFalse((helper_exe.parent / "Projects").exists())


class FinishUpdateTests(unittest.TestCase):
    def test_preserves_user_folders_and_replaces_app_files_windows_style(self):
        # Exercise the Windows branch of finish_update's file swap directly (skip the mac
        # ditto path, which needs a real .app bundle and the ditto binary - not available/
        # meaningful cross-platform in a unit test). Fakes old_pid as this test's own pid so
        # the wait-for-exit loop returns immediately.
        if os.name != "nt":
            self.skipTest("Exercises the Windows-specific swap path")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "SkeletonBuilder"
            (install_dir / "_internal").mkdir(parents=True)
            (install_dir / "_internal" / "old.dll").write_text("old")
            (install_dir / "SkeletonBuilder.exe").write_text("old exe")
            for name in ("Projects", "Media", "Models", "Cache", "Temp"):
                folder = install_dir / name
                folder.mkdir()
                (folder / "user_data.txt").write_text("must survive")

            update_src = root / "update_src" / "SkeletonBuilder"
            (update_src / "_internal").mkdir(parents=True)
            (update_src / "_internal" / "new.dll").write_text("new")
            (update_src / "SkeletonBuilder.exe").write_text("new exe")
            for name in ("Projects", "Media", "Models", "Cache", "Temp"):
                (update_src / name).mkdir()

            from zipfile import ZipFile
            zip_path = root / "update.zip"
            with ZipFile(zip_path, "w") as zf:
                for path in update_src.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(root / "update_src"))

            # A PID that has genuinely already exited (not this test's own, which is alive
            # for the whole test) - spawn a trivial process and wait for it to finish.
            import subprocess
            dead = subprocess.Popen([os.environ.get("COMSPEC", "cmd.exe"), "/c", "exit"])
            dead.wait()
            with patch("subprocess.Popen"):
                ok = updater.finish_update(zip_path, install_dir, dead.pid, wait_timeout=2)

            self.assertTrue(ok)
            for name in ("Projects", "Media", "Models", "Cache", "Temp"):
                self.assertEqual((install_dir / name / "user_data.txt").read_text(), "must survive")
            self.assertEqual((install_dir / "SkeletonBuilder.exe").read_text(), "new exe")
            self.assertEqual((install_dir / "_internal" / "new.dll").read_text(), "new")
            self.assertFalse((install_dir / "_internal" / "old.dll").exists())


if __name__ == "__main__":
    unittest.main()
