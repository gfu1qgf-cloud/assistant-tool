import copy
from fractions import Fraction
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.matching import scan_inputs, merge_entries, assign_backgrounds
from app_plugins.builtin.batch_text_video.adaptation import adaptation_plan, source_fps
from app_plugins.builtin.batch_text_video.mixing import audio_graph
from app_plugins.builtin.batch_text_video.plugin import BatchTextVideoPlugin
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class BatchTextVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_fifty_tasks_keep_voice_folder_identity_and_rotate_backgrounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(50):
                folder = root/f"task{index}"
                folder.mkdir()
                (folder/"task_audio.mp3").touch()
                (folder/"music.mp3").touch()
            backgrounds = [root/"b1.mp4", root/"b2.mp4"]
            for path in backgrounds:
                path.touch()
            result = scan_inputs([root])
            self.assertEqual(len(result["entries"]), 50)
            self.assertEqual(result["entries"][2]["label"], "task2")
            state = fresh_state()
            self.assertEqual(merge_entries(state, result["entries"]), 50)
            self.assertEqual(merge_entries(state, result["entries"]), 0)
            self.assertEqual(assign_backgrounds(state, backgrounds), 50)
            for index, job in enumerate(state["jobs"]):
                self.assertEqual(Path(job["voice_path"]).parent.name, f"task{index}")
                self.assertEqual(Path(job["path"]), backgrounds[index % 2])

    def test_same_video_can_back_many_voices_and_assign_only_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = [root/"one.wav", root/"two.wav"]
            video = root/"background.mp4"
            for path in [*audio, video]:
                path.touch()
            state = fresh_state()
            merge_entries(state, scan_inputs(audio)["entries"])
            self.assertEqual(len(state["jobs"]), 2)
            self.assertEqual(state["jobs"][0]["path"], state["jobs"][1]["path"])
            alternate = root/"another.mp4"
            alternate.touch()
            self.assertEqual(assign_backgrounds(state, [alternate], [state["jobs"][0]["id"]]), 1)
            self.assertEqual(Path(state["jobs"][1]["path"]), video)

    def test_import_attaches_voice_to_previously_added_video_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video, audio = root/"one.mp4", root/"task_audio.mp3"
            video.touch()
            audio.touch()
            state = fresh_state()
            state["jobs"] = [{"id": "old", "path": str(video), "title": "用户标题", "body": "用户正文", "name": "custom", "status": "待生成"}]
            self.assertEqual(merge_entries(state, scan_inputs([root])["entries"]), 1)
            self.assertEqual(len(state["jobs"]), 1)
            self.assertEqual(state["jobs"][0]["id"], "old")
            self.assertEqual(state["jobs"][0]["title"], "用户标题")
            self.assertEqual(Path(state["jobs"][0]["voice_path"]), audio)
            self.assertEqual(state["jobs"][0]["name"], "custom")

    def test_ambiguous_voice_is_not_guessed_and_export_subfolder_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"task_audio.mp3").touch()
            (root/"task_audio.wav").touch()
            excluded = root/"静态文字版"
            excluded.mkdir()
            (excluded/"task_audio.mp3").touch()
            result = scan_inputs([root])
            self.assertEqual(len(result["entries"]), 1)
            self.assertEqual(result["entries"][0]["voice_path"], "")
            self.assertIn("多个", result["entries"][0]["match_error"])

    def test_task_name_audio_fallback_and_metadata_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"hello.mp3").touch()
            result = scan_inputs(targets=[{"target_dir": str(root), "task_name": "hello", "title": "标题", "body": "Príď.", "label": "001"}])
            entry = result["entries"][0]
            self.assertEqual(Path(entry["voice_path"]).name, "hello.mp3")
            self.assertEqual(entry["body"], "Príď.")
            self.assertEqual(entry["label"], "001")

    def test_missing_voice_selected_task_is_listed_and_cancel_does_not_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            result = scan_inputs(targets=[{"target_dir": directory}])
            self.assertIn("没有找到", result["entries"][0]["match_error"])
            event = threading.Event()
            event.set()
            cancelled = scan_inputs([directory], cancel=event)
            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(cancelled["entries"], [])

    def test_duration_strategies_and_rational_fps(self):
        self.assertEqual(source_fps({"video": {"avg_frame_rate": "30000/1001"}}), Fraction(30000,1001))
        loop = adaptation_plan(8, 40, DEFAULTS)
        self.assertEqual(loop["speed"], 1)
        self.assertEqual(loop["loops"], 4)
        self.assertTrue(loop["fade"] > 0)
        slow = adaptation_plan(8, 40, {**DEFAULTS, "duration_strategy": "slow"})
        self.assertAlmostEqual(slow["speed"], 0.2)
        self.assertFalse(slow["repeating"])
        hybrid = adaptation_plan(8, 40, {**DEFAULTS, "duration_strategy": "hybrid"})
        self.assertEqual(hybrid["speed"], 0.5)
        self.assertTrue(hybrid["repeating"])
        shorter = adaptation_plan(8, 3, DEFAULTS)
        self.assertFalse(shorter["repeating"])
        self.assertEqual(shorter["speed"], 1)

    def test_music_eq_and_duck_affect_only_music_not_narration(self):
        graph, audio = audio_graph(DEFAULTS, 4, 2, 3, {"start": 0}, True)
        text = ";".join(graph)
        self.assertTrue(audio)
        self.assertIn("equalizer=f=1500", text)
        self.assertIn("[music_raw][voice_side]sidechaincompress", text)
        self.assertNotIn("silenceremove", text)
        self.assertNotIn("[0:a]", text)
        self.assertNotIn("atempo", text)
        self.assertNotIn("equalizer", graph[0])
        graph, _ = audio_graph({**DEFAULTS, "voice_eq": False, "voice_duck": False}, 4, 2, 3, {"start": 0})
        self.assertNotIn("equalizer", ";".join(graph))
        self.assertNotIn("sidechaincompress", ";".join(graph))

    def test_old_settings_migrate_without_losing_music_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["settings"] = {"music_volume": 42}
            state["music"] = [{"id": "a", "path": "a.mp3", "cursor": 15}]
            state.pop("backgrounds")
            store.path.write_text(json.dumps(state), "utf-8")
            loaded = store.load()
            self.assertEqual(loaded["music"][0]["cursor"], 15)
            self.assertEqual(loaded["settings"]["music_volume"], 42)
            self.assertFalse(loaded["settings"]["use_voice"])
            self.assertTrue(loaded["settings"]["voice_eq"])

    def test_dialog_keeps_voice_pairings_and_bulk_options(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["jobs"] = [{"id": "a", "path": "", "voice_path": "voice.wav", "task_dir": directory,
                               "title": "标题", "body": "Text", "name": "a", "status": "需配置", "output": "", "error": ""}]
            store.save(state, 0)
            with patch.object(BatchTextVideoDialog, "request_background"):
                dialog = BatchTextVideoDialog(store=store)
                self.assertEqual(dialog.jobs.columnCount(), 4)
                self.assertIn("voice.wav", dialog.voice_label.text())
                dialog.controls["use_voice"].setChecked(True)
                dialog.persist()
                self.assertTrue(store.load()["settings"]["use_voice"])
                self.assertEqual(store.load()["jobs"][0]["voice_path"], "voice.wav")
                dialog.close()

    def test_task_context_import_passes_all_selected_rows_without_widgets_in_worker(self):
        context = Mock()
        context.current_task_table_path.return_value = None
        context.task_targets.return_value = [{"target_dir": "a", "task": Mock(task_name="Hello", task_audio_text="Text", _full_task_name="Title")}]*50
        plugin = BatchTextVideoPlugin()
        plugin.register(context)
        dialog = Mock()
        with patch.object(plugin, "open_dialog", return_value=dialog):
            plugin.import_tasks(list(range(50)))
        records = dialog.import_task_copy.call_args.kwargs["records"]
        self.assertEqual(len(records), 50)
        self.assertEqual(records[0]["task_audio_text"], "Text")
        self.assertEqual(records[0]["task_name"], "Title")
        self.assertTrue(all(isinstance(record, dict) for record in records))
        context.task_targets.assert_called_once_with(tuple(range(50)))

    def test_fifty_task_import_worker_persists_full_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(50):
                folder = root/f"task{index}"
                folder.mkdir()
                (folder/"task_audio.wav").touch()
            store = Store(root/"private")
            with patch.object(BatchTextVideoDialog, "request_background"):
                dialog = BatchTextVideoDialog(store=store)
                loop = QtCore.QEventLoop()
                timer = QtCore.QTimer()
                timer.setInterval(10)
                timer.timeout.connect(lambda: loop.quit() if dialog.import_worker is None else None)
                self.assertTrue(dialog.start_import([root]))
                timer.start()
                QtCore.QTimer.singleShot(5000, loop.quit)
                loop.exec()
                timer.stop()
                try:
                    self.assertIsNone(dialog.import_worker)
                    self.assertEqual(dialog.copy_pool.rowCount(), 50)
                    self.assertEqual(len(store.load()["copy_pool"]), 50)
                    self.assertEqual(len(store.load()["jobs"]), 0)
                    self.assertTrue(store.load()["settings"]["use_voice"])
                    self.assertIn("task_audio.wav", dialog.voice_label.text())
                finally:
                    if dialog.import_worker:
                        dialog.cancel_work()
                        dialog.import_worker.wait(5000)
                    dialog.close()

    def test_preview_ignores_other_voice_and_refreshes_current_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            state = fresh_state()
            state["settings"]["use_voice"] = True
            state["jobs"] = [{"id": "a", "path": "shared.mp4", "voice_path": "first.wav",
                               "title": "", "body": "", "name": "a", "status": "待生成", "error": ""}]
            store.save(state, 0)
            with patch.object(BatchTextVideoDialog, "request_background"):
                dialog = BatchTextVideoDialog(store=store)
                dialog._voice_duration = 10
                dialog._background_ready({"path": "shared.mp4", "voice_path": "other.wav", "configured": "",
                                          "image": b"", "media": None, "voice_duration": 99})
                self.assertEqual(dialog._voice_duration, 10)
                dialog.state["jobs"][0]["voice_path"] = "second.wav"
                dialog.refresh_tables()
                self.assertIn("second.wav", dialog.voice_label.text())
                dialog.close()


if __name__ == "__main__":
    unittest.main()
