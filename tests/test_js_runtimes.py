import unittest

from youtube_media import js_runtimes_option


class JsRuntimesOptionTests(unittest.TestCase):
    """The option is handed to the real yt-dlp, whose parser rejects the wrong shape outright."""

    def test_yt_dlp_accepts_the_option_and_sees_the_node_path(self):
        from yt_dlp import YoutubeDL
        node = r"C:\tools\node.exe"
        with YoutubeDL({"quiet": True, "js_runtimes": js_runtimes_option(node)}) as ydl:
            runtime = ydl._js_runtimes["node"]
            self.assertIsNotNone(runtime)
            self.assertEqual(list(ydl.params["js_runtimes"]), ["node"])
            self.assertEqual(ydl.params["js_runtimes"]["node"]["path"], node)

    def test_the_command_line_string_form_is_rejected_by_yt_dlp(self):
        # This is what the app used to pass; keeping the check so a yt-dlp change that makes it valid
        # again is noticed rather than silently relied upon.
        from yt_dlp import YoutubeDL
        with self.assertRaisesRegex(ValueError, "Invalid js_runtimes format"):
            YoutubeDL({"quiet": True, "js_runtimes": "node:/usr/bin/node"})


if __name__ == "__main__":
    unittest.main()
