import os
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from unittest.mock import patch


helpers = run_path(str(Path(__file__).resolve().parents[1] / "packaging/run_tests_isolated.py"))
environment = helpers["test_environment"]


class TestRunnerTests(unittest.TestCase):
    def test_canonicalizes_all_temp_variables_without_changing_parent_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            value = str(Path(folder) / "child" / "..")
            with patch.dict(os.environ, {"TEMP": value, "TMP": value, "TMPDIR": value}, clear=True):
                result = environment()
                for name in ("TEMP", "TMP", "TMPDIR"):
                    self.assertEqual(result[name], str(Path(value).resolve()))
                    self.assertEqual(os.environ[name], value)

    def test_keeps_other_settings_and_explicit_qt_backend(self):
        with patch.dict(os.environ, {"PYTHONPATH": "unchanged", "QT_QPA_PLATFORM": "minimal"}, clear=True):
            result = environment()
            self.assertEqual(result["PYTHONPATH"], "unchanged")
            self.assertEqual(result["QT_QPA_PLATFORM"], "minimal")

    def test_default_offscreen_and_absent_temp_not_invented(self):
        with patch.dict(os.environ, {}, clear=True):
            result = environment()
            self.assertEqual(result["QT_QPA_PLATFORM"], "offscreen")
            for name in ("TEMP", "TMP", "TMPDIR"):
                self.assertNotIn(name, result)


if __name__ == "__main__":
    unittest.main()
