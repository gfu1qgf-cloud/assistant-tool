import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtWidgets
from model.AppIcon import application_icon


class AppIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_source_icon_and_window_inheritance(self):
        root = Path(__file__).resolve().parents[1]
        icon = application_icon(root)
        self.assertFalse(icon.isNull())
        old = self.app.windowIcon()
        try:
            self.app.setWindowIcon(icon)
            dialog = QtWidgets.QDialog()
            self.assertFalse(dialog.windowIcon().isNull())
        finally:
            self.app.setWindowIcon(old)

    def test_bundle_fallback(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(application_icon(empty, root).isNull())
            self.assertTrue(application_icon(empty, empty).isNull())

    def test_packaging_embeds_the_icon(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('icon=str(app_icon)', (root/'packaging/AssistantTool.spec').read_text(encoding='utf-8'))
        workflow = (root/'.github/workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn('Copy-Item app_icon.png', workflow)
        self.assertIn('Copy-Item app_icon.ico', workflow)


if __name__ == '__main__':
    unittest.main()
