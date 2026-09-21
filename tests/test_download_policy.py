import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from youtube_media import download_source, source_window


class DownloadPolicyTests(unittest.TestCase):
    def test_cutoff_and_boundary_buffers(self):
        self.assertEqual(source_window([(135, 141)], 900), (0, 900))
        self.assertEqual(source_window([(135, 141)], 5400), (0, 5400))
        self.assertEqual(source_window([(1800, 1810)], 5401), (1200, 2410))
        self.assertEqual(source_window([(10, 20)], 7200), (0, 620))
        self.assertEqual(source_window([(7100, 7190)], 7200), (6500, 7200))
        self.assertEqual(source_window([(1800, 1810), (2400, 2420)], 7200), (1200, 3020))
        self.assertEqual(source_window([(1800, 1810)], 7200, full=True), (0, 7200))

    def test_long_video_requests_section_without_downloading_full_source(self):
        calls = []
        class FakeYDL:
            def __init__(self, options): self.options = options
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                calls.append((download, self.options))
                if download:
                    path = self.options['outtmpl'].replace('%(ext)s', 'mp4')
                    Path(path).write_bytes(b'test')
                return {'id': 'fftGair1ZoA', 'title': 'Long video', 'duration': 7200}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with patch('yt_dlp.YoutubeDL', FakeYDL), patch('youtube_media.probe', return_value={'duration': 1210}):
                path, data = download_source('fftGair1ZoA', cache, [(1800, 1810)])
                self.assertEqual([c[0] for c in calls], [False, True])
                self.assertEqual(calls[1][1]['download_ranges']({}, None), [{'start_time': 1200, 'end_time': 2410}])
                self.assertEqual(data['download_offset_seconds'], 1200)
                self.assertEqual(data['duration'], 7200)
                self.assertFalse((cache/'fftGair1ZoA.mp4').exists())
                # A cached partial must not be treated as a complete video.
                calls.clear()
                download_source('fftGair1ZoA', cache, [(1800, 1810)])
                self.assertEqual([c[0] for c in calls], [False])

    def test_unknown_duration_rejected_before_download(self):
        calls = []
        class FakeYDL:
            def __init__(self, options): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                calls.append(download)
                return {'duration': None}
        with tempfile.TemporaryDirectory() as tmp, patch('yt_dlp.YoutubeDL', FakeYDL):
            with self.assertRaises(ValueError):
                download_source('fftGair1ZoA', Path(tmp), [(5, 10)])
        self.assertEqual(calls, [False])
