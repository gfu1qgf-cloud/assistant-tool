"""SRT-only exports must use reviewed timing without media/model processing."""
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import TaskRunContext
from app_plugins.builtin.smart_video_editor import engine, background
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.settings import SmartVideoEditorSettingsPage
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog, SmartVideoExportResultDialog


class SrtOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = engine.normalize_smart_video_editor_settings({"export_mode": "srt_only",
            "srt_max_words_per_block": 3, "srt_block_gap_ms": -1})
        lines, words = engine.build_script_word_records("alpha bravo charlie\ndelta echo")
        self.task = {"task_id": "SRT", "task_dir": str(self.root), "output_dir": str(self.root / "output"),
            "script": "alpha bravo charlie\ndelta echo", "script_lines": lines, "script_words": words, "clips": []}
        for name, order, span, ranges, timings, included in (
            ("a", 2, (0, 2), [[1, 2], [3, 4.5]], [(1.1, 1.4), (2.2, 2.5), (3.1, 3.4)], True),
            ("b", 1, (3, 4), [[0, 2]], [(.2, .6), (1., 1.4)], True),
            ("excluded", 3, (-1, -1), [[0, 4]], [], False),
        ):
            source = self.root / (name + ".mp4")
            source.write_bytes(b"source must remain unchanged")
            self.task["clips"].append({"source": str(source), "file_name": source.name,
                "included": included, "export_order": order, "status": "green", "original_duration": 5.,
                "trim_start": ranges[0][0], "trim_end": ranges[-1][1], "kept_ranges": ranges,
                "script_word_start": span[0], "script_word_end": span[1], "content_order_reliable": True,
                "word_timeline": [{"script_word_index": span[0]+i, "start": start, "end": end, "anchor": True}
                                  for i, (start, end) in enumerate(timings)]})
        self.bundle = {"tasks": [self.task], "settings": self.settings, "summary": {}}

    def export(self, settings=None):
        with patch.object(engine, "_resolve_ffmpeg", side_effect=AssertionError("no FFmpeg in SRT-only")), \
             patch.object(engine, "_export_task", side_effect=AssertionError("no video export")), \
             patch.object(engine, "_generate_final_aligned_srt", side_effect=AssertionError("no recognition/alignment")):
            return engine.export_smart_video_bundle(self.bundle, settings or self.settings)

    def test_writes_only_srt_in_reviewed_order_with_deleted_speech_removed(self):
        result = self.export()
        self.assertEqual(result["failed"], [])
        item = result["completed"][0]
        self.assertEqual(item["video"], "")
        text = Path(item["srt"]).read_text(encoding="utf-8-sig")
        self.assertIn("00:00:00,200 --> 00:00:01,400\ndelta echo", text)
        self.assertIn("00:00:02,100 --> 00:00:02,400\nalpha", text)
        self.assertIn("00:00:03,100 --> 00:00:03,400\ncharlie", text)
        self.assertNotIn("bravo", text)
        self.assertEqual([p.suffix for p in (self.root / "output").iterdir()], [".srt"])
        for source in self.root.glob("*.mp4"):
            self.assertEqual(source.read_bytes(), b"source must remain unchanged")

    def test_existing_output_version_skip_and_overwrite_do_not_touch_video(self):
        output = self.root / "output"
        output.mkdir()
        srt = output / "SRT_智能剪辑.srt"
        srt.write_text("previous subtitle", encoding="utf-8")
        video = srt.with_suffix(".mp4")
        video.write_bytes(b"existing final video")
        self.assertEqual(Path(self.export()["completed"][0]["srt"]).name, "SRT_智能剪辑_2.srt")
        skipped = self.export(dict(self.settings, existing_output="skip"))["completed"][0]
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(srt.read_text(encoding="utf-8"), "previous subtitle")
        self.export(dict(self.settings, existing_output="overwrite"))
        self.assertIn("delta echo", srt.read_text(encoding="utf-8-sig"))
        self.assertEqual(video.read_bytes(), b"existing final video")

    def test_cancel_after_staging_preserves_previous_srt_and_removes_temporary_files(self):
        settings = dict(self.settings, existing_output="overwrite")
        output = self.root / "output"
        output.mkdir()
        previous = output / "SRT_智能剪辑.srt"
        previous.write_text("preserve", encoding="utf-8")
        with self.assertRaises(InterruptedError):
            engine._export_srt_task(self.task, settings, lambda *_: None, Mock(side_effect=[False, True]))
        self.assertEqual(previous.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(list(output.iterdir()), [previous])

    def test_empty_subtitles_report_failure_without_publishing_empty_file(self):
        with patch.object(engine, "build_srt_cues", return_value=[]):
            result = self.export()
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("没有可导出的字幕", result["failed"][0]["error"])
        self.assertFalse((self.root / "output").exists())

    def test_manual_text_override_and_line_break_setting_are_respected(self):
        self.task["clips"][0].update(manual_text_override=True, expected_text="Reviewed\nreplacement")
        result = self.export(dict(self.settings, srt_include_line_breaks=False))
        text = Path(result["completed"][0]["srt"]).read_text(encoding="utf-8-sig")
        self.assertIn("Reviewed replacement", text)
        self.assertNotIn("alpha", text)

    def test_all_skipped_tasks_do_not_require_a_model_or_ffmpeg(self):
        engine.set_smart_video_missing_review(self.task, "skipped")
        result = self.export()
        self.assertEqual(result["completed"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertFalse((self.root / "output").exists())

    def test_background_exposes_srt_path_and_ignores_video_postprocessing(self):
        context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        payload = {"phase": "export", "settings": dict(self.settings, postprocess_enabled=True),
                   "bundle": self.bundle, "load_model": True, "postprocess": {"unused": True}}
        with patch("globalValue.globalValue.get_whisper_model", side_effect=AssertionError("no model")), \
             patch("app_plugins.builtin.smart_video_editor.postprocess_workflow.process_results", side_effect=AssertionError("no video workflow")), \
             patch.object(engine, "_resolve_ffmpeg", side_effect=AssertionError("no FFmpeg")):
            result = background.run_smart_video_job(payload, context)
        self.assertEqual(result["paths"], [result["completed"][0]["srt"]])

    def test_normalization_and_settings_page_keep_all_three_modes(self):
        page = SmartVideoEditorSettingsPage()
        self.addCleanup(page.widget.deleteLater)
        for mode in ("joined", "segments", "srt_only"):
            page.load_config({engine.SMART_VIDEO_EDITOR_CONFIG_KEY: {"export_mode": mode}})
            self.assertEqual(page.settings()["export_mode"], mode)
        self.assertEqual(engine.normalize_smart_video_editor_settings({"export_mode": {}})["export_mode"], "joined")

    def test_review_mode_disables_video_workflow_and_accepts_without_workflow_selection(self):
        with patch("app_plugins.builtin.smart_video_editor.timeline_review.FfplayReviewSurface.prepare"), \
             patch("app_plugins.builtin.smart_video_editor.timeline_review.WaveformLoader.load_task"):
            bundle = copy.deepcopy(self.bundle)
            bundle["settings"] = dict(self.settings, postprocess_enabled=True, postprocess_workflow="")
            dialog = SmartVideoReviewDialog(bundle)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.export_button.text(), "确认并导出 SRT")
        self.assertFalse(dialog.postprocess_controls.isEnabled())
        with patch("app_plugins.builtin.smart_video_editor.ui.save_analysis_reports"), \
             patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog.accept()
        warning.assert_not_called()
        self.assertEqual(dialog.reviewed_bundle["settings"]["export_mode"], "srt_only")

    def test_result_row_opens_srt_when_no_video_was_generated(self):
        result = self.export()
        dialog = SmartVideoExportResultDialog(result)
        self.addCleanup(dialog.deleteLater)
        row = dialog.tree.topLevelItem(0)
        self.assertEqual(row.text(1), "SRT 已导出")
        with patch("qt_compat.QtGui.QDesktopServices.openUrl") as opened:
            dialog._open_item(row, 3)
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), Path(result["completed"][0]["srt"]))

    def test_submission_does_not_prepare_workflow_or_request_model(self):
        plugin = SmartVideoEditorPlugin()
        handle = SimpleNamespace(task_id="srt", completed=Mock(), failed=Mock(), finished=Mock())
        plugin.context = SimpleNamespace(background_tasks=SimpleNamespace(is_accepting=lambda: True),
            submit_background_task=Mock(return_value=handle), log=Mock(), available_nodes=Mock(side_effect=AssertionError("no nodes")))
        plugin._submit_worker("export", dict(self.settings, postprocess_enabled=True), bundle=self.bundle, model_loader=Mock())
        submitted = plugin.context.submit_background_task.call_args.kwargs
        self.assertFalse(submitted["payload"]["load_model"])
        self.assertIsNone(submitted["payload"]["postprocess"])
        self.assertIn("字幕导出", submitted["title"])

    def test_clean_srt_export_does_not_check_whisper_model_readiness(self):
        plugin = SmartVideoEditorPlugin()
        plugin.context = SimpleNamespace(background_tasks=SimpleNamespace(is_accepting=lambda: True), log=Mock(), whisper_model=Mock())
        with patch.object(plugin, "_model_ready", side_effect=AssertionError("no model check")), \
             patch.object(plugin, "_remember_pending_reviews"), patch.object(plugin, "_start_worker") as start:
            plugin._handle_analysis(self.bundle)
        self.assertEqual(start.call_args.args[0], "export")
