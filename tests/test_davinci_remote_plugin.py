import os
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from app_plugins.builtin.davinci_remote import DaVinciRemotePlugin
from app_plugins.builtin.davinci_remote.ui import DaVinciRemoteDialog
from app_plugins.host import PluginHost
import davinci_remote_worker
from davinci_legacy import subtitle_review
from davinci_legacy.timeline_naming import get_timeline_task_name


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

    def test_four_qt_tools_expose_and_save_original_settings(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        self.assertEqual(dialog.tabs.count(), 4)
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

    def test_probe_replaces_task_name_from_previous_timeline(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        dialog.export_name.setText("旧时间线任务名")
        dialog._handle_result("probe", {
            "project": "项目", "timeline": "新时间线", "task_name": "0930",
            "subtitle_count": 0, "subtitles": [],
            "video_tracks": [{"track": 1, "count": 3}],
        })
        self.assertEqual(dialog.export_name.text(), "0930")
        self.assertIn("新时间线", dialog.connection.text())
        self.assertIn("V1：3 段", dialog.video_info.text())

    def test_timeline_date_name_does_not_require_resolve_sdk(self):
        class Timeline:
            def GetName(self):
                return "2026年9月17日-reels11"

        self.assertEqual(get_timeline_task_name(Timeline()), "0917")

    def test_probe_reads_video_tracks_even_without_subtitles(self):
        class Named:
            def __init__(self, name):
                self.name = name

            def GetName(self):
                return self.name

        class Timeline(Named):
            def GetTrackCount(self, kind):
                self.kind = kind
                return 2

            def GetItemListInTrack(self, kind, number):
                return [object()] * (3 if number == 1 else 1)

        timeline = Timeline("当前时间线")
        with patch.object(davinci_remote_worker, "_current_timeline", return_value=(
            object(), Named("项目"), timeline
        )), patch.object(subtitle_review, "get_all_subtitle_items", return_value=[]):
            result = davinci_remote_worker._probe({})
        self.assertEqual(timeline.kind, "video")
        self.assertEqual(result["video_tracks"], [
            {"track": 1, "count": 3}, {"track": 2, "count": 1},
        ])

    def test_connect_reads_fresh_timeline_without_foregrounding_resolve(self):
        old, current = object(), object()
        api = MagicMock()
        resolve = api.scriptapp.return_value
        project = resolve.GetProjectManager.return_value.GetCurrentProject.return_value
        project.GetCurrentTimeline.side_effect = [old, current]
        with patch.object(davinci_remote_worker, "_resolve_module", return_value=api), \
                patch.object(davinci_remote_worker.time, "sleep"), \
                patch("ctypes.WinDLL", create=True) as win_api:
            result = davinci_remote_worker._current_timeline()
        win_api.assert_not_called()
        self.assertIs(result[2], current)
        self.assertEqual(project.GetCurrentTimeline.call_count, 2)

    def test_node_presets_survive_restart_and_keep_other_settings(self):
        self.plugin.settings["export"] = {"category": "reels"}
        self.window.config["unrelated"] = "keep"
        entry = self.plugin.save_fusion_preset("常用动画", "{ Tools = {} }", {
            "entries": ["A.Input"], "exit": "A.Output", "track": 8, "clip_keys": ["old"],
        })
        self.assertEqual(self.window.config["unrelated"], "keep")
        self.assertEqual(self.plugin.settings["export"], {"category": "reels"})
        self.assertNotIn("track", entry)
        self.assertNotIn("clip_keys", entry)
        window = FakeWindow()
        window.config = deepcopy(self.window.config)
        window.plugin_host = PluginHost(window)
        try:
            reopened = window.plugin_host.install(DaVinciRemotePlugin())
            self.assertEqual(reopened.fusion_presets(), [entry])
            copy = reopened.fusion_presets()
            copy[0]["text"] = "must not mutate"
            self.assertEqual(reopened.fusion_presets()[0]["text"], entry["text"])
        finally:
            window.close()

    def test_node_preset_overwrite_rename_delete_and_validation(self):
        with self.assertRaises(ValueError):
            self.plugin.save_fusion_preset("", "nodes")
        with self.assertRaises(ValueError):
            self.plugin.save_fusion_preset("name", "  ")
        original = self.plugin.save_fusion_preset("Zoom", "one")
        replaced = self.plugin.save_fusion_preset("zoom", "two")
        self.assertEqual(original["id"], replaced["id"])
        self.assertEqual(len(self.plugin.fusion_presets()), 1)
        second = self.plugin.save_fusion_preset("second", "three")
        with self.assertRaises(ValueError):
            self.plugin.rename_fusion_preset(second["id"], "ZOOM")
        self.plugin.rename_fusion_preset(second["id"], "renamed")
        self.assertEqual(self.plugin.fusion_presets()[1]["name"], "renamed")
        self.assertTrue(self.plugin.delete_fusion_preset(original["id"]))
        self.assertFalse(self.plugin.delete_fusion_preset(original["id"]))
        self.assertEqual(len(self.plugin.fusion_presets()), 1)

    def test_failed_node_preset_save_restores_in_memory_library(self):
        self.plugin.save_fusion_preset("original", "nodes")
        before = deepcopy(self.plugin.settings)
        with patch.object(self.plugin.context, "save_config", return_value=False):
            with self.assertRaises(RuntimeError):
                self.plugin.save_fusion_preset("new", "different")
        self.assertEqual(self.plugin.settings, before)
        with patch.object(self.plugin.context, "save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.plugin.delete_fusion_preset(self.plugin.fusion_presets()[0]["id"])
        self.assertEqual(self.plugin.settings, before)

    def _fusion_preview_result(self):
        return {
            "inputs": ["A.Input", "B.Foreground"], "outputs": ["A.Output", "B.Output"],
            "nodes": ["A (Transform)", "B (Merge)"], "internal_links": [],
            "allowed_exits": {"A.Input": ["A.Output", "B.Output"], "B.Foreground": ["B.Output"]},
            "project": "new timeline", "track": 1, "digest": "digest",
            "clips": [{"name": "new clip", "key": "fresh", "status": "可应用"}],
        }

    def test_loading_node_preset_is_offline_and_restores_ports_after_fresh_preview(self):
        entry = self.plugin.save_fusion_preset("动画", "nodes", {
            "entries": ["B.Foreground"], "exit": "B.Output",
        })
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        dialog.fusion_presets.setCurrentIndex(dialog.fusion_presets.findData(entry["id"]))
        with patch.object(dialog, "_start") as start:
            dialog._load_fusion_preset()
        start.assert_not_called()
        self.assertEqual(dialog.fusion_text.toPlainText(), "nodes")
        self.assertFalse(dialog.fusion_apply.isEnabled())
        dialog._handle_result("fusion_preview", self._fusion_preview_result())
        self.assertEqual(dialog._selected_fusion_entries(), ["B.Foreground"])
        self.assertEqual(dialog.fusion_exit.currentData(), "B.Output")
        self.assertTrue(dialog.fusion_apply.isEnabled())
        dialog.fusion_track.setValue(2)
        self.assertFalse(dialog.fusion_apply.isEnabled())
        dialog._handle_result("fusion_preview", self._fusion_preview_result())
        self.assertEqual(dialog._selected_fusion_entries(), ["B.Foreground"])
        dialog.fusion_text.setPlainText("edited nodes")
        self.assertIsNone(dialog._fusion_pending_mapping)
        self.assertFalse(dialog.fusion_apply.isEnabled())

    def test_save_node_ui_keeps_mapping_and_cancelled_overwrite_does_not_save(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        dialog.fusion_text.setPlainText("nodes")
        dialog._handle_result("fusion_preview", self._fusion_preview_result())
        with patch.object(QtWidgets.QInputDialog, "getText", return_value=("动画", True)):
            dialog._save_fusion_preset()
        entry = self.plugin.fusion_presets()[0]
        self.assertEqual(entry["entries"], ["A.Input"])
        self.assertEqual(entry["exit"], "A.Output")
        dialog.fusion_text.setPlainText("changed nodes")
        with patch.object(QtWidgets.QInputDialog, "getText", return_value=("动画", True)), \
                patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
            dialog._save_fusion_preset()
        self.assertEqual(self.plugin.fusion_presets()[0], entry)

    def test_node_preset_with_missing_output_does_not_silently_apply(self):
        entry = self.plugin.save_fusion_preset("动画", "nodes", {
            "entries": ["A.Input"], "exit": "Missing.Output",
        })
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        dialog.fusion_presets.setCurrentIndex(dialog.fusion_presets.findData(entry["id"]))
        dialog._load_fusion_preset()
        dialog._handle_result("fusion_preview", self._fusion_preview_result())
        self.assertFalse(dialog.fusion_apply.isEnabled())
        self.assertIsNone(dialog.fusion_exit.currentData())

    def test_fusion_tab_requires_fresh_preview_and_passes_selected_ports(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        dialog.fusion_text.setPlainText("Fusion template")
        with patch.object(dialog, "_start") as run:
            dialog._run_fusion_preview()
        self.assertEqual(run.call_args.args[0], "fusion_preview")
        dialog._handle_result("fusion_preview", {
            "inputs": ["A.Input", "B.Foreground"], "outputs": ["B.Output"],
            "nodes": ["A (Transform)", "B (Merge)"],
            "internal_links": ["A.Output → B.Background"],
            "allowed_exits": {"A.Input": ["B.Output"], "B.Foreground": ["B.Output"]},
            "project": "测试时间线", "track": 1, "digest": "test-digest",
            "clips": [{"name": "片段一", "key": "1:20:片段一", "status": "可应用"}],
        })
        self.assertTrue(dialog.fusion_apply.isEnabled())
        dialog.fusion_inputs.cellWidget(0, 1).setCurrentIndex(0)
        dialog.fusion_inputs.cellWidget(1, 1).setCurrentIndex(1)
        with patch.object(QtWidgets.QMessageBox, "question",
                          return_value=QtWidgets.QMessageBox.StandardButton.Yes), patch.object(
                              dialog, "_start") as run:
            dialog._run_fusion_apply()
        self.assertEqual(run.call_args.args[0], "fusion_apply")
        self.assertEqual(run.call_args.args[1]["entries"], ["B.Foreground"])
        self.assertEqual(run.call_args.args[1]["exit"], "B.Output")
        dialog.fusion_text.setPlainText("changed")
        self.assertFalse(dialog.fusion_apply.isEnabled())

    def test_track_and_watermark_run_without_extra_confirmation(self):
        dialog = DaVinciRemoteDialog(self.plugin, self.window)
        self.plugin.dialog = dialog
        with patch.object(QtWidgets.QMessageBox, "question") as question, patch.object(dialog, "_start") as run:
            dialog._run_track_fill()
            dialog._run_watermark()
        question.assert_not_called()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[1]["settings"]["operation"], "track_fill")
        self.assertEqual(run.call_args_list[1].args[1]["settings"]["operation"], "watermark_batch")

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
