import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import BackgroundTaskManager
from app_plugins.builtin.facebook_contact_sheet import ui, batch_ui, batch
from app_plugins.builtin.facebook_contact_sheet.task_cache import cached_sheets, cache_directory


class FacebookBackgroundTasksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.manager = BackgroundTaskManager()
        self.release = threading.Event()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate())

    def tearDown(self):
        self.release.set()
        self.manager.cancel_all()
        self.wait_until(lambda: not self.manager.is_busy())
        self.manager.shutdown()
        self.manager.deleteLater()
        self.app.processEvents()

    def render(self, _video, _duration, _cuts, folder, **_kwargs):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        image = folder / "sheet.jpg"
        image.write_bytes(b"test image")
        return [image]

    def test_single_closed_window_keeps_job_running_and_commits_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            url = "https://www.facebook.com/reel/123/"
            folder = cache_directory(temporary, url)
            video = Path(temporary) / "video.mp4"
            video.touch()
            entered = threading.Event()
            def download(*_args, **_kwargs):
                entered.set()
                self.release.wait(2)
                return video, "title"
            dialog = ui.ContactSheetDialog(task_manager=self.manager)
            dialog.show()
            with patch.object(ui, "download_facebook_video", side_effect=download), \
                 patch.object(ui, "detect_cuts", return_value=(2., [])), \
                 patch.object(ui, "render_contact_sheets", side_effect=self.render), \
                 patch.object(QtGui.QDesktopServices, "openUrl") as opener:
                dialog.start_task_reference(url, folder)
                self.wait_until(entered.is_set)
                dialog.close()
                self.assertFalse(dialog.isVisible())
                self.assertTrue(dialog.is_busy())
                self.release.set()
                self.wait_until(lambda: not self.manager.is_busy())
                self.assertEqual(len(cached_sheets(folder, url)), 1)
                opener.assert_not_called()
            dialog.deleteLater()

    def test_batch_closing_and_appending_new_selection_do_not_interrupt_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = [batch.BatchJob(f"task {index}", f"https://www.facebook.com/reel/{index}/", temporary)
                    for index in range(3)]
            entered = []
            def runner(payload, context):
                entered.append(payload["job"].label)
                self.release.wait(2)
                context.check_canceled()
                if payload["job"].label == "task 1": raise ValueError("one bad link")
                return {"paths": []}
            dialog = batch_ui.BatchContactSheetDialog(task_manager=self.manager)
            dialog.show()
            with patch.object(batch_ui, "run_reference_job", side_effect=runner):
                dialog.set_jobs(jobs[:2])
                dialog._start()
                self.wait_until(lambda: len(entered) == 2)
                self.assertTrue(dialog.set_jobs(jobs[2:]))
                self.assertEqual(dialog.table.rowCount(), 3)
                dialog.close()
                self.assertTrue(dialog.is_busy())
                self.release.set()
                self.wait_until(lambda: not self.manager.is_busy())
                self.assertEqual(sorted(entered), ["task 0", "task 1", "task 2"])
                self.assertEqual(sum(record["status"] == "succeeded" for record in self.manager.records()), 2)
                self.assertEqual(sum(record["status"] == "failed" for record in self.manager.records()), 1)
            dialog.deleteLater()


if __name__ == "__main__": unittest.main()
