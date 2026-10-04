import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jobs
from drive_audio import _safe_name
from google_docs import _sanitize_filename
from job_worker import stage
from safe_names import RESTRICTED, clean_filename, clean_name, restricted_characters


class CleanNameTests(unittest.TestCase):
    def test_the_failing_folder_name_loses_its_ampersand(self):
        self.assertEqual(clean_name("MOST DISTURBING CRIMES AT 7-ELEVEN (CASE 5&6)"),
                         "MOST DISTURBING CRIMES AT 7-ELEVEN (CASE 5-6)")

    def test_every_restricted_character_becomes_a_dash(self):
        for char in sorted(RESTRICTED):
            self.assertEqual(clean_name(f"a{char}b"), "a-b", repr(char))

    def test_control_characters_are_replaced_too(self):
        self.assertEqual(clean_name("a\tb\nc\x00d"), "a-b-c-d")

    def test_safe_characters_are_left_alone(self):
        text = "Nikki (cases 1) - Part_2 v1.5 Café ’ok"
        self.assertEqual(clean_name(text), text)

    def test_runs_collapse_and_the_ends_are_trimmed(self):
        self.assertEqual(clean_name("Wait!!! What?!"), "Wait- What")
        self.assertEqual(clean_name("&Q&A&"), "Q-A")
        self.assertEqual(clean_name("5 & 6"), "5 - 6")

    def test_empty_results_use_the_fallback(self):
        self.assertEqual(clean_name("&&&", fallback="Untitled script"), "Untitled script")
        self.assertEqual(clean_name(None), "Untitled")

    def test_windows_device_names_are_not_used_as_is(self):
        self.assertEqual(clean_name("CON"), "CON-")
        self.assertEqual(clean_name("nul"), "nul-")

    def test_limit_applies_after_cleaning(self):
        self.assertEqual(clean_name("a&" * 50, limit=10), "a-a-a-a-a")


class CleanFilenameTests(unittest.TestCase):
    def test_extension_is_kept(self):
        self.assertEqual(clean_filename("Script: Part 1/2?.docx"), "Script- Part 1-2.docx")
        self.assertEqual(clean_filename("Q&A.MP3"), "Q-A.mp3")

    def test_a_dot_inside_the_name_is_not_taken_for_an_extension(self):
        self.assertEqual(clean_filename("v1.5 & more"), "v1.5 - more")

    def test_fallback(self):
        self.assertEqual(clean_filename("???", fallback="drive_1.mp3"), "drive_1.mp3")


class RestrictedCharactersTests(unittest.TestCase):
    def test_drive_letter_and_separators_are_not_flagged(self):
        self.assertEqual(restricted_characters(r"E:\Apps\Skeleton Builder\Projects\Nikki (cases 1)"), "")
        self.assertEqual(restricted_characters("/Users/ana/Projects/Case 1"), "")

    def test_a_folder_you_chose_is_flagged(self):
        self.assertEqual(restricted_characters(r"E:\Q&A, 2024\Projects\Media"), "&,")
        self.assertEqual(restricted_characters("/Users/ana/Bob's Projects"), "'")


class CallersUseTheCleanerTests(unittest.TestCase):
    def test_project_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = jobs.unique_folder(tmp, "CASE 5&6")
            self.assertEqual(first.name, "CASE 5-6")
            first.mkdir()
            self.assertEqual(jobs.unique_folder(tmp, "CASE 5&6").name, "CASE 5-6 (2)")

    def test_project_folder_with_nothing_usable(self):
        self.assertEqual(jobs.safe_name("???"), "Untitled script")

    def test_downloaded_script_name(self):
        self.assertEqual(_sanitize_filename("Script: Part 1/2?"), "Script- Part 1-2.docx")
        self.assertEqual(_sanitize_filename("CASE 5&6.docx"), "CASE 5-6.docx")
        self.assertEqual(_sanitize_filename("???"), "Google_Doc_Script.docx")

    def test_downloaded_voiceover_name(self):
        self.assertEqual(_safe_name("Crimes (CASE 5&6).mp3", "drive_x.mp3"), "Crimes (CASE 5-6).mp3")

    def test_staged_inputs_get_clean_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "CASE 5&6.docx"
            source.write_bytes(b"x")
            target_dir = Path(tmp) / "Script"
            target_dir.mkdir()
            staged = stage(source, target_dir)
            self.assertEqual(staged.name, "CASE 5-6.docx")
            self.assertTrue(staged.is_file())


if __name__ == "__main__":
    unittest.main()
