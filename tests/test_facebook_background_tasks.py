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
from app_plugins.builtin.facebook_contact_sheet.plugin import FacebookContactSheetPlugin
from unittest.mock import Mock


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

    def test_escape_hides_single_and_keeps_result_pending_in_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary)/'video.mp4'; video.touch()
            entered = threading.Event()
            def download(*_args, **_kwargs):
                entered.set(); self.release.wait(3); return video, 'title'
            dialog = ui.ContactSheetDialog(task_manager=self.manager); dialog.show()
            with patch.object(ui, 'download_facebook_video', side_effect=download), \
                 patch.object(ui, 'detect_cuts', return_value=(2., [])), \
                 patch.object(ui, 'render_contact_sheets', side_effect=self.render), \
                 patch.object(QtGui.QDesktopServices, 'openUrl', return_value=True) as opener:
                dialog.start_task_reference('https://www.facebook.com/reel/123/', cache_directory(temporary, 'https://www.facebook.com/reel/123/'))
                self.wait_until(entered.is_set); handle = dialog._worker
                from qt_compat import QtTest
                QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
                self.assertFalse(dialog.isVisible()); self.assertTrue(handle.isRunning())
                self.release.set(); self.wait_until(lambda: not self.manager.is_busy())
                self.assertEqual(self.manager.get(handle.task_id)['status'], 'succeeded')
                self.assertTrue(handle.has_pending_result()); opener.assert_not_called()
                self.assertTrue(handle.show_result()); self.assertFalse(handle.has_pending_result())
                opener.assert_called_once()
            dialog.deleteLater()

    def test_deleting_single_view_does_not_lose_host_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary)/'video.mp4'; video.touch()
            entered = threading.Event()
            def download(*_args, **_kwargs):
                entered.set(); self.release.wait(3); return video, 'title'
            dialog = ui.ContactSheetDialog(task_manager=self.manager); dialog.show()
            with patch.object(ui, 'download_facebook_video', side_effect=download), \
                 patch.object(ui, 'detect_cuts', return_value=(2., [])), \
                 patch.object(ui, 'render_contact_sheets', side_effect=self.render), \
                 patch.object(QtGui.QDesktopServices, 'openUrl', return_value=True) as opener:
                dialog.start_task_reference('https://www.facebook.com/reel/123/', cache_directory(temporary, 'https://www.facebook.com/reel/123/'))
                self.wait_until(entered.is_set); handle = dialog._worker
                dialog.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                self.release.set(); self.wait_until(lambda: not self.manager.is_busy())
                self.assertEqual(self.manager.get(handle.task_id)['status'], 'succeeded')
                self.assertTrue(handle.has_pending_result()); self.assertTrue(handle.show_result())
                opener.assert_called_once()

    def test_batch_background_button_retains_results_and_explicit_cancel_still_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = batch.BatchJob('task', 'https://www.facebook.com/reel/123/', temporary)
            entered = threading.Event()
            def runner(payload, context):
                entered.set(); self.release.wait(3); context.check_canceled()
                return {'paths': [str(path) for path in self.render(None, None, None, payload['job'].folder)]}
            dialog = batch_ui.BatchContactSheetDialog(task_manager=self.manager); dialog.show()
            with patch.object(batch_ui, 'run_reference_job', side_effect=runner):
                dialog.set_jobs([job]); dialog._start(); self.wait_until(entered.is_set)
                handle = dialog._handles[0]; dialog.background_button.click()
                self.assertFalse(dialog.isVisible()); self.assertEqual(self.manager.get(handle.task_id)['status'], 'running')
                self.release.set(); self.wait_until(lambda: not self.manager.is_busy())
                self.assertTrue(handle.has_pending_result())
            dialog.deleteLater()

    def test_plugin_stop_never_silently_cancels_running_work(self):
        plugin = FacebookContactSheetPlugin(); plugin.dialog = Mock()
        plugin.dialog.is_busy.return_value = True
        self.assertFalse(plugin.stop()); plugin.dialog.cancel_work.assert_not_called(); plugin.dialog.dispose.assert_not_called()
        self.assertFalse(plugin.can_close()[0])
        plugin.dialog.is_busy.return_value = False
        self.assertTrue(plugin.stop()); plugin.dialog.cancel_work.assert_not_called(); plugin.dialog.dispose.assert_called_once()

    def test_explicit_cancel_task_cancels_even_after_view_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary)/'video.mp4'; video.touch(); entered = threading.Event()
            def download(*_args, **_kwargs):
                entered.set(); self.release.wait(3); return video, 'title'
            dialog = ui.ContactSheetDialog(task_manager=self.manager); dialog.show()
            with patch.object(ui, 'download_facebook_video', side_effect=download), \
                 patch.object(ui, 'detect_cuts', return_value=(2., [])), \
                 patch.object(ui, 'render_contact_sheets', side_effect=self.render):
                dialog.start_task_reference('https://www.facebook.com/reel/123/', cache_directory(temporary, 'https://www.facebook.com/reel/123/'))
                self.wait_until(entered.is_set); handle = dialog._worker
                dialog.close(); dialog.cancel_work(); self.release.set()
                self.wait_until(lambda: not self.manager.is_busy())
                self.assertEqual(self.manager.get(handle.task_id)['status'], 'canceled')
                self.assertFalse(handle.has_pending_result())
            dialog.deleteLater()

    def test_closing_queued_single_does_not_cancel_or_resubmit(self):
        self.manager.kind_limits['media'] = 1
        entered = threading.Event()
        def blocker(_payload, _context): entered.set(); self.release.wait(3)
        self.manager.submit(owner='fixture', title='hold media slot', runner=blocker, payload={}, kind='media')
        self.wait_until(entered.is_set)
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary)/'video.mp4'; video.touch()
            dialog = ui.ContactSheetDialog(task_manager=self.manager); dialog.show()
            with patch.object(ui, 'download_facebook_video', return_value=(video, 'title')) as download, \
                 patch.object(ui, 'detect_cuts', return_value=(2., [])), \
                 patch.object(ui, 'render_contact_sheets', side_effect=self.render):
                dialog.start_task_reference('https://www.facebook.com/reel/123/', cache_directory(temporary, 'https://www.facebook.com/reel/123/'))
                handle = dialog._worker; self.assertEqual(self.manager.get(handle.task_id)['status'], 'queued')
                dialog.close(); self.assertEqual(self.manager.get(handle.task_id)['status'], 'queued')
                self.release.set(); self.wait_until(lambda: not self.manager.is_busy())
                self.assertEqual(self.manager.get(handle.task_id)['status'], 'succeeded'); download.assert_called_once()
            dialog.deleteLater()


if __name__ == "__main__": unittest.main()
