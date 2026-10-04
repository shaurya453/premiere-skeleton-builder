import re
import tempfile
import unittest
from pathlib import Path

from lxml import etree as ET

from media_names import media_stem, rename_media
from tests import test_builder


class MediaStemTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(media_stem("IMG", 5, 1), "IMG_5_1")
        self.assertEqual(media_stem("VID", 12, 3), "VID_12_3")
        self.assertEqual(media_stem("IMG", 0, 2), "IMG_INTRO_2")
        self.assertEqual(media_stem("IMG", None, 4), "IMG_4")
        self.assertEqual(media_stem("GUIDE", 6, 1), "GUIDE_6_1")


class RenameMediaTests(unittest.TestCase):
    def _files(self, root, *names):
        paths = []
        for name in names:
            path = root / name
            path.write_bytes(name.encode())
            paths.append(path)
        return paths

    def test_numbering_restarts_in_every_case_and_follows_script_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b, c, d = self._files(root, "web_aaa.png", "asset_02.jpg", "web_ccc.png", "web_ddd.png")
            assets = [{"path": str(p)} for p in (a, b, c, d)]
            rename_media(list(zip(assets, (0, 5, 5, 6))), "IMG")
            self.assertEqual([Path(x["path"]).name for x in assets],
                             ["IMG_INTRO_1.png", "IMG_5_1.jpg", "IMG_5_2.png", "IMG_6_1.png"])
            self.assertEqual(sorted(p.name for p in root.iterdir()),
                             ["IMG_5_1.jpg", "IMG_5_2.png", "IMG_6_1.png", "IMG_INTRO_1.png"])
            self.assertEqual((root / "IMG_5_2.png").read_bytes(), b"web_ccc.png")

    def test_a_file_used_twice_keeps_the_name_from_its_first_use(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (shared,) = self._files(root, "web_aaa.png")
            first, again = {"path": str(shared)}, {"path": str(shared)}
            rename_media([(first, 5), (again, 6)], "IMG")
            self.assertEqual(Path(first["path"]).name, "IMG_5_1.png")
            self.assertEqual(again["path"], first["path"])
            self.assertEqual([p.name for p in root.iterdir()], ["IMG_5_1.png"])

    def test_script_without_cases_gets_plain_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = self._files(root, "x.mp4", "y.mp4")
            assets = [{"path": str(a)}, {"path": str(b)}]
            rename_media([(assets[0], None), (assets[1], None)], "VID")
            self.assertEqual([Path(x["path"]).name for x in assets], ["VID_1.mp4", "VID_2.mp4"])

    def test_a_file_already_holding_a_target_name_is_not_lost(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = self._files(root, "IMG_5_2.png", "other.png")
            assets = [{"path": str(second)}, {"path": str(first)}]
            rename_media([(assets[0], 5), (assets[1], 5)], "IMG")
            self.assertEqual((root / "IMG_5_1.png").read_bytes(), b"other.png")
            self.assertEqual((root / "IMG_5_2.png").read_bytes(), b"IMG_5_2.png")

    def test_assets_without_a_path_are_skipped(self):
        self.assertEqual(rename_media([({"path": None}, 1), ({}, 1)], "IMG"), {})


class BuildNamingTests(unittest.TestCase):
    def test_files_clips_and_guide_cards_carry_the_case_and_number(self):
        helper = test_builder.PartialVoiceoverTests()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, _, _ = helper._build(root, (1, 2, 3, 4), (1, 2, 3, 4))
            images = sorted(p.name for p in (root / "Run" / "Media" / "Images").iterdir())
            self.assertEqual(images, ["IMG_1_1.png", "IMG_2_1.png", "IMG_3_1.png", "IMG_4_1.png"])
            xml = ET.parse(str(root / "Run" / "Timeline" / "Skeleton_full.xml")).getroot()
            clips = xml.findall(".//media/video/track/clipitem")
            names = [c.findtext("name") for c in clips]
            self.assertEqual([n for n in names if n.startswith("IMG_")], ["IMG_1_1", "IMG_2_1", "IMG_3_1", "IMG_4_1"])
            for clip in clips:
                url = clip.findtext("file/pathurl")
                if clip.findtext("name").startswith("IMG_"):
                    self.assertTrue(url.endswith(f"/Images/{clip.findtext('name')}.png"), url)
            guides = [n for n in names if n.startswith("GUIDE")]
            self.assertTrue(guides)
            for guide in guides:
                self.assertRegex(guide, r"^GUIDE_(INTRO|\d+)_\d+ - visual to add$")
            # Numbers inside a case run 1, 2, 3 in timeline order.
            by_case = {}
            for guide in guides:
                case, number = re.match(r"GUIDE_(\w+)_(\d+)", guide).groups()
                by_case.setdefault(case, []).append(int(number))
            for numbers in by_case.values():
                self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
            markers = [m.findtext("name") for m in xml.findall("sequence/marker")] or \
                      [m.findtext("name") for m in xml.findall("marker")]
            self.assertIn("IMG_2_1", markers)
            self.assertFalse(list((root / "Run" / "Media" / "Images").glob("web_*")))
            self.assertFalse(list((root / "Run" / "Media" / "Images").glob("asset_*")))


if __name__ == "__main__":
    unittest.main()
