import tempfile
import unittest
from pathlib import Path

from model.TaskResultExporter import first_existing_file


class ExportCandidateTests(unittest.TestCase):
    def test_numbered_timeline_beyond_configured_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["2026年9月17日-reels.mp4"] + [
                f"2026年9月17日-reels{number}.mp4" for number in range(1, 11)
            ]
            (root / "2026年9月17日-reels11.mp4").touch()
            (root / "2026年9月17日-reels12.mp4").touch()
            self.assertEqual(first_existing_file(root, names).name, "2026年9月17日-reels12.mp4")
            (root / names[0]).touch()
            self.assertEqual(first_existing_file(root, names).name, names[0])

    def test_unrelated_or_unconfigured_numbered_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2026年9月17日-reels11.mp4").touch()
            self.assertIsNone(first_existing_file(root, ["2026年9月17日-other.mp4"]))
            self.assertIsNone(first_existing_file(root, ["2026年9月17日-reels.mp4"]))


if __name__ == "__main__":
    unittest.main()
