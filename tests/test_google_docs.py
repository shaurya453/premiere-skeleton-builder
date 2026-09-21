import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import zipfile

from google_docs import (
    extract_google_doc_id,
    is_google_doc_url,
    download_google_doc,
    _sanitize_filename,
    _extract_filename_from_headers,
)


class GoogleDocsTests(unittest.TestCase):
    def test_extract_doc_id(self):
        urls = [
            "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
            "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit?usp=sharing",
            "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
            "https://docs.google.com/document/u/1/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
            "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
        ]
        for url in urls:
            self.assertEqual(
                extract_google_doc_id(url),
                "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
                f"Failed on {url}",
            )
            self.assertTrue(is_google_doc_url(url))

        self.assertIsNone(extract_google_doc_id("https://google.com/search?q=test"))
        self.assertFalse(is_google_doc_url("https://google.com/search?q=test"))
        self.assertFalse(is_google_doc_url("C:/path/to/script.docx"))

    def test_sanitize_filename(self):
        self.assertEqual(_sanitize_filename("Script: Part 1/2?"), "Script_ Part 1_2_.docx")
        self.assertEqual(_sanitize_filename("my_script.docx"), "my_script.docx")

    def test_extract_filename_from_headers(self):
        headers = {"Content-Disposition": 'attachment; filename="FNAF Episode 1.docx"'}
        self.assertEqual(_extract_filename_from_headers(headers, "fallback.docx"), "FNAF Episode 1.docx")

        headers_utf8 = {"Content-Disposition": "attachment; filename*=UTF-8''Story%20Part%201.docx"}
        self.assertEqual(_extract_filename_from_headers(headers_utf8, "fallback.docx"), "Story Part 1.docx")

        self.assertEqual(_extract_filename_from_headers({}, "fallback.docx"), "fallback.docx")

    def test_download_valid_doc_mocked(self):
        # Create a real minimal zip/docx in memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
        valid_docx_bytes = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.geturl.return_value = "https://docs.google.com/document/d/xyz/export?format=docx"
        mock_resp.read.return_value = valid_docx_bytes
        mock_resp.headers = {"Content-Disposition": 'attachment; filename="Downloaded_Script.docx"'}
        mock_resp.__enter__.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp:
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result_file = download_google_doc(
                    "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
                    destination_dir=tmp,
                )
                self.assertTrue(result_file.is_file())
                self.assertEqual(result_file.name, "Downloaded_Script.docx")
                self.assertTrue(zipfile.is_zipfile(str(result_file)))

    def test_download_private_doc_rejected(self):
        # Simulate redirect to accounts.google.com login page
        mock_resp = MagicMock()
        mock_resp.geturl.return_value = "https://accounts.google.com/ServiceLogin?service=wise..."
        mock_resp.read.return_value = b"<html><head><title>Google Accounts</title></head></html>"
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp:
            with patch("urllib.request.urlopen", return_value=mock_resp):
                with self.assertRaises(PermissionError) as ctx:
                    download_google_doc(
                        "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
                        destination_dir=tmp,
                    )
                self.assertIn("private", str(ctx.exception).lower())

    def test_download_404_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(None, 404, "Not Found", {}, None)):
                with self.assertRaises(FileNotFoundError):
                    download_google_doc(
                        "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
                        destination_dir=tmp,
                    )


if __name__ == "__main__":
    unittest.main()
