import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker


class QuickExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def source(self, root):
        path = Path(root) / "background.mp4"
        path.write_bytes(b"source fixture")
        return {"id": "copy", "path": str(path), "title": "Title", "body": "Body",
                "name": "Current", "task_dir": str(Path(root) / "task"), "requires_title": True}

    def test_snapshot_pairs_only_current_and_retains_exact_name_and_task_dir(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            state["jobs"] = [{"id": "other", "path": "other.mp4", "status": "待生成"}]
            before = copy.deepcopy(state)
            entry = self.source(root)
            snapshot, identifier = prepare_current_export(state, entry, "copy")
            self.assertEqual(state, before)
            self.assertEqual(snapshot["jobs"][0], before["jobs"][0])
            self.assertNotEqual(identifier, "copy")
            job = snapshot["jobs"][-1]
            self.assertEqual((job["name"], job["task_dir"], job["title"]),
                             ("Current", entry["task_dir"], "Title"))

    def test_repeated_quick_export_reuses_pair_instead_of_appending_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            entry = self.source(root)
            state, first = prepare_current_export(fresh_state(), entry, "copy")
            state["jobs"][0].update(status="已完成", output="old.mp4")
            entry.update(title="Changed", name="User name")
            state, second = prepare_current_export(state, entry, "copy")
            self.assertEqual(first, second)
            self.assertEqual(len(state["jobs"]), 1)
            self.assertEqual(state["jobs"][0]["title"], "Changed")
            self.assertEqual(state["jobs"][0]["name"], "User name")
            self.assertEqual(state["jobs"][0]["status"], "待生成")

    def test_queue_export_reuses_selected_queue_id_and_never_source_copy_id(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            entry = self.source(root)
            state["jobs"] = [{**entry, "id": "queue", "status": "待生成"}]
            result, identifier = prepare_current_export(state, entry, queue_id="queue")
            self.assertEqual(identifier, "queue")
            self.assertEqual(len(result["jobs"]), 1)

    def test_copy_preview_never_replaces_old_highlighted_queue_row(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            other = {"id":"other", "path":"unrelated.mp4", "status":"待生成"}
            state["jobs"] = [other]
            result, identifier = prepare_current_export(state, self.source(root),
                                                        copy_id="copy", queue_id="other")
            self.assertNotEqual(identifier, "other")
            self.assertEqual(result["jobs"][0], other)
            self.assertEqual(len(result["jobs"]), 2)

    def test_fixed_layers_can_export_video_without_copy(self):
        with tempfile.TemporaryDirectory() as root:
            entry = self.source(root)
            state, identifier = prepare_current_export(fresh_state(), {"path": entry["path"]})
            job = state["jobs"][0]
            self.assertEqual(job["name"], "background_文字版")
            self.assertEqual(job["title"], "")
            self.assertEqual(job["id"], identifier)

    def test_invalid_source_or_missing_table_title_leaves_ledger_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            before = copy.deepcopy(state)
            entry = self.source(root)
            for values in ({**entry, "path": "missing.mp4"}, {**entry, "title": "  "}):
                with self.assertRaises(ValueError):
                    prepare_current_export(state, values)
            self.assertEqual(state, before)

    def dialog(self, root):
        store = Store(Path(root) / "private")
        state = fresh_state()
        state["backgrounds"] = [self.source(root)["path"]]
        state["copy_pool"] = [self.source(root)]
        store.save(state, 0)
        dialog = BatchTextVideoDialog(store=store)
        dialog.current_copy_id = "copy"
        dialog._show_job(state["copy_pool"][0])
        return dialog

    def test_button_menu_and_worker_export_one_visible_composition(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"), \
                patch.object(BatchWorker, "start"):
            dialog = self.dialog(root)
            dialog.title.setPlainText("Latest text, not yet autosaved")
            dialog.name.setText("Current export")
            dialog.quick_export_button.click()
            self.assertEqual(dialog.quick_export_action.shortcut().toString(), "Ctrl+E")
            self.assertEqual(len(dialog.worker.ids), 1)
            job = next(j for j in dialog.worker.state["jobs"] if j["id"] == dialog.worker.ids[0])
            self.assertEqual(job["title"], "Latest text, not yet autosaved")
            self.assertEqual(job["name"], "Current export")
            self.assertEqual(job["task_dir"], str(Path(root) / "task"))
            dialog._batch_finished()
            dialog.close()

    def test_preview_preparation_is_cancelled_and_export_keeps_click_time_snapshot(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"), \
                patch.object(BatchWorker, "start"):
            dialog = self.dialog(root)
            preparing = Mock()
            dialog.playback_worker = preparing
            before = dialog.store.path.read_bytes()
            dialog.quick_export_current()
            preparing.cancel.set.assert_called()
            self.assertTrue(dialog.is_batch_busy())
            self.assertFalse(dialog.splitter.isEnabled())
            self.assertEqual(dialog.store.path.read_bytes(), before)
            # A programmatic late edit must not replace what was visible on click.
            dialog.title.setPlainText("Late edit")
            dialog._playback_finished()
            self.app.processEvents()
            self.assertEqual(dialog.worker.state["jobs"][-1]["title"], "Title")
            dialog._batch_finished()
            dialog.close()

    def test_cancel_or_close_before_preview_finished_never_exports(self):
        for action in ("cancel_work", "close"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as root, \
                    patch.object(BatchTextVideoDialog, "request_background"):
                dialog = self.dialog(root)
                dialog.playback_worker = Mock()
                dialog.quick_export_current()
                getattr(dialog, action)()
                dialog._playback_finished()
                with patch.object(dialog, "_launch_batch") as start:
                    self.app.processEvents()
                    start.assert_not_called()
                self.assertFalse(dialog.is_batch_busy())
                self.assertTrue(dialog.splitter.isEnabled())
                self.assertEqual(dialog.store.load()["jobs"], [])
                dialog.close()

    def test_success_advances_music_and_sequence_once_only_after_real_export(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            state["jobs"] = [{"id": "other", "path": "unrelated.mp4", "status": "待生成"}]
            state["music"] = [{"id": "music", "path": "song.wav", "cursor": 2}]
            state["settings"]["layers"] = [new_text_component()]
            state["settings"]["layers"][0].update(source="sequence", sequence_cursor=0,
                sequence_items=[{"id": "a", "text": "First"}, {"id": "b", "text": "Next"}])
            state, identifier = prepare_current_export(state, self.source(root), "copy")
            store = Store(Path(root) / "private")
            state = store.save(state, 0)
            worker = BatchWorker(store, state, [identifier])
            target = Path(root) / "task" / "Current.mp4"
            def renderer(job, snapshot, *args, **kwargs):
                self.assertEqual(snapshot["music"][0]["cursor"], 2)
                self.assertNotIn("preview", kwargs)
                target.parent.mkdir()
                target.write_bytes(b"rendered output")
                return target, {"id": "music", "cursor": 7, "audio_key": "key"}
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS", {"static_text": renderer}):
                worker.run()
            saved = store.load()
            self.assertEqual(saved["music"][0]["cursor"], 7)
            self.assertEqual(saved["settings"]["layers"][0]["sequence_cursor"], 1)
            self.assertEqual(saved["jobs"][0], state["jobs"][0])
            self.assertEqual(saved["jobs"][-1]["status"], "已完成")

    def test_failed_export_does_not_advance_music_or_sequence(self):
        with tempfile.TemporaryDirectory() as root:
            state, identifier = prepare_current_export(fresh_state(), self.source(root), "copy")
            state["music"] = [{"id": "music", "path": "song.wav", "cursor": 2}]
            store = Store(Path(root) / "private")
            state = store.save(state, 0)
            worker = BatchWorker(store, state, [identifier])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",
                            {"static_text": Mock(side_effect=FileExistsError("existing output"))}):
                worker.run()
            saved = store.load()
            self.assertEqual(saved["music"][0]["cursor"], 2)
            self.assertEqual(saved["jobs"][0]["status"], "需处理")


if __name__ == "__main__":
    unittest.main()
