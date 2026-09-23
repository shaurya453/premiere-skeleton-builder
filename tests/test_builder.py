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

    def test_unreadable_embedded_image_is_skipped_not_fatal(self):
        # A corrupt/unsupported embedded picture (e.g. a WMF/EMF Word sometimes embeds)
        # should not sink the whole doc parse; it's reported as a warning and skipped.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="broken"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="broken"/><w:p><a:blip r:embed="a1"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.wmf"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.writestr("word/media/image1.wmf", b"not actually an image")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertEqual(assets, [])
            self.assertTrue(any("Skipped unreadable embedded image" in w for w in warnings))
            self.assertTrue(any("Missing bookmark/image" in w for w in warnings))

    def test_embedded_image_crop_is_applied(self):
        # Word keeps the full original image and stores any crop the writer applied as a
        # sibling <a:srcRect> (thousandths-of-a-percent trimmed from each edge). The saved
        # asset should reflect the cropped region, not the original full image.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (100, 100)).save(root / "img.png")
            # l=25%, t=10%, r=25%, b=10% -> crop to the middle 50x80 region.
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:p><a:blipFill><a:blip r:embed="a1"/><a:srcRect l="25000" t="10000" r="25000" b="10000"/></a:blipFill></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.write(root / "img.png", "word/media/image1.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(warnings)
            self.assertEqual(len(assets), 1)
            self.assertEqual((assets[0]["width"], assets[0]["height"]), (50, 80))
            with Image.open(assets[0]["path"]) as saved:
                self.assertEqual(saved.size, (50, 80))

    def test_unrelated_unreadable_image_does_not_orphan_a_later_bookmark(self):
        # A decorative/unrelated picture that fails to open (e.g. a WMF Word embeds for a
        # link preview) sitting between a bookmark and its real image must not steal that
        # bookmark - the next readable, bookmarked image downstream should still get it.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/>
                     <w:p><a:blip r:embed="bad"/></w:p>
                     <w:p><a:blip r:embed="a1"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="bad" Target="media/broken.wmf"/>
                      <Relationship Id="a1" Target="media/image1.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.writestr("word/media/broken.wmf", b"not actually an image")
                z.write(root / "img.png", "word/media/image1.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertTrue(any("Skipped unreadable embedded image" in w for w in warnings))
            self.assertFalse(any("Missing bookmark/image" in w for w in warnings))
            self.assertFalse(any("never attached to any image" in w for w in warnings))
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["source_part"], "word/media/image1.png")
            self.assertEqual(cues[0]["refs"][0]["asset"]["source_part"], "word/media/image1.png")

    def test_non_img_labelled_missing_bookmark_now_warns(self):
        # A hyperlink to a bookmark that never resolved to an image used to be dropped
        # completely (no warning at all) unless its label literally said "IMG". Any
        # unresolved reference to a same-document bookmark should be surfaced.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:r><w:t>They found the </w:t></w:r><w:hyperlink w:anchor="missing"><w:r><w:t>book</w:t></w:r></w:hyperlink><w:r><w:t>.</w:t></w:r></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertTrue(any("Missing bookmark/image for book: missing" in w for w in warnings))

    def test_vml_legacy_picture_is_extracted(self):
        # A picture stored via the legacy VML fallback (w:pict/v:imagedata, r:id instead of
        # r:embed) previously had no matching branch at all and was invisible to the scanner.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:v="urn:schemas-microsoft-com:vml"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:p><w:r><w:pict><v:shape><v:imagedata r:id="a1"/></v:shape></w:pict></w:r></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.write(root / "img.png", "word/media/image1.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(warnings)
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["source_part"], "word/media/image1.png")
            self.assertEqual(cues[0]["refs"][0]["asset"]["source_part"], "word/media/image1.png")

    def test_percent_encoded_bookmark_anchor_matches_unicode_name(self):
        # bookmark_id() must unquote() the plain-w:anchor fallback path too, not just the
        # Google-Docs "#bookmark=id.xxx" branch, so percent-encoded anchors still match the
        # raw-unicode name a bookmarkStart actually stores.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="caf%C3%A9"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="café"/><w:p><a:blip r:embed="a1"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.write(root / "img.png", "word/media/image1.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(warnings)
            self.assertEqual(len(assets), 1)
            self.assertEqual(cues[0]["refs"][0]["asset"]["source_part"], "word/media/image1.png")

    def test_bookmark_with_no_nearby_image_warns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:r><w:t>Just narration, no image link at all.</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="orphan"/>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertTrue(any("Bookmark 'orphan' was never attached to any image" in w for w in warnings))

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
