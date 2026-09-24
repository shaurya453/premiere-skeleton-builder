import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

import web_media
from skeleton_builder import visual_links


def image_bytes(fmt, size=(40, 20)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, fmt)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {
            "/pic.png": ("image/png", image_bytes("PNG")),
            "/pic.webp": ("image/webp", image_bytes("WEBP")),
            "/page": ("text/html", b'<html><head><meta property="og:image" content="/pic.png"></head></html>'),
            "/bare": ("text/html", b"<html><body>no image</body></html>"),
            "/notimage.jpg": ("image/jpeg", b"definitely not an image"),
        }
        if self.path not in routes:
            self.send_response(404)
            self.end_headers()
            return
        kind, body = routes[self.path]
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class WebImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def fetch(self, path):
        with tempfile.TemporaryDirectory() as temp:
            return web_media.fetch_image(self.base + path, temp, progress=lambda m: None)

    def test_direct_png_is_kept_and_webp_converted_to_png(self):
        png = self.fetch("/pic.png")
        self.assertTrue(png["path"].endswith(".png"))
        self.assertEqual((png["width"], png["height"]), (40, 20))
        webp = self.fetch("/pic.webp")
        self.assertTrue(webp["path"].endswith(".png"))  # Premiere cannot import WebP

    def test_page_link_uses_og_image(self):
        self.assertEqual(self.fetch("/page")["width"], 40)

    def test_failures_are_explained(self):
        for path, message in [("/bare", "og:image"), ("/missing.png", "404"), ("/notimage.jpg", "readable")]:
            with self.assertRaisesRegex(Exception, message):
                self.fetch(path)


class ClassificationTests(unittest.TestCase):
    def test_link_kinds(self):
        self.assertEqual(web_media.identify_source("https://youtu.be/fftGair1ZoA")[:2], ("youtube", "fftGair1ZoA"))
        self.assertEqual(web_media.identify_source("https://vimeo.com/12345")[0], "site")
        self.assertEqual(web_media.identify_source("https://example.com/clips/a.MP4?x=1")[0], "file")
        kind, key, _ = web_media.identify_source("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view")
        self.assertEqual((kind, key), ("drive", "drive_1AbCdEfGhIjKlMnO"))
        with self.assertRaises(ValueError):
            web_media.identify_source("ftp://example.com/a.mp4")

    def test_google_redirect_wrapper_is_unwrapped(self):
        wrapped = "https://www.google.com/url?q=https://vimeo.com/12345&sa=D"
        self.assertEqual(web_media.identify_source(wrapped)[2], "https://vimeo.com/12345")
        self.assertTrue(web_media.is_direct_image_url("https://x.org/a/b.JPG?w=200"))
        self.assertFalse(web_media.is_direct_image_url("https://x.org/article"))


class VisualLinkTests(unittest.TestCase):
    def test_web_images_and_other_site_videos_become_cues(self):
        raw = "Look at this photo and then vimeo 1:00-1:20 or IMG 2 later"
        def span(text):
            i = raw.index(text)
            return i, i + len(text)
        links = [
            {"start": span("photo")[0], "end": span("photo")[1], "target": "https://x.org/cat.jpg"},
            {"start": span("vimeo 1:00-1:20")[0], "end": span("vimeo 1:00-1:20")[1], "target": "https://vimeo.com/12345"},
            {"start": span("IMG 2")[0], "end": span("IMG 2")[1], "target": "https://x.org/article"},
        ]
        fetched = []
        def fetch(url):
            fetched.append(url)
            return {"path": "p", "width": 1, "height": 1}
        warnings = []
        found = visual_links(raw, links, {}, warnings, fetch)
        self.assertEqual([l["kind"] for l in found], ["image", "video", "image"])
        self.assertEqual(fetched, ["https://x.org/cat.jpg", "https://x.org/article"])
        self.assertEqual(warnings, [])
        # Inspection mode never touches the network.
        inspected = visual_links(raw, links, {}, [], None)
        self.assertTrue(all(l["kind"] in ("image", "video") for l in inspected))
        self.assertEqual(inspected[0]["asset"], {"path": None, "remote": "https://x.org/cat.jpg"})

    def test_failed_download_is_reported_not_fatal(self):
        raw = "a photo here"
        links = [{"start": 2, "end": 7, "target": "https://x.org/cat.jpg"}]
        warnings = []
        def fail(url):
            raise RuntimeError("HTTP 403")
        found = visual_links(raw, links, {}, warnings, fail)
        self.assertEqual(found[0]["asset"], None)
        self.assertIn("HTTP 403", warnings[0])

    def test_drive_link_without_img_label_is_recognized_as_image(self):
        # A Drive share link is a common way to link an image and shouldn't need the "IMG"
        # label workaround other unlabelled page links require.
        raw = "Look at this drive picture here"
        target = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890/view"
        links = [{"start": raw.index("drive picture"), "end": raw.index("drive picture") + len("drive picture"), "target": target}]
        fetched = []
        found = visual_links(raw, links, {}, [], lambda url: fetched.append(url) or {"path": "p", "width": 1, "height": 1})
        self.assertEqual(fetched, [target])
        self.assertEqual(found[0]["kind"], "image")

    def test_unrecognized_http_link_warns_instead_of_vanishing(self):
        # A plain http(s) link that isn't a video, a recognized image, or a bookmark used to
        # vanish from the output with zero trace. It should at least produce a warning now.
        raw = "check this out here"
        links = [{"start": raw.index("this out"), "end": raw.index("this out") + len("this out"), "target": "https://example.com/some-page"}]
        warnings = []
        found = visual_links(raw, links, {}, warnings, None)
        self.assertEqual(found, [])
        self.assertTrue(any("Unrecognized link" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
