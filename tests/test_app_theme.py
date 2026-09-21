import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtGui, QtWidgets

from app_plugins.builtin.chrome_launcher import ChromeLauncherPlugin
from model.AppTheme import (
    DEFAULT_UI_THEME,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
    apply_ui_theme,
    load_configured_ui_theme,
    normalize_ui_theme,
)


def _luminance(color):
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(first, second):
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class AppThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_missing_and_invalid_config_default_to_light(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(DEFAULT_UI_THEME, load_configured_ui_theme(path))
            path.write_text(json.dumps({"ui_theme": "invalid"}), encoding="utf-8")
            self.assertEqual(THEME_LIGHT, load_configured_ui_theme(path))
        self.assertEqual(THEME_LIGHT, normalize_ui_theme(None))

    def test_all_supported_theme_names_round_trip(self):
        for theme in (THEME_LIGHT, THEME_DARK, THEME_SYSTEM):
            self.assertEqual(theme, normalize_ui_theme(theme))

    def test_light_and_dark_palettes_keep_core_text_readable(self):
        roles = QtGui.QPalette.ColorRole
        for theme in (THEME_LIGHT, THEME_DARK):
            self.assertEqual(theme, apply_ui_theme(self.app, theme))
            palette = self.app.palette()
            self.assertGreaterEqual(
                _contrast(palette.color(roles.WindowText), palette.color(roles.Window)),
                7.0,
            )
            self.assertGreaterEqual(
                _contrast(palette.color(roles.Text), palette.color(roles.Base)),
                7.0,
            )
            self.assertGreaterEqual(
                _contrast(palette.color(roles.ButtonText), palette.color(roles.Button)),
                7.0,
            )

    def test_chrome_launcher_uses_native_qt6_dialog_exec(self):
        plugin = ChromeLauncherPlugin()
        plugin.dialog = QtWidgets.QDialog()
        QtCore.QTimer.singleShot(0, plugin.dialog.reject)
        self.assertEqual(QtWidgets.QDialog.Rejected, plugin.open_launcher())


if __name__ == "__main__":
    unittest.main()
