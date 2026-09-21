import re
import unittest
from pathlib import Path

import qt_compat


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Qt6MigrationTests(unittest.TestCase):
    def test_runtime_is_pyqt6(self):
        self.assertTrue(qt_compat.QtCore.PYQT_VERSION_STR.startswith("6."))
        self.assertTrue(qt_compat.QtCore.QT_VERSION_STR.startswith("6."))

    def test_production_source_has_no_pyqt5_imports(self):
        import_pattern = re.compile(r"^\s*(?:from|import)\s+PyQt5\b", re.MULTILINE)
        roots = ("main.py", "PYUI", "QTUI", "QtPlus", "model", "app_plugins")
        offenders = []
        for root_name in roots:
            root = PROJECT_ROOT / root_name
            paths = [root] if root.is_file() else root.rglob("*.py")
            for path in paths:
                if import_pattern.search(path.read_text(encoding="utf-8-sig")):
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual([], offenders)

    def test_requirements_select_only_pyqt6(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8-sig"
        ).lower()
        self.assertIn("pyqt6==", requirements)
        self.assertNotIn("pyqt5==", requirements)
        self.assertNotIn("pyqt5_sip", requirements)

    def test_smart_player_uses_qt6_multimedia_api(self):
        source = (
            PROJECT_ROOT
            / "app_plugins"
            / "builtin"
            / "smart_video_editor"
            / "player.py"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("QAudioOutput", source)
        self.assertIn("videoSink()", source)
        self.assertIn("setSource(", source)
        self.assertNotIn("QMediaContent", source)
        self.assertNotIn("QMediaPlayer.VideoSurface", source)

    def test_production_code_does_not_use_removed_exec_spelling(self):
        offenders = []
        for root_name in ("main.py", "PYUI", "QTUI", "QtPlus", "model", "app_plugins"):
            root = PROJECT_ROOT / root_name
            paths = [root] if root.is_file() else root.rglob("*.py")
            for path in paths:
                if ".exec_(" in path.read_text(encoding="utf-8-sig"):
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
