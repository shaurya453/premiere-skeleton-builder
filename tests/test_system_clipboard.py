import subprocess
import sys
import unittest
from unittest import mock

import system_clipboard


class CopyTextTests(unittest.TestCase):
    def test_unconfirmed_system_copy_falls_back_and_reports_failure(self):
        seen = []
        with mock.patch.object(system_clipboard.sys, "platform", "win32"), \
                mock.patch.object(system_clipboard, "_windows_copy", return_value=True), \
                mock.patch.object(system_clipboard, "_windows_read", return_value=""):
            self.assertFalse(system_clipboard.copy_text("C:/x.xml", fallback=seen.append))
        self.assertEqual(seen, ["C:/x.xml"])  # still tried Tk's clipboard

    def test_confirmed_system_copy_skips_the_fallback(self):
        seen = []
        with mock.patch.object(system_clipboard.sys, "platform", "win32"), \
                mock.patch.object(system_clipboard, "_windows_copy", return_value=True), \
                mock.patch.object(system_clipboard, "_windows_read", return_value="C:/x.xml"):
            self.assertTrue(system_clipboard.copy_text("C:/x.xml", fallback=seen.append))
        self.assertEqual(seen, [])

    def test_a_crashing_system_route_or_fallback_never_raises(self):
        def boom(*_):
            raise OSError("no clipboard")
        with mock.patch.object(system_clipboard.sys, "platform", "darwin"), \
                mock.patch.object(system_clipboard, "_mac_copy", side_effect=boom):
            self.assertFalse(system_clipboard.copy_text("x", fallback=boom))

    @unittest.skipUnless(sys.platform == "win32", "Windows clipboard")
    def test_text_is_on_the_real_clipboard_for_other_programs_immediately(self):
        # The old Tk route handed text over lazily, so a program reading while this one was
        # busy got nothing. Here the reader is another process and this one never pumps events.
        path = "C:/Users/Example User/Ünï Run/Timeline/Skeleton_full.xml"
        try:
            self.assertTrue(system_clipboard.copy_text(path))
        except AssertionError:
            self.skipTest("clipboard unavailable in this session")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.stdout.strip(), path)


if __name__ == "__main__":
    unittest.main()
