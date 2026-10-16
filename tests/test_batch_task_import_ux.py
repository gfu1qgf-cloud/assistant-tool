import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.captions import caption_subtitle_path, new_caption
from app_plugins.builtin.batch_text_video.pools import apply_pairs, merge_copies, pairing_plan
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.task_texts import entry_from_record, record_from_task
from app_plugins.builtin.batch_text_video.task_text_ui import TaskTextImportDialog
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.audio_sources import resolve_voice


class TaskImportUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def record(self, directory, **values):
        return {"task_id": "1000", "task_type": "reels", "target_dir": str(directory),
                "task_audio_text": "Title\nBody", "task_name": "Original task", **values}

    def test_task_audio_and_subtitles_follow_copy_through_pairing_and_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task"
            task.mkdir()
            voice, subtitle = task / "TASK_AUDIO.WAV", task / "TASK_AUDIO.SRT"
            for path in (voice, subtitle, root / "background.mp4"):
                path.touch()
            entry = entry_from_record(self.record(task))
            state = fresh_state()
            self.assertEqual(merge_copies(state, [entry], sheet=True), (1, 0))
            apply_pairs(state, pairing_plan(state["copy_pool"], [str(root / "background.mp4")]))
            loaded = Store(root / "private").save(state, 0)
            job = loaded["jobs"][0]
            self.assertEqual(job["task_voice_path"], str(voice.resolve()))
            self.assertEqual(job["task_subtitle_path"], str(subtitle.resolve()))
            self.assertEqual(caption_subtitle_path(job, job["voice_path"], new_caption()), subtitle)
            self.assertTrue(voice.is_file() and subtitle.is_file())

    def test_reimport_refreshes_new_attachments_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = fresh_state()
            record = self.record(root)
            merge_copies(state, [entry_from_record(record)], sheet=True)
            identifier = state["copy_pool"][0]["id"]
            (root / "task_audio.mp3").touch()
            (root / "task_audio.srt").touch()
            self.assertEqual(merge_copies(state, [entry_from_record(record)], sheet=True), (0, 1))
            self.assertEqual(state["copy_pool"][0]["id"], identifier)
            self.assertTrue(state["copy_pool"][0]["task_subtitle_path"])
            self.assertEqual(merge_copies(state, [entry_from_record(record)], sheet=True), (0, 0))

    def test_task_named_audio_uses_the_actual_safe_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = Mock(task_id="1000", task_type="reels", task_name="safe_name",
                        _full_task_name="Full\nName", task_audio_text="Title\nBody", source_row=2)
            (root / "safe_name.mp3").touch()
            (root / "safe_name.srt").touch()
            entry = entry_from_record(record_from_task(task, root))
            self.assertEqual(Path(entry["voice_path"]).name, "safe_name.mp3")
            self.assertEqual(Path(entry["task_subtitle_path"]).name, "safe_name.srt")

    def test_manual_voice_override_is_kept_and_does_not_reuse_other_voice_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("task_audio.mp3", "task_audio.srt", "manual.wav"):
                (root / name).touch()
            state = fresh_state()
            record = self.record(root)
            merge_copies(state, [entry_from_record(record)], sheet=True)
            item = state["copy_pool"][0]
            item.update(voice_override=True, voice_path=str(root / "manual.wav"))
            merge_copies(state, [entry_from_record(record)], sheet=True)
            self.assertEqual(item["voice_path"], str(root / "manual.wav"))
            self.assertEqual(Path(item["task_voice_path"]).name, "task_audio.mp3")
            self.assertIsNone(caption_subtitle_path(item, item["voice_path"], new_caption()))
            layer = new_caption()
            layer["caption_timing"] = "task"
            with self.assertRaises(FileNotFoundError):
                caption_subtitle_path(item, item["voice_path"], layer)

    def test_unrelated_music_is_not_mistaken_for_narration_or_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "music.mp3").touch()
            (root / "other.srt").touch()
            entry = entry_from_record(self.record(root))
            self.assertEqual(entry["voice_path"], "")
            self.assertEqual(entry["task_subtitle_path"], "")
            self.assertEqual(entry["match_error"], "")

    def test_mixed_task_import_keeps_text_only_tasks_usable_without_narration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with_voice, text_only = root / "voice", root / "text"
            with_voice.mkdir()
            text_only.mkdir()
            (with_voice / "task_audio.mp3").touch()
            video = root / "background.mp4"
            video.touch()
            state = fresh_state()
            entries = [entry_from_record(self.record(with_voice)),
                       entry_from_record(self.record(text_only, task_id="1001"))]
            merge_copies(state, entries, sheet=True)
            apply_pairs(state, pairing_plan(state["copy_pool"], [str(video)]))
            state["settings"]["use_voice"] = True
            self.assertTrue(resolve_voice(state["jobs"][0], state, discover=True))
            self.assertEqual(resolve_voice(state["jobs"][1], state, discover=True), "")

    def test_import_enables_attached_task_narration_and_reports_subtitles(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(BatchTextVideoDialog, "request_background"), \
                patch.object(BatchTextVideoDialog, "update_preview"):
            root = Path(directory)
            (root / "task_audio.mp3").touch()
            (root / "task_audio.srt").touch()
            entry = entry_from_record(self.record(root))
            def accept(dialog):
                dialog.entries = [entry]
                return QtWidgets.QDialog.DialogCode.Accepted
            dialog = BatchTextVideoDialog(store=Store(root / "private"))
            try:
                with patch.object(TaskTextImportDialog, "exec", accept):
                    self.assertTrue(dialog.import_task_copy(records=[self.record(root)]))
                self.assertTrue(dialog.controls["use_voice"].isChecked())
                self.assertIn("人声1条、字幕1条", dialog.status.text())
                self.assertEqual(len(dialog.store.load()["copy_pool"]), 1)
            finally:
                dialog.close()

    def test_ambiguous_task_audio_is_not_silently_skipped_in_voice_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("task_audio.mp3", "task_audio.wav", "background.mp4"):
                (root / name).touch()
            state = fresh_state()
            merge_copies(state, [entry_from_record(self.record(root))], sheet=True)
            apply_pairs(state, pairing_plan(state["copy_pool"], [str(root / "background.mp4")]))
            state["settings"]["use_voice"] = True
            with self.assertRaisesRegex(ValueError, "多个人声"):
                resolve_voice(state["jobs"][0], state, discover=True)

    def test_copy_context_import_and_video_reveal_have_direct_working_actions(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(BatchTextVideoDialog, "request_background"), \
                patch.object(BatchTextVideoDialog, "update_preview"):
            root = Path(directory)
            video = root / "video with spaces.mp4"
            video.touch()
            dialog = BatchTextVideoDialog(store=Store(root / "private"))
            menus = []
            try:
                imported = []
                with patch.object(dialog, "import_sheet_text", lambda *_: imported.append(True)), \
                        patch.object(QtWidgets.QMenu, "exec", lambda menu, pos: menus.append(menu)):
                    dialog.resource_menu("copy_pool", QtCore.QPoint())
                    action = next(a for a in menus[-1].actions() if "从当前任务导入" in a.text())
                    action.trigger()
                    self.assertEqual(imported, [True])
                dialog.state["backgrounds"] = [str(video)]
                dialog.refresh_pools()
                dialog.video_pool.selectRow(0)
                with patch.object(QtWidgets.QMenu, "exec", lambda menu, pos: menus.append(menu)), \
                        patch.object(QtCore.QProcess, "startDetached", return_value=(True, 1)) as opened:
                    dialog.resource_menu("backgrounds", QtCore.QPoint())
                    next(a for a in menus[-1].actions() if a.text() == "打开文件位置").trigger()
                    opened.assert_called_once_with("explorer.exe", ["/select,", str(video.resolve())])
                self.assertTrue(video.is_file())
                video.unlink()
                with patch.object(QtCore.QProcess, "startDetached") as opened:
                    self.assertFalse(dialog.open_resource_location("backgrounds"))
                    opened.assert_not_called()
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
