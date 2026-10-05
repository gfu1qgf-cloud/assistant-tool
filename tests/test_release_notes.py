import contextlib
import io
import os
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
helpers = run_path(str(ROOT / "packaging/release_notes.py"))
extract = helpers["extract_release_notes"]
main = helpers["main"]

from qt_compat import QtWidgets
from PYUI.release_notes_pyui import ReleaseNotesDialog
from PYUI.utility_managers_pyui import AboutDialog


class ReleaseNotesTests(unittest.TestCase):
    def test_all_historical_release_tags_have_specific_notes(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for number in range(19):
            tag = f"v1.0.{number}"
            with self.subTest(tag=tag):
                notes = extract(text, tag)
                self.assertTrue(notes.startswith(f"## {tag} "))
                self.assertEqual(notes.count("\n## v"), 0)
                self.assertNotIn("批量文案视频", notes)
                self.assertNotIn("视频拼接插件", notes)

    def test_current_major_release_has_its_own_new_features(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        notes = extract(text, "v2.0.0")
        for feature in ("静态文字批量视频插件", "视频拼接插件", "BUILD_INFO.json", "OpenCV-contrib 4.14.0.94"):
            self.assertIn(feature, notes)
        self.assertEqual(notes.count("\n## v"), 0)

    def test_exact_version_not_prefix_and_only_its_body(self):
        text = "# 更新日志\n\n## v1.0.10 2026-09-21\n\n- 十。\n\n## v1.0.1 2026-08-26\n\n- 一。\n"
        self.assertEqual(extract(text, "v1.0.1"), "## v1.0.1 2026-08-26\n\n- 一。\n")
        self.assertNotIn("一。", extract(text, "v1.0.10"))

    def test_unknown_duplicate_empty_or_placeholder_notes_fail(self):
        invalid = [
            "## v1.0.1\n- 不同版本。\n",
            "## v1.0.2\n- 修复一。\n## v1.0.2\n- 修复二。\n",
            "## v1.0.2\n\n",
            "## v1.0.2\n没有具体条目\n",
            "## v1.0.2\n- TODO\n",
            "## v1.0.2\n- 待补充\n",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract(text, "v1.0.2")

    def test_invalid_tag_cannot_be_used_as_arbitrary_match(self):
        for tag in ("v*", "../v1.0.1", "v1.0", "v1.0.1\n"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                extract("## v1.0.1\n- 修复。\n", tag)

    def test_prerelease_tags_supported(self):
        self.assertIn("预览", extract("## v2.0.0-rc.1\n- 预览。\n", "v2.0.0-rc.1"))

    def test_cli_writes_utf8_notes_and_refuses_missing_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "CHANGELOG.md"
            output = Path(temp) / "dist" / "notes.md"
            source.write_text("## v1.0.2 2026-09-01\n\n- 中文修复。\n", encoding="utf-8-sig")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--version", "v1.0.2", "--changelog", str(source), "--output", str(output)]), 0)
            self.assertIn("中文修复", output.read_text(encoding="utf-8"))
            original = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--version", "v1.0.3", "--changelog", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_bytes(), original)

    def test_workflow_uses_same_version_notes_and_packages_complete_changelog(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('--version "$env:GITHUB_REF_NAME"', workflow)
        self.assertIn("body_path: dist_final/RELEASE_NOTES.md", workflow)
        self.assertIn("generate_release_notes: false", workflow)
        self.assertIn("Copy-Item CHANGELOG.md", workflow)
        self.assertLess(workflow.index("name: Fetch pinned standard FFmpeg runtime"), workflow.index("name: Run tests"))
        self.assertLess(workflow.index("name: Fetch pinned mpv runtime"), workflow.index("name: Run tests"))


class ReleaseNotesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_viewer_renders_local_changelog_and_searches_versions(self):
        dialog = ReleaseNotesDialog(changelog_path=ROOT / "CHANGELOG.md")
        self.addCleanup(dialog.close)
        self.assertIn("v1.0.18", dialog.browser.toPlainText())
        self.assertTrue(dialog.browser.isReadOnly())
        self.assertFalse(dialog.browser.openExternalLinks())
        dialog.search.setText("v1.0.7")
        dialog.find_next()
        self.assertEqual(dialog.browser.textCursor().selectedText(), "v1.0.7")
        dialog.find_next()
        self.assertEqual(dialog.browser.textCursor().selectedText(), "v1.0.7")

    def test_missing_changelog_has_actionable_message(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "CHANGELOG.md"
            dialog = ReleaseNotesDialog(changelog_path=path)
            self.addCleanup(dialog.close)
            self.assertIn(str(path), dialog.browser.toPlainText())
            self.assertIn("完整发布包", dialog.browser.toPlainText())

    def test_about_button_opens_viewer_without_touching_maintenance_notes(self):
        dialog = AboutDialog()
        self.addCleanup(dialog.close)
        before = dialog.lesson_browser.toPlainText()
        with patch("PYUI.utility_managers_pyui.ReleaseNotesDialog") as viewer:
            dialog.release_notes_button.click()
        viewer.assert_called_once_with(dialog)
        viewer.return_value.exec.assert_called_once_with()
        self.assertEqual(dialog.lesson_browser.toPlainText(), before)


if __name__ == "__main__":
    unittest.main()
