import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree as ET
from PIL import Image

from skeleton_builder import read_docx, timed_tokens, xml_sequence, validate_xml, inspect_docx, preview_cues, build, align_cues


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

    def test_unresolved_relationship_id_warns_not_silent(self):
        # A picture whose r:embed/r:id doesn't resolve to any relationship used to be
        # dropped with a bare `continue` - no warning at all. It should surface now.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:p><a:blip r:embed="missing_rel"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertEqual(assets, [])
            self.assertTrue(any("Could not resolve picture relationship" in w for w in warnings))

    def test_picture_in_header_is_captured_via_its_own_rels(self):
        # Headers/footers are separate XML parts with their own relationships file - a
        # picture placed there was previously invisible to a document.xml-only scan.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     </w:body></w:document>'''
            header = '''<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                        xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                        <w:p><w:bookmarkStart w:id="1" w:name="first"/><w:r><a:blip r:embed="a1"/></w:r></w:p>
                        </w:hdr>'''
            doc_rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''
            header_rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                             <Relationship Id="a1" Target="media/imageH.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", doc_rels)
                z.writestr("word/header1.xml", header)
                z.writestr("word/_rels/header1.xml.rels", header_rels)
                z.write(root / "img.png", "word/media/imageH.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(warnings)
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["source_part"], "word/media/imageH.png")
            self.assertEqual(cues[0]["refs"][0]["asset"]["source_part"], "word/media/imageH.png")

    def test_externally_linked_picture_gets_specific_warning(self):
        # A linked (not embedded) picture (TargetMode="External") can't be recovered from
        # inside the .docx - it should get a distinct, actionable warning, not the generic
        # "unreadable" one used for a corrupt/unsupported embedded format.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:p><a:blip r:embed="ext1"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="ext1" Target="http://example.com/photo.png" TargetMode="External"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertEqual(assets, [])
            self.assertTrue(any("linked, not embedded" in w for w in warnings))
            self.assertFalse(any("Skipped unreadable" in w for w in warnings))

    def test_closed_bookmark_does_not_absorb_later_image(self):
        # A bookmark whose bookmarkEnd already closed, with real narration text before any
        # image appears, shouldn't attach to a later, unrelated picture.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:bookmarkEnd w:id="1"/>
                     <w:p><w:r><w:t>Several unrelated sentences of narration follow, with no image nearby at all.</w:t></w:r></w:p>
                     <w:p><a:blip r:embed="a1"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.write(root / "img.png", "word/media/image1.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["bookmarks"], [])
            self.assertIsNone(cues[0]["refs"][0]["asset"])
            self.assertTrue(any("Bookmark 'first' was never attached to any image" in w for w in warnings))
            self.assertTrue(any("Missing bookmark/image for IMG 1: first" in w for w in warnings))

    def test_google_docs_zero_width_bookmark_before_image_still_attaches(self):
        # Google Docs cannot bookmark an inline image directly: bookmarking an image there
        # produces a zero-width bookmark (start immediately followed by end) in an empty
        # paragraph, with the actual picture in the very next paragraph - nothing in between.
        # This is how real Google Docs scripts bookmark every image, so it must still attach.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>They found the book. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:p><w:bookmarkStart w:id="1" w:name="first"/><w:bookmarkEnd w:id="1"/></w:p>
                     <w:p><a:blip r:embed="a1"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.write(root / "img.png", "word/media/image1.png")
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["bookmarks"], ["first"])
            self.assertIsNotNone(cues[0]["refs"][0]["asset"])
            self.assertFalse(any("was never attached to any image" in w for w in warnings))

    def test_google_auto_generated_bookmarks_are_not_warned_about(self):
        # A doc with active suggestions (Google Docs "Suggesting" mode) carries one internal,
        # underscore-prefixed bookmark per suggestion range, plus others for heading anchors -
        # none of these were ever meant to attach to an image, so warning about each one (a
        # real doc can have hundreds) buries the warnings that are actually actionable. A
        # bookmark someone actually inserted (no leading underscore) should still warn.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:bookmarkStart w:id="1" w:name="_1a2b3c4d5e6f"/><w:bookmarkEnd w:id="1"/>
                     <w:r><w:t>Some narration text with nothing bookmarked nearby.</w:t></w:r></w:p>
                     <w:p><w:bookmarkStart w:id="2" w:name="realUserBookmark9"/><w:bookmarkEnd w:id="2"/>
                     <w:r><w:t>More narration, again with no image nearby.</w:t></w:r></w:p>
                     </w:body></w:document>'''
            rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(any("_1a2b3c4d5e6f" in w for w in warnings))
            self.assertTrue(any("realUserBookmark9" in w and "never attached" in w for w in warnings))

    def test_isolated_video_timestamp_paragraph_becomes_an_insert_cue(self):
        # A scriptwriter habit seen in a real script: a bracketed timestamp alone on its own
        # paragraph, hyperlinked to the source video, with no other narration in that
        # paragraph at all - meaning "the VO pauses here for this clip," not "illustrate this
        # passage." This used to be silently dropped with a "No preceding narration" warning.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:r><w:t>Officers arrived and searched the house.</w:t></w:r></w:p>
                     <w:p><w:hyperlink r:id="h1"><w:r><w:t>[ 2:07 - 2:10 ]</w:t></w:r></w:hyperlink></w:p>
                     <w:p><w:r><w:t>What they found upstairs changed everything.</w:t></w:r></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="h1" Target="https://youtu.be/fftGair1ZoA"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertFalse(any("No preceding narration" in w for w in warnings))
            inserts = [c for c in cues if c.get("insert")]
            self.assertEqual(len(inserts), 1)
            self.assertEqual(inserts[0]["kind"], "video")
            self.assertEqual(inserts[0]["script_start"], inserts[0]["script_end"])
            self.assertEqual(inserts[0]["refs"][0]["target"], "https://youtu.be/fftGair1ZoA")

    def test_autolinkified_url_label_does_not_pollute_narration_tokens(self):
        # A real scriptwriter habit: paste a bare URL, which Google Docs auto-linkifies with
        # the URL text itself as the visible label. A whole paragraph of these (a reference
        # block) used to feed every word of the URL into the ASR-matching corpus as if it
        # were spoken narration, contaminating alignment for anything scanned afterward.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:r><w:t>Officers arrived at the scene.</w:t></w:r></w:p>
                     <w:p><w:hyperlink r:id="h1"><w:r><w:t>https://www.facebook.com/AETV/videos/mauricio-guerrero-tries-to-convince-jury</w:t></w:r></w:hyperlink></w:p>
                     <w:p><w:r><w:t>What they found upstairs changed everything.</w:t></w:r></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="h1" Target="https://www.facebook.com/AETV/videos/mauricio-guerrero-tries-to-convince-jury"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets", fetch_web=False)
            self.assertEqual(words, ["officers", "arrived", "at", "the", "scene",
                                      "what", "they", "found", "upstairs", "changed", "everything"])
            self.assertTrue(any("No preceding narration" in w for w in warnings))

    def test_pronunciation_link_is_excluded_from_cues(self):
        # (pron: <link>) is a note for the voiceover artist, not an editing instruction - a
        # link inside one (often a pronunciation clip) must not become an image/video cue
        # the way every other inline link does, and its URL must not leak into the corpus.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
                     <w:p><w:r><w:t>Mauricio Guerrero (pron: </w:t></w:r><w:hyperlink r:id="h1"><w:r><w:t>https://youtube.com/shorts/xyz</w:t></w:r></w:hyperlink><w:r><w:t>) was arrested.</w:t></w:r></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="h1" Target="https://youtube.com/shorts/xyz"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
            words, cues, assets, warnings = read_docx(root / "script.docx", root / "assets")
            self.assertEqual(cues, [])
            self.assertFalse(warnings)
            self.assertEqual(words, ["mauricio", "guerrero", "was", "arrested"])

    def _link_doc(self, *paragraphs):
        """Parse paragraphs given as lists of ("text" | ("yt"|"range"|"bk", text)) runs.

        "yt" is a YouTube link carrying ?t=100, "range" a plain YouTube link (its range lives
        in the visible text), "bk" a bookmark link to one embedded image."""
        def run(part):
            if isinstance(part, str):
                return f'<w:r><w:t xml:space="preserve">{part}</w:t></w:r>'
            rid, text = part
            return f'<w:hyperlink r:id="{rid}"><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:hyperlink>'
        body = "".join("<w:p>" + "".join(run(part) for part in para) + "</w:p>" for para in paragraphs)
        doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
               'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>' + body +
               '<w:bookmarkStart w:id="1" w:name="bk"/><w:p><a:blip r:embed="a1"/></w:p></w:body></w:document>')
        rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="yt" Target="https://youtu.be/fftGair1ZoA?t=100"/>'
                '<Relationship Id="range" Target="https://youtu.be/fftGair1ZoA"/>'
                '<Relationship Id="bk" Target="https://docs.google.com/document/d/demo/edit#bookmark=id.bk"/>'
                '<Relationship Id="a1" Target="media/image1.png"/></Relationships>')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                z.write(root / "img.png", "word/media/image1.png")
            return read_docx(root / "script.docx", root / "assets", fetch_web=False)

    def test_start_time_link_on_spoken_words_covers_those_words_wherever_they_sit(self):
        # The writer linked the very words the clip illustrates: the cue anchors at the end
        # of those words and its passage is that sentence - never the sentence before it, and
        # never a pause-the-VO clip just because the linked words open the paragraph.
        linked = ("yt", "In that clip he defends the decision.")
        for layout in [["The trial began in March. ", linked, " Then the jury left."],
                       [linked, " Then the jury left."],
                       ["The trial began in March. Then the jury left. ", linked],
                       [linked]]:
            words, cues, _, warnings = self._link_doc(layout)
            self.assertEqual(len(cues), 1, layout)
            self.assertFalse(cues[0].get("insert"), layout)
            self.assertEqual(cues[0]["passage"], "In that clip he defends the decision", layout)
            self.assertFalse(warnings, layout)
            self.assertIn("decision", words)  # linked words stay in the narration

    def test_bookmark_link_on_spoken_words_still_covers_those_words(self):
        for layout in [["The trial began in March. ", ("bk", "She held up the profile picture."), " Then the jury left."],
                       [("bk", "She held up the profile picture."), " Then the jury left."]]:
            _, cues, _, warnings = self._link_doc(layout)
            self.assertEqual([c["kind"] for c in cues], ["image"])
            self.assertEqual(cues[0]["passage"], "She held up the profile picture")
            self.assertFalse(warnings)

    def test_pause_clip_needs_a_paragraph_with_no_narration_at_all(self):
        # A range alone in its own paragraph pauses the VO; the timestamp's digits are not
        # narration and must not leak into the ASR-matching words.
        words, cues, _, warnings = self._link_doc(
            ["Hello there."], [("range", "[2:00-2:05]")], ["Back to the story."])
        self.assertEqual(words, ["hello", "there", "back", "to", "the", "story"])
        self.assertEqual([c.get("insert") for c in cues], [True])
        self.assertFalse(warnings)
        # A bare start time on a start-time link is a marker too, not narration.
        words, cues, _, _ = self._link_doc(["Hello there."], [("yt", "0:05")], ["Back."])
        self.assertEqual(words, ["hello", "there", "back"])
        self.assertEqual([c.get("insert") for c in cues], [True])
        # The same range after some words is an overlay on those words, digits still excluded.
        words, cues, _, _ = self._link_doc(["Hello there. ", ("range", "[2:00-2:05]"), " More."])
        self.assertEqual(words, ["hello", "there", "more"])
        self.assertEqual(len(cues), 1)
        self.assertFalse(cues[0].get("insert"))
        self.assertEqual(cues[0]["passage"], "Hello there")

    def test_range_before_narration_in_the_same_paragraph_is_not_a_pause_clip(self):
        # Narration shares the paragraph, so it is neither "alone" (pause) nor "after some
        # words" (overlay): warn and leave it for the writer rather than pausing the VO.
        words, cues, _, warnings = self._link_doc([("range", "[2:00-2:05]"), " Then he left."])
        self.assertEqual(cues, [])
        self.assertEqual(words, ["then", "he", "left"])
        self.assertTrue(any("No preceding narration" in w for w in warnings))

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


class AlignmentDiagnosticsTests(unittest.TestCase):
    def test_low_coverage_error_points_at_the_unmatched_stretch(self):
        # A script with real narration followed by a long stretch of unrelated text (an old
        # draft, notes, another tab's content pulled in by mistake) should fail with a message
        # that names roughly where the bad stretch starts and what it says - not just a bare
        # percentage the user has to go spelunking in the XML to explain.
        matched = [f"word{i}" for i in range(40)]
        junk = [f"junk{i}" for i in range(200)]
        script_tokens = matched + junk
        narration = [{"token": t, "start": i, "end": i + 1} for i, t in enumerate(matched)]
        with self.assertRaises(ValueError) as ctx:
            align_cues(script_tokens, [], narration)
        message = str(ctx.exception)
        self.assertIn("%", message)
        self.assertIn("junk0 junk1", message)

    def test_well_matched_script_does_not_raise(self):
        script_tokens = [f"word{i}" for i in range(40)]
        narration = [{"token": t, "start": i, "end": i + 1} for i, t in enumerate(script_tokens)]
        align_cues(script_tokens, [], narration)  # should not raise


class InsertCueAlignmentTests(unittest.TestCase):
    def test_insert_cue_gets_splice_time_from_nearest_earlier_word(self):
        # An insert's script_start == script_end (no matched passage - see read_docx) would
        # crash the normal range-matching path on an empty `matches` list; it needs its own
        # splice-time computation instead: the end of the nearest earlier aligned word.
        script_tokens = [f"word{i}" for i in range(10)]
        narration = [{"token": t, "start": i * 2.0, "end": i * 2.0 + 1.0} for i, t in enumerate(script_tokens)]
        cue = {"kind": "video", "script_start": 5, "script_end": 5, "insert": True, "refs": []}
        align_cues(script_tokens, [cue], narration)
        self.assertEqual(cue["start"], cue["end"])
        self.assertEqual(cue["start"], narration[4]["end"])

    def test_insert_cue_with_nothing_before_it_falls_back_to_unaligned(self):
        script_tokens = [f"word{i}" for i in range(10)]
        narration = [{"token": t, "start": i * 2.0, "end": i * 2.0 + 1.0} for i, t in enumerate(script_tokens)]
        cue = {"kind": "video", "script_start": 0, "script_end": 0, "insert": True, "refs": []}
        align_cues(script_tokens, [cue], narration)
        self.assertIsNone(cue["start"])
        self.assertIsNone(cue["end"])


class CaseDetectionTests(unittest.TestCase):
    def _script(self, root, paragraphs):
        """Write a docx of plain paragraphs; a "@name" paragraph is an image bookmark + picture."""
        Image.new("RGB", (60, 40)).save(root / "img.png")
        body, rels = "", ""
        for index, text in enumerate(paragraphs, 1):
            if text.startswith("@"):
                body += (f'<w:bookmarkStart w:id="{index}" w:name="{text[1:]}"/>'
                         f'<w:p><a:blip r:embed="a{index}"/></w:p>')
                rels += f'<Relationship Id="a{index}" Target="media/image{index}.png"/>'
            else:
                body += f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
               'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>' + body +
               "</w:body></w:document>")
        with ZipFile(root / "script.docx", "w") as z:
            z.writestr("word/document.xml", doc)
            z.writestr("word/_rels/document.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       + rels + "</Relationships>")
            for index, text in enumerate(paragraphs, 1):
                if text.startswith("@"):
                    z.write(root / "img.png", f"word/media/image{index}.png")
        return root / "script.docx"

    def test_cases_are_detected_by_divider_and_only_real_dividers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = self._script(root, [
                "Welcome to the video.", "@intro_pic",
                "Case 1: The Wiztale Incident", "@one_a",
                "Case 2 - Another One", "@two_a", "@two_b",
                "Case 3 was closed years ago, the narrator says.", "@still_two",
                "CASE 4) Last", "@four_a"])
            _, _, embedded, _ = read_docx(script, root / "assets", fetch_web=False)
            self.assertEqual([a["case"] for a in embedded], [0, 1, 2, 2, 2, 4])

    def test_script_without_case_dividers_has_no_case_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = self._script(root, ["Just a story.", "@pic"])
            _, cues, embedded, _ = read_docx(script, root / "assets", fetch_web=False)
            self.assertNotIn("case", embedded[0])

    def test_inspect_lists_each_case_with_its_heading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = self._script(root, [
                "Welcome to the video.", "Case 1: The Wiztale Incident", "Some narration here.",
                "Case 2 - Another One", "More narration follows."])
            listed = inspect_docx(script)["case_list"]
            self.assertEqual([c["number"] for c in listed], [1, 2])
            self.assertEqual(listed[0]["title"], "Case 1: The Wiztale Incident")
            self.assertEqual(listed[1]["title"], "Case 2 - Another One")
            plain = self._script(root, ["Just a story with no case headings."])
            self.assertEqual(inspect_docx(plain)["case_list"], [])

    def test_case_summary_counts_cues_per_case(self):
        from skeleton_builder import _case_summary
        layout = {"cases": [{"number": 1, "title": "Case 1: A"}, {"number": 2, "title": "Case 2: B"}]}
        cues = [{"kind": "image", "case": 1}, {"kind": "image", "case": 1}, {"kind": "video", "case": 1},
                {"kind": "video", "case": 2}, {"kind": "image", "case": 0}]
        self.assertEqual(_case_summary(layout, cues), [
            {"number": 1, "title": "Case 1: A", "image_cues": 2, "video_cues": 1},
            {"number": 2, "title": "Case 2: B", "image_cues": 0, "video_cues": 1}])

    def test_ticked_cases_become_one_contiguous_range(self):
        from skeleton_builder import case_range_for, parse_case_range
        every = [1, 2, 3, 4]
        self.assertEqual(case_range_for({1, 2, 3, 4}, every), "")
        self.assertEqual(case_range_for({3}, every), "3")
        self.assertEqual(case_range_for({1, 3}, every), "1-3")  # the gap (case 2) is filled in
        self.assertEqual(case_range_for({2, 4}, every), "2-4")
        self.assertEqual(case_range_for({1, 2}, [1, 2, 4]), "1-2")
        self.assertEqual(case_range_for({1, 4}, [1, 2, 4]), "")  # all of them, however they're numbered
        self.assertEqual(case_range_for({2, 4}, [1, 2, 4]), "2-4")
        for text in ("1-3", "2-4", "3"):
            self.assertIsNotNone(parse_case_range(text))
        with self.assertRaisesRegex(ValueError, "at least one case"):
            case_range_for(set(), every)

    def test_unsynced_items_are_named_by_case_and_position(self):
        import wave
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paragraphs = [
                "Welcome to the quiet valley today", "@intro_pic",
                "Case 1: First", "Sunlight warmed the misty morning slowly", "@one_a",
                "Case 2: Second", "Birds sang softly near the old bridge", "@two_a", "@two_b",
                "Case 3: Third", "The river ran cold under gray winter skies", "@three_a"]
            script = self._script(root, paragraphs)
            audio = root / "voice.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(bytes(2) * 16000 * 40)
            digest = hashlib.sha256(audio.read_bytes()).hexdigest()
            spoken = [w for text in paragraphs if not text.startswith("@") for w in text.replace(":", "").split()]
            (root / "words.json").write_text(json.dumps({"audio_sha256": digest, "words": [
                {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(spoken)]}))
            report = build(script, audio, root / "Project" / "Timeline", words=root / "words.json")
            names = [c["name"] for c in sorted(report["unconfirmed_clips"], key=lambda c: c["start_frame"])]
            self.assertEqual(names, ["UNSYNCED IMG_INTRO_1", "UNSYNCED IMG_1_1", "UNSYNCED IMG_2_1", "UNSYNCED IMG_2_2", "UNSYNCED IMG_3_1"])
            xml = (root / "Project" / "Timeline" / "Skeleton_full.xml").read_text(encoding="utf8")
            self.assertIn("UNSYNCED IMG_2_2", xml)


class CaseSplitTests(unittest.TestCase):
    """Two editors, one video: each takes a range of cases; the voiceover is cut between them."""
    CASES = [
        ("Case 1: One", "Sunlight warmed the quiet misty valley slowly across the morning while birds sang softly", "b1"),
        ("Case 2: Two", "Lanterns glowed along the empty harbor road as fishermen coiled their heavy wet ropes", None),
        ("Case 3: Three", "The river ran cold under gray winter skies while the villagers waited for spring rain", "b3"),
        ("Case 4: Four", "Copper kettles rattled on the crowded market stalls beneath the striped canvas awnings", None),
    ]

    def _make(self, root, speak_headings):
        import re
        import wave
        Image.new("RGB", (60, 40)).save(root / "img.png")
        body, rels, files, spoken, t, sentence_start, section_start = "", "", [], [], 0.0, {}, {}
        for number, (heading, sentence, bookmark) in enumerate(self.CASES, 1):
            link = ""
            if bookmark:
                link = (f'<w:r><w:t xml:space="preserve"> (</w:t></w:r><w:hyperlink w:anchor="{bookmark}"><w:r>'
                        f'<w:t>IMG {number}</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r>')
            body += (f"<w:p><w:r><w:t>{heading}</w:t></w:r></w:p>"
                     f'<w:p><w:r><w:t>{sentence}.</w:t></w:r>{link}</w:p>')
            for k in range(2 if bookmark else 1):
                name = f"loose{number}" if k or not bookmark else bookmark
                body += f'<w:bookmarkStart w:id="{number * 10 + k}" w:name="{name}"/><w:p><a:blip r:embed="a{number}{k}"/></w:p>'
                rels += f'<Relationship Id="a{number}{k}" Target="media/image{number}{k}.png"/>'
                files.append(f"word/media/image{number}{k}.png")
            t += 2.0  # the pause between cases
            section_start[number] = round(t, 3)
            if speak_headings:
                for word in re.findall(r"[A-Za-z0-9]+", heading):
                    spoken.append({"word": word.lower(), "start": round(t, 3), "end": round(t + 0.4, 3)})
                    t += 0.5
                t += 0.3
            for index, word in enumerate(sentence.split()):
                if index == 0:
                    sentence_start[number] = round(t, 3)
                spoken.append({"word": word.lower(), "start": round(t, 3), "end": round(t + 0.4, 3)})
                t += 0.5
        doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
               'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>' + body + "</w:body></w:document>")
        with ZipFile(root / "script.docx", "w") as z:
            z.writestr("word/document.xml", doc)
            z.writestr("word/_rels/document.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       + rels + "</Relationships>")
            for name in files:
                z.write(root / "img.png", name)
        total = t + 2.0
        audio = root / "voice.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(2) * int(16000 * total))
        (root / "words.json").write_text(json.dumps({
            "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "words": spoken}))
        return audio, total, sentence_start, section_start

    def _wav_seconds(self, path):
        import wave
        with wave.open(str(path)) as wav:
            return wav.getnframes() / wav.getframerate()

    def _check_split(self, speak_headings):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio, total, sentence_start, section_start = self._make(root, speak_headings)
            script = root / "script.docx"
            first = build(script, audio, root / "A" / "Timeline", words=root / "words.json", cases=(1, 2))
            second = build(script, audio, root / "B" / "Timeline", words=root / "words.json", cases=(3, 4))
            a, b = first["case_split"], second["case_split"]
            # Both editors' audio meets at one instant, inside the pause before case 3 begins
            # (the pause is the 2s just before section_start[3]).
            self.assertEqual(a["cut_end_seconds"], b["cut_start_seconds"])
            self.assertGreater(a["cut_end_seconds"], section_start[3] - 2.0)
            self.assertLess(a["cut_end_seconds"], section_start[3])
            self.assertEqual(a["cut_start_seconds"], 0.0)
            self.assertAlmostEqual(first["duration_seconds"] + second["duration_seconds"], total, delta=0.01)
            from skeleton_builder import FPS
            for run, report in (("A", first), ("B", second)):
                # Each editor's file holds the whole recording (so its ends can be dragged out to
                # restore audio); the sequence uses only their stretch of it.
                self.assertAlmostEqual(self._wav_seconds(root / run / "Audio" / "voiceover.wav"), total, delta=0.05)
                xml = ET.parse(str(root / run / "Timeline" / "Skeleton_full.xml"))
                voice = xml.find("sequence/media/audio/track[1]/clipitem")
                self.assertEqual(int(voice.findtext("in")), round(report["case_split"]["cut_start_seconds"] * FPS))
                self.assertAlmostEqual((int(voice.findtext("out")) - int(voice.findtext("in"))) / FPS,
                                       report["duration_seconds"], delta=0.1)
                self.assertGreaterEqual(int(voice.findtext("duration")), round(total * FPS))
            # Each editor only gets their own media: one linked picture and one loose picture per case.
            self.assertEqual(len(first["embedded_assets"]), 3)
            self.assertEqual(len(second["embedded_assets"]), 3)
            self.assertEqual(len(list((root / "A" / "Media" / "Images").glob("*.png"))), 3)
            self.assertEqual(len(list((root / "B" / "Media" / "Images").glob("*.png"))), 3)
            self.assertEqual([c["case"] for c in first["cues"]], [1])
            self.assertEqual([c["case"] for c in second["cues"]], [3])
            # Case 3's cue lands where its sentence starts, measured from the cut, not from 0:00.
            self.assertAlmostEqual(second["cues"][0]["start"], sentence_start[3] - b["cut_start_seconds"], delta=0.05)
            self.assertTrue(all(c["name"].startswith(("UNSYNCED IMG_3_", "UNSYNCED IMG_4_")) for c in second["unconfirmed_clips"]))

    def test_two_editors_split_the_voiceover_between_their_cases(self):
        self._check_split(speak_headings=False)

    def test_split_also_works_when_the_vo_reads_the_case_headings_aloud(self):
        self._check_split(speak_headings=True)

    def _case_markers_in(self, xml_path):
        from skeleton_builder import FPS
        markers = ET.parse(str(xml_path)).getroot().findall("./sequence/marker")
        return [(m.findtext("name"), int(m.findtext("in")) / FPS) for m in markers if m.findtext("name").startswith("Case ")]

    def test_timeline_carries_a_marker_where_each_case_begins(self):
        for speak_headings in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                audio, total, sentence_start, section_start = self._make(root, speak_headings)
                script = root / "script.docx"
                whole = build(script, audio, root / "W" / "Timeline", words=root / "words.json")
                # The marker sits on the first thing said in the case: its spoken heading if the VO
                # reads them out, otherwise its first sentence.
                begins = section_start if speak_headings else sentence_start
                self.assertEqual([m["case"] for m in whole["case_markers"]], [1, 2, 3, 4])
                for marker in whole["case_markers"]:
                    self.assertAlmostEqual(marker["seconds"], begins[marker["case"]], delta=.05)
                for xml in ("Skeleton_full.xml", "Skeleton_test_45s.xml"):
                    found = self._case_markers_in(root / "W" / "Timeline" / xml)
                    limit = 45 if "45s" in xml else total
                    expected = [(c[0], begins[n]) for n, c in enumerate(self.CASES, 1) if begins[n] < limit]
                    self.assertEqual([name for name, _ in found], [name for name, _ in expected])
                    for (_, seen), (_, wanted) in zip(found, expected):
                        self.assertAlmostEqual(seen, wanted, delta=.05)
                # An editor's slice only carries its own cases, measured from where its audio starts.
                second = build(script, audio, root / "B" / "Timeline", words=root / "words.json", cases=(3, 4))
                cut = second["case_split"]["cut_start_seconds"]
                self.assertEqual([m["case"] for m in second["case_markers"]], [3, 4])
                for marker in second["case_markers"]:
                    self.assertAlmostEqual(marker["seconds"], begins[marker["case"]] - cut, delta=.05)

    def test_last_range_runs_to_the_end_and_first_range_holds_the_intro(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio, total, _, _ = self._make(root, False)
            tail = build(root / "script.docx", audio, root / "T" / "Timeline", words=root / "words.json", cases=(4, None))
            self.assertAlmostEqual(tail["case_split"]["cut_end_seconds"], total, delta=0.01)
            self.assertGreater(tail["case_split"]["cut_start_seconds"], 0)
            head = build(root / "script.docx", audio, root / "H" / "Timeline", words=root / "words.json", cases=(1, 1))
            self.assertEqual(head["case_split"]["cut_start_seconds"], 0.0)

    def test_requesting_a_missing_case_or_a_script_without_cases_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio, _, _, _ = self._make(root, False)
            with self.assertRaisesRegex(ValueError, "has cases 1, 2, 3, 4"):
                build(root / "script.docx", audio, root / "X" / "Timeline", words=root / "words.json", cases=(7, 8))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plain = CaseDetectionTests()._script(root, ["Just a story with no case headings at all.", "@pic"])
            with self.assertRaisesRegex(ValueError, "no 'Case N:' headings"):
                read_docx(plain, root / "assets", fetch_web=False, cases=(1, 2))

    def test_case_range_text_is_parsed(self):
        from skeleton_builder import parse_case_range
        self.assertIsNone(parse_case_range(""))
        self.assertEqual(parse_case_range("3"), (3, 3))
        self.assertEqual(parse_case_range("1-4"), (1, 4))
        self.assertEqual(parse_case_range(" 5 – 8 "), (5, 8))
        self.assertEqual(parse_case_range("5-"), (5, None))
        for bad in ("0", "4-2", "a-b", "1,3"):
            with self.assertRaises(ValueError):
                parse_case_range(bad)


class UnconfirmedTrackTests(unittest.TestCase):
    def test_unaligned_and_orphan_media_land_on_the_unsynced_track(self):
        # Nothing in the doc should be left for the editor to manually find: an image whose
        # cue couldn't be confidently aligned against the narration, and an embedded image
        # never referenced by any cue at all, must still end up placed on the timeline - on
        # a separate unsynced track (V3), in script order, between the confirmed clips they
        # fall between - rather than silently dropped to a warning.
        import wave
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (60, 40)).save(root / "img.png")

            p1 = "Sunlight warmed the quiet valley slowly across the misty morning while birds sang softly near the old wooden bridge today"
            p2 = "Zonked quetzal ambled wobbly puffins"
            doc = f'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
                     <w:p><w:r><w:t>{p1}. (</w:t></w:r><w:hyperlink w:anchor="first"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="1" w:name="first"/><w:p><a:blip r:embed="a1"/></w:p>
                     <w:bookmarkStart w:id="2" w:name="orphan"/><w:p><a:blip r:embed="a3"/></w:p>
                     <w:p><w:r><w:t>{p2}. (</w:t></w:r><w:hyperlink w:anchor="second"><w:r><w:t>IMG 2</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>
                     <w:bookmarkStart w:id="3" w:name="second"/><w:p><a:blip r:embed="a2"/></w:p>
                     </w:body></w:document>'''
            rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="a1" Target="media/image1.png"/>
                      <Relationship Id="a2" Target="media/image2.png"/>
                      <Relationship Id="a3" Target="media/image3.png"/></Relationships>'''
            with ZipFile(root / "script.docx", "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/_rels/document.xml.rels", rels)
                for name in ("image1.png", "image2.png", "image3.png"):
                    z.write(root / "img.png", "word/media/" + name)

            audio = root / "voice.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 16000 * 10)  # 10s of silence
            digest = hashlib.sha256(audio.read_bytes()).hexdigest()
            words, t = [], 0.0
            for w in p1.split():
                words.append({"word": w, "start": t, "end": t + 0.3})
                t += 0.4
            (root / "words.json").write_text(json.dumps({"audio_sha256": digest, "words": words}))

            out = root / "Project" / "Timeline"
            report = build(root / "script.docx", audio, out, words=root / "words.json")

            confirmed = [c for c in report["clips"] if c.get("kind") != "video"]
            self.assertEqual(len(confirmed), 1)
            self.assertEqual(confirmed[0]["name"], "IMG_1")

            unconfirmed = sorted(report["unconfirmed_clips"], key=lambda c: c["start_frame"])
            self.assertEqual(len(unconfirmed), 2)
            # Script order preserved: the orphan bookmark sits between paragraph 1's image
            # and paragraph 2 in the document, so it must be placed first on the unsynced track.
            self.assertEqual([c["name"] for c in unconfirmed], ["UNSYNCED IMG_2", "UNSYNCED IMG_3"])
            for clip in unconfirmed:
                self.assertGreaterEqual(clip["start_frame"], confirmed[0]["end_frame"])
            for a, b in zip(unconfirmed, unconfirmed[1:]):
                self.assertLessEqual(a["end_frame"], b["start_frame"])

            xml = ET.parse(str(out / "Skeleton_full.xml"))
            tracks = xml.findall(".//sequence/media/video/track")
            self.assertEqual(len(tracks), 3)
            self.assertEqual(len(tracks[2].findall("clipitem")), 2)


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

    def test_overlapping_confirmed_and_unconfirmed_video_audio_get_separate_tracks(self):
        # Regression for a real crash: validate_xml raised "Invalid clip range" because a V3
        # ("unsynced") clip is deliberately allowed to overlap a V2 (confirmed) clip in time
        # (see xml_sequence's source_audio comment) when it has no free gap to slot into - fine
        # for the video tracks, which are already separate, but their source audio used to be
        # merged onto one shared audio track regardless of which group it came from.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video_path = root / "video.mp4"
            video_path.write_bytes(b"fake")
            confirmed = {"name": "Confirmed clip", "start_frame": 0, "end_frame": 100,
                         "path": str(video_path), "width": 640, "height": 360, "kind": "video",
                         "has_audio": True, "source_duration_frames": 100, "in_frame": 0}
            unconfirmed = {"name": "Unconfirmed clip", "start_frame": 50, "end_frame": 150,
                           "path": str(video_path), "width": 640, "height": 360, "kind": "video",
                           "has_audio": True, "source_duration_frames": 150, "in_frame": 0}
            path = root / "output.xml"
            xml_sequence(path, "Test", [confirmed], [], [], None, 10, 1920, 1080,
                         unconfirmed=[unconfirmed])
            validate_xml(path)  # must not raise "Invalid clip range"
            tracks = ET.parse(str(path)).findall(".//sequence/media/audio/track")
            # One stereo pair per group with audio-bearing clips, not one pair shared across
            # both: 2 tracks for the confirmed clip's source audio, 2 more for the
            # unconfirmed one's (no voiceover track here, since audio_path is None).
            self.assertEqual(len(tracks), 4)

    def test_hard_insert_splits_the_voiceover_track_around_the_gap(self):
        # A hard-insert clip (see build()) needs the voiceover audio itself to have a real
        # gap at its position - not just another overlay sitting on top of continuous
        # narration - and the sequence's own duration must grow to fit the gap, or every
        # clip already shifted later by build() would get wrongly capped/dropped.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video_path = root / "video.mp4"
            video_path.write_bytes(b"fake")
            audio_path = root / "voice.wav"
            audio_path.write_bytes(b"fake")
            insert_clip = {"name": "INSERT clip", "start_frame": 300, "end_frame": 390,
                           "path": str(video_path), "width": 640, "height": 360, "kind": "video",
                           "has_audio": True, "source_duration_frames": 900, "in_frame": 0}
            path = root / "output.xml"
            xml_sequence(path, "Test", [insert_clip], [], [], audio_path, 20, 1920, 1080,
                         audio_inserts=[(300, 90)])
            validate_xml(path)  # must not raise "Invalid clip range"
            xml = ET.parse(str(path))
            self.assertEqual(int(xml.findtext(".//sequence/duration")), 600 + 90)
            vo_clips = xml.findall(".//sequence/media/audio/track[1]/clipitem")
            self.assertEqual(len(vo_clips), 2)
            self.assertEqual([c.findtext("start") for c in vo_clips], ["0", "390"])
            self.assertEqual([c.findtext("end") for c in vo_clips], ["300", "690"])
            self.assertEqual([c.findtext("in") for c in vo_clips], ["0", "300"])
            self.assertEqual([c.findtext("out") for c in vo_clips], ["300", "600"])
            # The insert clip's own audio (has_audio) still gets its own stereo pair, same as
            # any other video clip - the VO pausing doesn't mean the clip itself is silent.
            tracks = xml.findall(".//sequence/media/audio/track")
            self.assertEqual(len(tracks), 3)

    def test_no_inserts_keeps_the_single_continuous_voiceover_clip(self):
        # Zero-insert scripts (the overwhelming common case) must produce byte-identical
        # voiceover output to before this feature existed - same clip/file ids, one segment.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio_path = root / "voice.wav"
            audio_path.write_bytes(b"fake")
            path = root / "output.xml"
            xml_sequence(path, "Test", [], [], [], audio_path, 20, 1920, 1080)
            xml = ET.parse(str(path))
            clip = xml.find(".//sequence/media/audio/track/clipitem")
            self.assertEqual(clip.get("id"), "voiceover")
            self.assertEqual(clip.findtext("name"), "Voiceover - continuous")
            self.assertEqual(clip.findtext("start"), "0")
            self.assertEqual(clip.findtext("end"), str(600))


class PartialVoiceoverTests(unittest.TestCase):
    """The script and the voiceover don't have to cover the same cases."""
    SENTENCES = {
        1: "Sunlight warmed the quiet misty valley slowly across the morning while small birds sang softly above",
        2: "Lanterns glowed along the empty harbor road as weary fishermen coiled their heavy wet ropes before dawn",
        3: "The river ran cold under gray winter skies while patient villagers waited anxiously for the spring rain",
        4: "Copper kettles rattled on the crowded market stalls beneath striped canvas awnings during the busy noon",
        5: "Ancient clocks chimed across the silent monastery courtyard as pale monks carried lanterns toward the chapel",
        6: "Frozen lakes cracked loudly under the heavy northern moon while distant wolves howled across the endless pines",
    }

    def _make(self, root, script_cases, vo_cases, lead=0):
        import re
        import wave
        Image.new("RGB", (60, 40)).save(root / "img.png")
        body, rels, files = "", "", []
        for n in script_cases:
            # "The valley" also occurs in case 1's narration: a heading whose words match a stray spot in
            # a voiceover that has other cases is the realistic trap for locating where the script starts.
            body += (f"<w:p><w:r><w:t>Case {n}: The valley</w:t></w:r></w:p>"
                     f'<w:p><w:r><w:t>{self.SENTENCES[n]}.</w:t></w:r><w:r><w:t xml:space="preserve"> (</w:t></w:r>'
                     f'<w:hyperlink w:anchor="b{n}"><w:r><w:t>IMG {n}</w:t></w:r></w:hyperlink><w:r><w:t>)</w:t></w:r></w:p>'
                     f'<w:bookmarkStart w:id="{n}" w:name="b{n}"/><w:p><a:blip r:embed="a{n}"/></w:p>')
            rels += f'<Relationship Id="a{n}" Target="media/image{n}.png"/>'
            files.append(f"word/media/image{n}.png")
        doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
               'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>' + body + "</w:body></w:document>")
        with ZipFile(root / "script.docx", "w") as z:
            z.writestr("word/document.xml", doc)
            z.writestr("word/_rels/document.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + rels + "</Relationships>")
            for name in files:
                z.write(root / "img.png", name)
        spoken, t, starts = [], 0.0, {}
        for word in ("so", "welcome", "back", "to", "the", "show", "everyone")[:lead]:
            spoken.append({"word": word, "start": round(t, 3), "end": round(t + .4, 3)})
            t += .5
        for n in vo_cases:
            t += 2.0  # the pause between cases
            starts[n] = round(t, 3)
            for word in self.SENTENCES[n].split():
                spoken.append({"word": word.lower(), "start": round(t, 3), "end": round(t + .4, 3)})
                t += .5
        total = t + 2.0
        audio = root / "voice.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(2) * int(16000 * total))
        (root / "words.json").write_text(json.dumps({
            "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "words": spoken}))
        return audio, total, starts

    def _build(self, root, script_cases, vo_cases, cases=None, lead=0):
        audio, total, starts = self._make(root, script_cases, vo_cases, lead)
        report = build(root / "script.docx", audio, root / "Run" / "Timeline", words=root / "words.json", cases=cases)
        return report, total, starts

    def test_voiceover_that_covers_only_the_ticked_cases_builds(self):
        with tempfile.TemporaryDirectory() as temp:
            report, total, starts = self._build(Path(temp), range(1, 7), (3, 4), cases=(3, 4))
            split = report["case_split"]
            self.assertEqual(split["cut_start_seconds"], 0.0)
            self.assertAlmostEqual(split["cut_end_seconds"], total, delta=.01)  # nothing to cut: the VO is these cases
            self.assertEqual([c["name"] for c in report["clips"]], ["IMG_3_1", "IMG_4_1"])
            self.assertEqual([m["case"] for m in report["case_markers"]], [3, 4])
            self.assertAlmostEqual(report["case_markers"][0]["seconds"], starts[3], delta=.05)

    def test_ticking_one_case_still_cuts_between_cases_inside_a_partial_voiceover(self):
        with tempfile.TemporaryDirectory() as temp:
            report, total, starts = self._build(Path(temp), range(1, 7), (3, 4), cases=(3, 3))
            split = report["case_split"]
            self.assertEqual(split["cut_start_seconds"], 0.0)
            self.assertGreater(split["cut_end_seconds"], starts[4] - 2.0)  # inside the pause before case 4
            self.assertLess(split["cut_end_seconds"], starts[4])
            self.assertEqual([c["name"] for c in report["clips"]], ["IMG_3_1"])

    def test_ticking_cases_the_voiceover_lacks_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, r"Cases 1-2 aren't in the voiceover.*contain cases 3-4"):
                self._build(Path(temp), range(1, 7), (3, 4), cases=(1, 2))

    def test_whole_script_build_against_a_partial_voiceover_names_the_cases_it_has(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "seems to contain only cases 3-4; tick just those"):
                self._build(Path(temp), range(1, 7), (3, 4))

    def test_a_media_folder_with_a_restricted_character_is_warned_about(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio, _, _ = self._make(root, (3, 4), (3, 4))
            for folder, warned in (("Q&A", True), ("Plain", False)):
                report = build(root / "script.docx", audio, root / folder / "Timeline", words=root / "words.json",
                               media_dir=root / folder / "Media")
                found = [w for w in report["warnings"] if "Premiere may refuse to open" in w]
                self.assertEqual(bool(found), warned, folder)
                if warned:
                    self.assertIn("&", found[0])

    def test_voiceover_longer_than_the_script_is_trimmed_to_the_scripts_cases(self):
        for cases in (None, (3, 4)):
            with tempfile.TemporaryDirectory() as temp:
                report, total, starts = self._build(Path(temp), (3, 4), range(1, 5), cases=cases)
                split = report["case_split"]
                self.assertIsNotNone(split)
                self.assertGreater(split["cut_start_seconds"], starts[3] - 2.0)  # inside the pause before case 3
                self.assertLess(split["cut_start_seconds"], starts[3])
                self.assertAlmostEqual(split["cut_end_seconds"], total, delta=.01)
                self.assertLess(report["duration_seconds"], total - starts[3] + 2.0 + .01)
                self.assertEqual([c["name"] for c in report["clips"]], ["IMG_3_1", "IMG_4_1"])
                self.assertAlmostEqual(report["case_markers"][0]["seconds"], starts[3] - split["cut_start_seconds"], delta=.05)
                self.assertEqual(report["voiceover_offset_seconds"], split["cut_start_seconds"])

    def test_trailing_cases_the_script_lacks_are_trimmed_too(self):
        with tempfile.TemporaryDirectory() as temp:
            report, total, starts = self._build(Path(temp), (1, 2), range(1, 5))
            split = report["case_split"]
            self.assertEqual(split["cut_start_seconds"], 0.0)
            self.assertGreater(split["cut_end_seconds"], starts[3] - 2.0)
            self.assertLess(split["cut_end_seconds"], starts[3])

    def test_a_voiceover_that_matches_the_script_is_not_cut(self):
        with tempfile.TemporaryDirectory() as temp:
            report, total, starts = self._build(Path(temp), range(1, 5), range(1, 5))
            self.assertIsNone(report["case_split"])
            self.assertAlmostEqual(report["duration_seconds"], total, delta=.01)

    def test_a_short_unscripted_lead_in_is_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            report, total, starts = self._build(Path(temp), range(1, 5), range(1, 5), lead=7)
            self.assertIsNone(report["case_split"])
            self.assertAlmostEqual(report["duration_seconds"], total, delta=.01)


class PauseInsertTimelineTests(unittest.TestCase):
    """A pause-VO clip pushes everything after it later: audio parts, clips and markers alike."""
    LINES = ["Case 1 Start", "Officers arrived and searched the whole quiet house.",
             "Case 2 Next", "Later the detectives returned to interview every single neighbour again.",
             "What they found upstairs changed everything for them.",
             "Case 3 Last", "Finally the whole matter was closed for good."]

    def _make(self, root):
        import wave
        Image.new("RGB", (60, 40)).save(root / "img.png")
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doc = (f'<w:document xmlns:w="{w}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
               'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>'
               '<w:p><w:r><w:t>Case 1: Start</w:t></w:r></w:p>'
               f'<w:p><w:r><w:t>{self.LINES[1]}</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>Case 2: Next</w:t></w:r></w:p>'
               f'<w:p><w:r><w:t>{self.LINES[3]}</w:t></w:r></w:p>'
               '<w:p><w:hyperlink r:id="h1"><w:r><w:t>[ 2:07 - 2:10 ]</w:t></w:r></w:hyperlink></w:p>'
               f'<w:p><w:r><w:t>What they found upstairs changed everything for them</w:t></w:r><w:r><w:t xml:space="preserve"> (</w:t></w:r>'
               '<w:hyperlink w:anchor="b1"><w:r><w:t>IMG 1</w:t></w:r></w:hyperlink><w:r><w:t>).</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>Case 3: Last</w:t></w:r></w:p>'
               f'<w:p><w:r><w:t>{self.LINES[6]}</w:t></w:r></w:p>'
               '<w:bookmarkStart w:id="1" w:name="b1"/><w:p><a:blip r:embed="a1"/></w:p></w:body></w:document>')
        with ZipFile(root / "script.docx", "w") as z:
            z.writestr("word/document.xml", doc)
            z.writestr("word/_rels/document.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="h1" Target="https://youtu.be/fftGair1ZoA"/>'
                       '<Relationship Id="a1" Target="media/image1.png"/></Relationships>')
            z.write(root / "img.png", "word/media/image1.png")
        spoken, t, begins = [], 0.0, []
        for n, line in enumerate(self.LINES):
            t += 1.5
            begins.append(t)
            for word in line.split():
                spoken.append({"word": word.strip(".").lower(), "start": round(t, 3), "end": round(t + .4, 3)})
                t += .5
            if n == 3:
                t += 4.0  # where the pause clip goes
        total = t + 3
        audio = root / "vo.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(2) * int(16000 * total))
        (root / "words.json").write_text(json.dumps({
            "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "words": spoken}))
        (root / "clip.mp4").write_bytes(b"fake")
        return audio, total, begins

    def _build(self, root, out, cases=None):
        from unittest import mock

        def prepare(cues, *args, **kwargs):
            for cue in cues:
                if cue["kind"] == "video":
                    for ref in cue["refs"]:
                        ref["video_asset"] = {"path": str(root / "clip.mp4"), "width": 640, "height": 360,
                                              "kind": "video", "has_audio": False,
                                              "source_duration_frames": 2000, "title": "fake"}
                        ref.update(in_frame=100, out_frame=190, source_start=2.0, source_end=5.0)
            return [], []

        audio, total, begins = self._make(root)
        with mock.patch("youtube_media.prepare_sources", prepare):
            report = build(root / "script.docx", audio, root / out / "Timeline", words=root / "words.json",
                           download_videos=True, cases=cases)
        return report, ET.parse(str(root / out / "Timeline" / "Skeleton_full.xml")), total, begins

    def _check(self, xml, report, offset_frames):
        from skeleton_builder import FPS
        seq = xml.find("sequence")
        parts = seq.findall("media/audio/track[1]/clipitem")
        self.assertEqual(len(parts), 2)
        insert = next(c for c in seq.findall("media/video/track/clipitem") if "| INSERT" in c.findtext("name"))
        gap = int(insert.findtext("end")) - int(insert.findtext("start"))
        self.assertGreater(gap, 0)
        # The VO stops where the pause clip starts, resumes where it ends, and picks up the
        # recording exactly where it left off.
        self.assertEqual(int(parts[0].findtext("end")), int(insert.findtext("start")))
        self.assertEqual(int(parts[1].findtext("start")), int(insert.findtext("end")))
        self.assertEqual(int(parts[1].findtext("in")), int(parts[0].findtext("out")))
        self.assertEqual(int(parts[0].findtext("in")), offset_frames)
        # One recording, defined once and then referred to.
        files = seq.findall("media/audio/track[1]//file")
        self.assertEqual({f.get("id") for f in files}, {"voiceover-file"})
        self.assertEqual([len(f) > 0 for f in files], [True, False])
        # Every marker sits on the thing it names, after the shift.
        markers = {m.findtext("name"): m for m in seq.findall("marker")}
        image = next(c for c in seq.findall("media/video/track/clipitem") if c.findtext("name").startswith("IMG_"))
        self.assertEqual(markers[image.findtext("name")].findtext("in"), image.findtext("start"))
        video = next(m for n, m in markers.items() if n.startswith("VID_"))
        self.assertEqual(video.findtext("in"), insert.findtext("start"))
        self.assertEqual(int(video.findtext("out")) - int(video.findtext("in")), gap)
        return markers, gap, FPS

    def test_pause_clip_shifts_audio_parts_clips_and_markers_together(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, xml, total, begins = self._build(root, "W")
            markers, gap, fps = self._check(xml, report, 0)
            # Cases 1 and 2 are announced before the pause, so they stay put; case 3 comes after it
            # and moves later by exactly the pause clip's length.
            self.assertAlmostEqual(int(markers["Case 1: Start"].findtext("in")) / fps, begins[0], delta=.05)
            self.assertAlmostEqual(int(markers["Case 2: Next"].findtext("in")) / fps, begins[2], delta=.05)
            self.assertAlmostEqual(int(markers["Case 3: Last"].findtext("in")) / fps, begins[5] + gap / fps, delta=.05)

    def test_split_build_keeps_the_whole_recording_so_the_ends_can_be_restored(self):
        import wave
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, xml, total, begins = self._build(root, "B", cases=(2, None))
            cut = report["case_split"]["cut_start_seconds"]
            self.assertGreater(cut, 0)
            from skeleton_builder import FPS
            self._check(xml, report, round(cut * FPS))
            with wave.open(str(root / "B" / "Audio" / "voiceover.wav")) as wav:
                self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), total, delta=.05)
            part = xml.find("sequence/media/audio/track[1]/clipitem")
            self.assertGreaterEqual(int(part.findtext("duration")), round(total * FPS))
            self.assertEqual(report["voiceover_offset_seconds"], cut)
            # The timeline itself is only this range's stretch, plus the pause clip.
            self.assertLess(int(xml.findtext("sequence/duration")), round(total * FPS))
            # ...and the review page's player seeks into the whole recording from that cut.
            page = (root / "B" / "Review.html").read_text(encoding="utf-8")
            first = next(c for c in report["cues"] if c["start"] is not None)
            self.assertIn(f'data-time="{first["start"] + cut}"', page)


if __name__ == "__main__":
    unittest.main()
