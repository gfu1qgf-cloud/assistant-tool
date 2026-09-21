import tempfile
import unittest
from pathlib import Path

from model.ProjectInitializer import initialize_project_directory


class ProjectInitializerTests(unittest.TestCase):
    def test_copies_template_once_and_never_overwrites_existing_table(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            template = root / "template.ods"
            template.write_bytes(b"template-v1")
            project = root / "0918"

            first = initialize_project_directory(
                project,
                "tasks.ods",
                [template],
            )
            self.assertTrue(first["copied"])
            self.assertEqual((project / "tasks.ods").read_bytes(), b"template-v1")
            self.assertTrue((project / "result").is_dir())

            (project / "tasks.ods").write_bytes(b"user-data")
            template.write_bytes(b"template-v2")
            second = initialize_project_directory(
                project,
                "tasks.ods",
                [template],
            )
            self.assertFalse(second["copied"])
            self.assertEqual((project / "tasks.ods").read_bytes(), b"user-data")

    def test_creates_project_but_reports_missing_template(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "0918"
            result = initialize_project_directory(
                project,
                "tasks.ods",
                [Path(temp) / "missing.ods"],
            )
            self.assertTrue(result["missing_template"])
            self.assertTrue(project.is_dir())
            self.assertFalse((project / "tasks.ods").exists())


if __name__ == "__main__":
    unittest.main()
