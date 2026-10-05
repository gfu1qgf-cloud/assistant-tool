from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.engine import Cancelled
from app_plugins.builtin.batch_text_video.store import Store, fresh_state, file_identity
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker


class ExportPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def worker(self, root, count=1):
        store = Store(Path(root) / "private")
        state = fresh_state()
        state["jobs"] = [{"id":str(i), "path":"source.mp4", "name":str(i), "title":"Title",
                          "body":"Body", "status":"待生成"} for i in range(count)]
        state = store.save(state, 0)
        return BatchWorker(store, state, [job["id"] for job in state["jobs"]])

    def renderer(self, root):
        def render(job, state, directory, cancel, progress, report):
            progress(99)
            path = Path(root) / (job["name"] + ".mp4")
            path.write_bytes(b"verified fixture")
            return path, None
        return render

    def test_progress_reaches_100_only_after_file_and_ledger_saved(self):
        with tempfile.TemporaryDirectory() as root:
            worker = self.worker(root)
            values = []
            def progress(identifier, value):
                values.append(value)
                if value == 100:
                    job = worker.store.load()["jobs"][0]
                    self.assertEqual(job["status"], "已完成")
                    self.assertTrue(Path(job["output"]).is_file())
            worker.progress.connect(progress)
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",
                            {"static_text":self.renderer(root)}):
                worker.run()
            self.assertEqual(values, [0, 99, 100])

    def test_each_batch_item_resets_progress_before_rendering(self):
        with tempfile.TemporaryDirectory() as root:
            worker = self.worker(root, 2)
            values = []
            worker.progress.connect(lambda _id, value:values.append(value))
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",
                            {"static_text":self.renderer(root)}):
                worker.run()
            self.assertEqual(values, [0, 99, 100, 0, 99, 100])

    def test_cancel_and_render_failure_never_claim_completion(self):
        for error in (Cancelled("cancel"), RuntimeError("encoding failed")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as root:
                worker = self.worker(root)
                values = []
                worker.progress.connect(lambda _id, value:values.append(value))
                with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",
                                {"static_text":Mock(side_effect=error)}):
                    worker.run()
                self.assertEqual(values, [0])

    def test_ledger_failure_after_encoding_does_not_display_100(self):
        with tempfile.TemporaryDirectory() as root:
            worker = self.worker(root)
            values = []
            worker.progress.connect(lambda _id, value:values.append(value))
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",
                            {"static_text":self.renderer(root)}), \
                    patch.object(worker.store, "completed", side_effect=RuntimeError("ledger failed")):
                worker.run()
            self.assertEqual(values, [0, 99])
            self.assertEqual(worker.store.load()["jobs"][0]["status"], "待生成")

    def test_launch_resets_old_100_for_regular_batches(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"), \
                patch.object(BatchWorker, "start"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog.progress_bar.setValue(100)
            dialog._launch_batch([])
            self.assertEqual(dialog.progress_bar.value(), 0)
            dialog._batch_finished()
            dialog.close()

    def open_folder(self, root, entry, jobs=None, copy_id="", queue_id="", configured="", click=False):
        with patch.object(BatchTextVideoDialog, "request_background"):
            dialog = BatchTextVideoDialog(store=Store(Path(root) / "private"))
            dialog.state["jobs"] = jobs or []
            dialog.current_copy_id, dialog.current_id = copy_id, queue_id
            dialog.output.setText(configured)
            with patch.object(dialog, "active_entry", return_value=entry), \
                    patch.object(QtGui.QDesktopServices, "openUrl", return_value=True) as opener:
                result = dialog.open_directory_button.click() if click else dialog.open_output_directory()
                url = opener.call_args.args[0] if opener.called else None
                message = dialog.status.text()
            dialog.close()
            return result, url, message

    def test_directory_button_uses_current_tasks_folder_not_general_output(self):
        with tempfile.TemporaryDirectory() as root:
            task, configured = Path(root) / "task", Path(root) / "ordinary"
            task.mkdir()
            configured.mkdir()
            _, url, _ = self.open_folder(root, {"task_dir":str(task)}, configured=str(configured), click=True)
            self.assertEqual(Path(url.toLocalFile()), task.resolve())

    def test_copy_preview_opens_matching_actual_product_not_another_queue_row(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mp4"
            source.write_bytes(b"source")
            task = Path(root) / "actual"
            task.mkdir()
            output = task / "finished.mp4"
            output.write_bytes(b"output")
            jobs = [{"id":"other", "pair_key":["other",file_identity(source)], "output":"other.mp4"},
                    {"id":"current", "pair_key":["copy",file_identity(source)], "output":str(output)}]
            result, url, _ = self.open_folder(root, {"path":str(source)}, jobs, "copy", "other")
            self.assertTrue(result)
            self.assertEqual(Path(url.toLocalFile()), task.resolve())

    def test_queue_entry_opens_actual_output_parent(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "product.mp4"
            output.write_bytes(b"output")
            result, url, _ = self.open_folder(root, {"output":str(output)}, queue_id="q")
            self.assertTrue(result)
            self.assertEqual(Path(url.toLocalFile()), Path(root).resolve())

    def test_independent_video_uses_configured_or_source_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mp4"
            source.write_bytes(b"source")
            configured = Path(root) / "configured"
            configured.mkdir()
            for value, expected in ((str(configured),configured), ("",Path(root))):
                with self.subTest(configured=value):
                    result, url, _ = self.open_folder(root, {"path":str(source)}, configured=value)
                    self.assertTrue(result)
                    self.assertEqual(Path(url.toLocalFile()), expected.resolve())

    def test_missing_directory_is_reported_not_created_or_redirected(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "not-created"
            result, url, message = self.open_folder(root, {"task_dir":str(folder)}, configured=root)
            self.assertFalse(result)
            self.assertIsNone(url)
            self.assertIn("尚不存在", message)
            self.assertFalse(folder.exists())

    def test_no_selection_does_not_open_random_folder(self):
        with tempfile.TemporaryDirectory() as root:
            result, url, message = self.open_folder(root, {})
            self.assertFalse(result)
            self.assertIsNone(url)
            self.assertIn("请先选择", message)


if __name__ == "__main__":
    unittest.main()
