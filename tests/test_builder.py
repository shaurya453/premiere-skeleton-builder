import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree as ET
from PIL import Image

from skeleton_builder import read_docx, timed_tokens, xml_sequence, inspect_docx, preview_cues


class BookmarkTests(unittest.TestCase):
    def test_body_level_bookmarks_and_adjacent_cues(self):
        # Image filenames deliberately do not match IMG numbering.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink r:id="h1"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>) (</w:t></w:r><w:hyperlink w:anchor="second"><w:r><w:t>IMG 2</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:p><a:blip r:embed="a1"/></w:p>
                     <w:bookmarkStart w:id="2" w:name="second"/><w:p><a:blip r:embed="a2"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="h1" Target="https://docs.google.com/document/d/demo/edit#bookmark=id.first"/>
                      <Relationship Id="a1" Target="media/image9.png"/>
                      <Relationship Id="a2" Target="media/image3.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                for name in ["image9.png", "image3.png"]:
                    z.write(root / "img.png", "word/media/" + name)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(warnings)
            self.assertEqual(words, ["they", "found", "the", "book"])
            self.assertEqual(len(cues), 1)
            self.assertEqual(cues[0]["script_start"], 0)
            self.assertEqual(cues[0]["refs"][0]["asset"]["source_part"], "word/media/image9.png")
            self.assertEqual(cues[0]["refs"][1]["asset"]["source_part"], "word/media/image3.png")

            # Validate inspect_docx returns faithful pre-flight stats
            stat = inspect_docx(root / "script.docx")
            self.assertTrue(stat["valid"])
            self.assertEqual(stat["image_cues"], 1)
            self.assertEqual(stat["video_cues"], 0)
            self.assertEqual(stat["word_count"], 4)
            self.assertEqual(stat["embedded_images"], 2)

            # preview_cues gives the same cue without touching the network, and embedded
            # images (unlike remote ones) already have a real, persistent local path.
            preview, preview_warnings = preview_cues(root / "script.docx")
            self.assertFalse(preview_warnings)
            self.assertEqual(len(preview), 1)
            self.assertEqual(preview[0]["kind"], "image")
            self.assertEqual(preview[0]["passage"], "They found the book")
            self.assertTrue(Path(preview[0]["asset_path"]).is_file())

    def test_wrong_audio_cache_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "audio").write_bytes(b"different audio")
            (root / "words.json").write_text(json.dumps({"audio_sha256": "wrong", "words": []}))
            with self.assertRaisesRegex(ValueError, "different audio"):
                timed_tokens(root/"words.json", root/"audio")


    def test_word_timings_become_tokens_and_missing_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "audio").write_bytes(b"voice")
            digest = hashlib.sha256(b"voice").hexdigest()
            (root / "words.json").write_text(json.dumps({"audio_sha256": digest, "words": [
                {"word": " Hello,", "start": 0.0, "end": 0.5}, {"word": " world", "start": 0.6, "end": 1.0}]}))
            fine, method, warnings = timed_tokens(root / "words.json", root / "audio")
            self.assertEqual([t["token"] for t in fine], ["hello", "world"])
            self.assertEqual(fine[1]["start"], 0.6)
            with self.assertRaisesRegex(ValueError, "required"):
                timed_tokens(None, root / "audio")


class XmlTests(unittest.TestCase):
    def test_test_sequence_trims_source_out_and_escapes_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "output.xml"
            image = {"name": "IMG & 1", "start_frame": 20, "end_frame": 1000,
                     "path": str(root / "image & space.png"), "width": 1000, "height": 2000}
            xml_sequence(path, "Test", [image], [], [], root/"audio.wav", 20, 1920, 1080, limit=2)
            xml = ET.parse(str(path))
            clip = xml.find(".//video/track/clipitem")
            self.assertEqual(clip.findtext("end"), "60")
            self.assertEqual(clip.findtext("out"), "40")
            self.assertEqual(clip.findtext("filter/effect/parameter/value"), "54.000000")
            self.assertIn("%20%26%20", clip.findtext("file/pathurl"))
            self.assertEqual(xml.findtext(".//sequence/rate/ntsc"), "TRUE")


if __name__ == "__main__":
    unittest.main()
