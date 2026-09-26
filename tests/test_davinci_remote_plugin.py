import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from app_plugins.builtin.davinci_remote import DaVinciRemotePlugin
from app_plugins.builtin.davinci_remote.ui import DaVinciRemoteDialog
from app_plugins.host import PluginHost
import davinci_remote_worker
from davinci_legacy import subtitle_review


class FakeWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.config = {}
        self.logs = []

    def load_config(self):
        return dict(self.config)

    def saveCurrentConfig(self):
        self.config.update(self.plugin_host.update_runtime_config({}))
        return True

    def appendLog(self, message, end="", level=None):
        self.logs.append(str(message))


class DaVinciRemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.window = FakeWindow()
        self.window.plugin_host = PluginHost(self.window)
        self.plugin = self.window.plugin_host.install(DaVinciRemotePlugin())

    def tearDown(self):
        if self.plugin.dialog is not None:
            self.plugin.dialog.close()
        self.window.close()

    def test_mounts_into_existing_remote_tab(self):
        tabs = QtWidgets.QTabWidget(self.window)
        placeholder = QtWidgets.QWidget(tabs)
        layout = QtWidgets.QVBoxLayout(placeholder)
        layout.addWidget(QtWidgets.QTextEdit("旧占位内容", placeholder))
        tabs.addTab(placeholder, "旧页面")
        pages = self.window.plugin_host.attach_tab_area(tabs, placeholder)
        self.assertEqual(len(pages), 1)
        self.assertEqual(tabs.count(), 1)
        self.assertEqual(tabs.tabText(0), "达芬奇遥控器")
        self.assertIsNotNone(self.plugin.panel)
        self.assertEqual(len(placeholder.findChildren(QtWidgets.QTextEdit)), 1)
        self.assertFalse(placeholder.findChildren(QtWidgets.QTextEdit)[0].isVisible())

    def test_three_qt_tools_expose_and_save_original_settings(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        self.assertEqual(dialog.tabs.count(), 3)
        self.assertEqual(dialog.track_tabs.count(), 2)
        dialog.export_category.setCurrentIndex(1)
        dialog.export_audio.setValue(3)
        settings = dialog._export_settings()
        self.assertEqual(settings["category"], "口播")
        self.assertEqual(settings["audio_track"], 3)
        dialog.correct_text.setPlainText("one two three")
        with patch.object(dialog, "_start") as run:
            dialog._run_subtitle()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1]["settings"]["correct_text"], "one two three")
        self.assertNotIn("correct_text", self.plugin.settings["subtitle"])

    def test_track_uses_confirmation_before_destructive_change(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        with patch.object(QtWidgets.QMessageBox, "question", return_value=(
            QtWidgets.QMessageBox.StandardButton.No
        )), patch.object(dialog, "_start") as run:
            dialog._run_track_fill()
        run.assert_not_called()

    def test_subtitle_worker_returns_review_data_without_fusion_dialog(self):
        class Resolve:
            def Fusion(self):
                return object()

        def fake_process(*_args):
            subtitle_review.show_review_dialog(
                None, None, None, None, "核对完成",
                [{"index": 3, "start": 42, "original": "bad",
                  "review_text": "good", "color_name": "橘黄",
                  "severity": "minor", "accuracy": 50.0,
                  "error_ratio": 50.0, "error_count": 1}],
                "AI 核对内容",
            )
            return True

        old_review = subtitle_review.show_review_dialog
        old_result = subtitle_review.show_result_dialog
        try:
            with patch.object(davinci_remote_worker, "_current_timeline", return_value=(
                Resolve(), object(), object()
            )), patch.object(subtitle_review, "get_all_subtitle_items", return_value=[
                {"original_text": "bad"}
            ]), patch.object(subtitle_review, "process_subtitles", side_effect=fake_process):
                result = davinci_remote_worker._subtitle({
                    "settings": {"correct_text": "good"}
                })
        finally:
            subtitle_review.show_review_dialog = old_review
            subtitle_review.show_result_dialog = old_result
        self.assertEqual(result["reviews"][0]["review_text"], "good")
        self.assertEqual(result["ai_review_prompt"], "AI 核对内容")


if __name__ == "__main__":
    unittest.main()
