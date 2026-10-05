import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import BatchTextVideoPlugin
from app_plugins.builtin.batch_text_video.engine import Cancelled, audible_ranges, output_size, safe_output, resolve_tools, filter_file_option
from app_plugins.builtin.batch_text_video.layout import TextDoesNotFit, ensure_fonts, render_overlay
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, add_paths, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker


class BatchTextVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_plugin_registers_lazily_without_model_or_runtime_state(self):
        context = Mock()
        plugin = BatchTextVideoPlugin()
        plugin.register(context)
        command = context.register_command.call_args_list[0].args[0]
        self.assertEqual(command.title, "批量文案视频…")
        self.assertIsNone(plugin.dialog)
        self.assertEqual(plugin.can_close(), (True, ""))
        task_command = context.register_command.call_args_list[1].args[0]
        self.assertIn("task_context_menu", task_command.locations)

    def test_standard_bundled_encoder_is_preferred_and_sha_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root/"runtime"/"ffmpeg"
            binaries.mkdir(parents=True)
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                (binaries/name).touch()
            with patch("app_plugins.builtin.batch_text_video.engine.APP_ROOT", root):
                ffmpeg, ffprobe = resolve_tools()
            self.assertEqual(Path(ffmpeg), binaries/"ffmpeg.exe")
            self.assertEqual(Path(ffprobe), binaries/"ffprobe.exe")
            with self.assertRaisesRegex(ValueError, "标准FFmpeg"):
                resolve_tools("ShanaEncoder.sha")

    def test_filter_script_option_supports_standard_new_and_older_versions(self):
        filter_file_option.cache_clear()
        with patch("app_plugins.builtin.batch_text_video.engine.subprocess.run",
                   return_value=Mock(stdout=b"ffmpeg version 9.0.2", stderr=b"")):
            self.assertEqual(filter_file_option("new-test"), "-/filter_complex")
        with patch("app_plugins.builtin.batch_text_video.engine.subprocess.run",
                   return_value=Mock(stdout=b"ffmpeg version 6.1.6", stderr=b"")):
            self.assertEqual(filter_file_option("old-test"), "-filter_complex_script")
        filter_file_option.cache_clear()

    def test_short_text_uses_maximum_sizes_and_long_text_adapts_within_bounds(self):
        settings = copy.deepcopy(DEFAULTS)
        image, short = render_overlay("Modlitba", "Pokoj a nádej.", 1080, 1920, settings)
        self.assertFalse(image.isNull())
        self.assertEqual(short["title_size"], settings["title_max"])
        self.assertEqual(short["body_size"], settings["body_max"])
        _, long = render_overlay("Modlitba", "Pokoj a nádej. " * 120, 1080, 1920, settings)
        self.assertTrue(settings["body_min"] <= long["body_size"] <= settings["body_max"])
        self.assertLess(long["body_size"], settings["body_max"])
        self.assertLessEqual(long["text_height"], long["available_height"])

    def test_overflow_and_reversed_bounds_fail_instead_of_clipping(self):
        with self.assertRaises(TextDoesNotFit):
            render_overlay("标题", "很长的正文 " * 5000, 1080, 1920, DEFAULTS)
        with self.assertRaisesRegex(ValueError, "最小字号"):
            render_overlay("标题", "正文", 1080, 1920, {**DEFAULTS, "body_min": 80, "body_max": 30})

    def test_all_nine_positions_draw_and_portrait_landscape_source_sizes_work(self):
        hashes = set()
        for vertical in ("top", "center", "bottom"):
            for horizontal in ("left", "center", "right"):
                image, _ = render_overlay("Hello", "Text", 360, 640,
                    {**DEFAULTS, "vertical": vertical, "title_align": horizontal, "body_align": horizontal})
                hashes.add(bytes(image.constBits().asstring(image.sizeInBytes())))
        self.assertEqual(len(hashes), 9)
        self.assertEqual(output_size(DEFAULTS), (1080, 1920))
        self.assertEqual(output_size({**DEFAULTS, "aspect": "landscape"}), (1920, 1080))
        self.assertEqual(output_size({**DEFAULTS, "aspect": "source"}, {"video": {
            "width": 640, "height": 360, "side_data_list": [{"rotation": -90}]}}), (360, 640))

    def test_silence_start_end_internal_and_eof_are_excluded(self):
        ranges = audible_ranges("silence_start: 0\nsilence_end: 1\nsilence_start: 2\nsilence_end: 3\nsilence_start: 4", 5)
        self.assertEqual(len(ranges), 2)
        self.assertLess(sum(b-a for a, b in ranges), 2.2)
        self.assertEqual(audible_ranges("silence_start: 0", 5), [])
        self.assertEqual(audible_ranges("", 5), [(0, 5)])

    def test_progress_only_commits_after_existing_output_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["jobs"] = [{"id": "job", "path": "a.mp4", "status": "待生成"}]
            state["music"] = [{"id": "a", "cursor": 2}, {"id": "b", "cursor": 3}]
            state = store.save(state, 0)
            plan = {"id": "a", "cursor": 7.5, "audio_key": "signature"}
            with self.assertRaises(FileNotFoundError):
                store.completed(state, "job", Path(directory)/"none.mp4", plan)
            self.assertEqual(store.load()["music"][0]["cursor"], 2)
            output = Path(directory)/"done.mp4"
            output.touch()
            done = store.completed(state, "job", output, plan)
            self.assertEqual(done["next_music_id"], "b")
            self.assertEqual(Store(directory).load()["music"][0]["cursor"], 7.5)
            with self.assertRaisesRegex(ValueError, "其他窗口"):
                store.save(state, state["revision"])

    def test_bad_state_is_not_replaced_and_input_paths_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.path.write_text("broken", "utf-8")
            with self.assertRaises(json.JSONDecodeError):
                store.load()
            self.assertEqual(store.path.read_text("utf-8"), "broken")
            source = Path(directory)/"source.mp4"
            with self.assertRaisesRegex(ValueError, "源文件"):
                safe_output(directory, "source", [source])
            self.assertEqual(safe_output(directory, "../../hello", []).parent, Path(directory).resolve())

    def test_addition_is_deduplicated_and_dialog_keeps_text_on_selection_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root/"one.mp4", root/"two.mp4"]
            for path in paths:
                path.touch()
            store = Store(root/"state")
            with patch.object(BatchTextVideoDialog, "request_background"):
                dialog = BatchTextVideoDialog(store=store)
                try:
                    dialog.add_files([str(p) for p in paths] * 2)
                    self.assertEqual(dialog.video_pool.rowCount(), 2)
                    self.assertEqual(dialog.jobs.rowCount(), 0)
                    dialog.add_manual_copy()
                    dialog.title.setPlainText("第一条标题")
                    dialog.body.setPlainText("Príď a pokračuj.")
                    dialog.add_manual_copy()
                    self.assertEqual(dialog.state["copy_pool"][0]["title"], "第一条标题")
                    dialog.persist()
                    self.assertEqual(store.load()["copy_pool"][0]["body"], "Príď a pokračuj.")
                finally:
                    dialog.close()

    def test_failed_job_does_not_abort_later_jobs_or_consume_music(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["jobs"] = [{"id": "a", "path": "a.mp4", "status": "待生成"},
                             {"id": "b", "path": "b.mp4", "status": "待生成"}]
            state["music"] = [{"id": "song", "cursor": 5}]
            state = store.save(state, 0)
            output = Path(directory)/"b.mp4"
            output.touch()
            fake = Mock(side_effect=[TextDoesNotFit("放不下"), (output, None)])
            worker = BatchWorker(store, state, ["a", "b"])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS", {"static_text": fake}):
                worker.run()
            saved = store.load()
            self.assertEqual([job["status"] for job in saved["jobs"]], ["需处理", "已完成"])
            self.assertEqual(saved["music"][0]["cursor"], 5)

    def test_cancelled_job_leaves_pending_queue_and_music_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["jobs"] = [{"id": "a", "path": "a.mp4", "status": "待生成"}]
            state = store.save(state, 0)
            worker = BatchWorker(store, state, ["a"])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS", {"static_text": Mock(side_effect=Cancelled())}):
                worker.run()
            self.assertEqual(store.load(), state)

    def test_ledger_failure_stops_before_rendering_the_next_video(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["jobs"] = [{"id": "a", "path": "a.mp4", "status": "待生成"},
                             {"id": "b", "path": "b.mp4", "status": "待生成"}]
            state = store.save(state, 0)
            renderer = Mock(return_value=(Path(directory)/"a.mp4", None))
            worker = BatchWorker(store, state, ["a", "b"])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS", {"static_text": renderer}), \
                 patch.object(store, "completed", side_effect=OSError("disk failure")):
                worker.run()
            self.assertEqual(renderer.call_count, 1)
            self.assertEqual(store.load(), state)

    def test_bad_record_reload_does_not_escape_the_qt_finished_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with patch.object(BatchTextVideoDialog, "request_background"):
                dialog = BatchTextVideoDialog(store=store)
                dialog.worker = Mock()
                with patch.object(store, "load", side_effect=ValueError("bad data")):
                    dialog._batch_finished()
                self.assertIsNone(dialog.worker)
                self.assertIn("读取生成记录失败", dialog.status.text())
                dialog.close()


if __name__ == "__main__":
    unittest.main()
