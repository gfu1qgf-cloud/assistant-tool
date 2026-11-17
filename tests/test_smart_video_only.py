"""One merged MP4, no subtitle model or SRT, with optional seam-cover footage."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtWidgets
from model.BackgroundTasks import TaskRunContext
from app_plugins.builtin.smart_video_editor import engine, background
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.settings import SmartVideoEditorSettingsPage
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog, SmartVideoExportResultDialog
from app_plugins.builtin.smart_video_editor import seam_cover


class VideoOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/"source.mp4"
        self.source.write_bytes(b"UI fixture")
        self.settings = engine.normalize_smart_video_editor_settings({"export_mode": "video_only"})
        self.task = {"task_id": "video", "task_name": "demo", "task_dir": str(self.root),
                     "output_dir": str(self.root/"out"), "missing_blocks": [], "clips": [{
                         "source": str(self.source), "file_name": self.source.name, "included": True,
                         "kept_ranges": [[.2, 1.2]], "script_word_start": 0, "script_word_end": 0,
                         "original_duration": 1.5, "trim_start": .2, "trim_end": 1.2, "status": "green"}]}
        self.bundle = {"settings": self.settings, "tasks": [self.task], "summary": {}}

    def test_mode_normalizes_and_seam_cover_stays_active(self):
        self.assertEqual(self.settings["export_mode"], "video_only")
        self.assertTrue(seam_cover.is_active(dict(self.settings, seam_cover_enabled=True)))
        self.assertFalse(seam_cover.is_active(dict(self.settings, export_mode="segments", seam_cover_enabled=True)))

    def test_export_uses_merged_renderer_with_subtitles_explicitly_disabled(self):
        item = {"task_id": "video", "video": "one.mp4", "srt": "", "status": "completed"}
        with patch.object(engine, "_resolve_ffmpeg", return_value="ffmpeg"), \
             patch.object(engine, "_export_task", return_value=item) as render, \
             patch.object(engine, "_export_segmented_task", side_effect=AssertionError("must not split")), \
             patch.object(engine, "_generate_final_aligned_srt", side_effect=AssertionError("no subtitles")):
            result = engine.export_smart_video_bundle(self.bundle, alignment_model_loader=Mock(side_effect=AssertionError("no model")))
        self.assertEqual(result["failed"], [])
        self.assertFalse(render.call_args.kwargs["generate_srt"])
        self.assertNotIn("alignment_model_loader", render.call_args.kwargs)
        self.assertEqual(result["completed"][0]["export_mode"], "video_only")

    def test_all_skipped_tasks_do_not_resolve_ffmpeg_or_model(self):
        engine.set_smart_video_missing_review(self.task, "skipped")
        with patch.object(engine, "_resolve_ffmpeg", side_effect=AssertionError("no export")):
            result = engine.export_smart_video_bundle(self.bundle)
        self.assertEqual(result["completed"], [])
        self.assertEqual(len(result["skipped"]), 1)

    def test_submission_does_not_request_subtitle_model(self):
        plugin = SmartVideoEditorPlugin()
        handle = SimpleNamespace(task_id="video", completed=Mock(), failed=Mock(), finished=Mock())
        plugin.context = SimpleNamespace(background_tasks=SimpleNamespace(is_accepting=lambda: True),
                                         submit_background_task=Mock(return_value=handle), log=Mock())
        plugin._submit_worker("export", self.settings, bundle=self.bundle, model_loader=Mock())
        submitted = plugin.context.submit_background_task.call_args.kwargs
        self.assertFalse(submitted["payload"]["load_model"])
        self.assertEqual(submitted["payload"]["settings"]["export_mode"], "video_only")

    def test_clean_export_does_not_check_subtitle_model_readiness(self):
        plugin = SmartVideoEditorPlugin()
        plugin.context = SimpleNamespace(background_tasks=SimpleNamespace(is_accepting=lambda: True), log=Mock(), whisper_model=Mock())
        with patch.object(plugin, "_model_ready", side_effect=AssertionError("no model check")), \
             patch.object(plugin, "_remember_pending_reviews"), patch.object(plugin, "_start_worker") as start:
            plugin._handle_analysis(self.bundle)
        self.assertEqual(start.call_args.args[0], "export")
        self.assertEqual(start.call_args.args[1]["export_mode"], "video_only")

    def test_settings_and_review_preserve_all_four_export_modes(self):
        page = SmartVideoEditorSettingsPage()
        dialog = SmartVideoReviewDialog(self.bundle)
        self.addCleanup(page.widget.deleteLater)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        for mode in ("joined", "segments", "srt_only", "video_only"):
            page.load_config({engine.SMART_VIDEO_EDITOR_CONFIG_KEY: {"export_mode": mode}})
            self.assertEqual(page.settings()["export_mode"], mode)
        self.assertEqual(dialog.export_mode_combo.currentData(), "video_only")
        self.assertEqual(dialog.export_button.text(), "确认并导出视频")
        self.assertTrue(dialog.seam_cover_controls.isEnabled())
        self.assertTrue(dialog.postprocess_controls.isEnabled())
        self.assertEqual(dialog.collect()["settings"]["export_mode"], "video_only")

    def test_result_row_opens_the_single_video(self):
        result = {"completed": [{"task_id": "video", "video": str(self.source), "srt": "",
                                  "export_mode": "video_only"}], "failed": [], "skipped": []}
        dialog = SmartVideoExportResultDialog(result)
        self.addCleanup(dialog.deleteLater)
        row = dialog.tree.topLevelItem(0)
        self.assertEqual(row.text(1), "视频已导出（无字幕）")
        with patch("qt_compat.QtGui.QDesktopServices.openUrl") as opened:
            dialog._open_item(row, 3)
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), self.source)

    def test_real_background_export_creates_one_mp4_without_any_subtitle_work(self):
        ffmpeg = os.environ.get("NODE_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg unavailable")
        asset = self.root/"cover.mp4"
        for path, color, sound in ((self.source, "white", True), (asset, "red", False)):
            args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color={color}:s=160x90:r=30:d=3"]
            if sound:
                args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3"]
            args += ["-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)]
            subprocess.run(args, check=True, capture_output=True)
        self.task["clips"].append(dict(self.task["clips"][0], kept_ranges=[[1.4, 2.4]],
                                          script_word_start=1, script_word_end=1, export_order=2))
        options = dict(self.settings, ffmpeg_path=ffmpeg, seam_cover_enabled=True, seam_cover_source=str(asset))
        context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        payload = {"phase": "export", "settings": options, "bundle": self.bundle, "load_model": True}
        with patch("globalValue.globalValue.get_whisper_model", side_effect=AssertionError("no subtitle model")), \
             patch.object(engine, "_generate_final_aligned_srt", side_effect=AssertionError("no alignment")), \
             patch.object(engine, "build_srt_cues", side_effect=AssertionError("no SRT")):
            result = background.run_smart_video_job(payload, context)
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["completed"]), 1)
        item = result["completed"][0]
        self.assertEqual(item["srt"], "")
        self.assertEqual(result["paths"], [item["video"]])
        self.assertEqual([p.suffix for p in (self.root/"out").iterdir()], [".mp4"])
        import cv2
        capture = cv2.VideoCapture(item["video"])
        try:
            self.assertEqual(capture.get(cv2.CAP_PROP_FRAME_COUNT), 60)
            capture.set(cv2.CAP_PROP_POS_FRAMES, 30)
            ok, frame = capture.read()
            self.assertTrue(ok)
            self.assertGreater(int(frame[45, 80, 2]), 180, "join must contain the red cover shot")
        finally:
            capture.release()

    def test_invalid_clip_blockers_still_apply_to_video_only(self):
        self.task["clips"][0].update(script_word_start=-1, script_word_end=-1, original_duration=3.0)
        with patch.object(engine, "_resolve_ffmpeg", side_effect=AssertionError("must validate first")):
            with self.assertRaises(ValueError):
                engine.export_smart_video_bundle(self.bundle)
