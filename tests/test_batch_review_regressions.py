"""Preserve unsaved work and wait for local workers during host shutdown."""
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import BackgroundTaskHandle
from model.QtThreadLifecycle import connect_thread_finished
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.plugin import BatchTextVideoPlugin
from app_plugins.builtin.batch_text_video.store import Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class BatchReviewRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.background = patch.object(BatchTextVideoDialog, 'request_background')
        self.background.start()
        self.addCleanup(self.background.stop)
        self.dialog = BatchTextVideoDialog(store=Store(self.temp.name))
        self.dialog.show()
        self.wait(lambda: self.dialog.overlay_worker is None)
        self.dialog.edit_timer.stop()
        self.plugin = BatchTextVideoPlugin()
        self.plugin.dialog = self.dialog

    def wait(self, condition):
        deadline = time.monotonic() + 10
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def tearDown(self):
        self.dialog.worker = None
        self.dialog.edit_timer.stop()
        self.wait(lambda: self.dialog.overlay_worker is None)
        with patch.object(self.dialog, 'persist', return_value=True):
            self.dialog.close()
        self.dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_failed_close_save_keeps_editor_visible_and_edits_in_memory(self):
        self.dialog.layer_panel.add_text()
        identifier = self.dialog.layer_panel.current()['id']
        self.dialog.layer_panel.set_geometry(identifier, dict(x=83, y=62, width=72, height=40))
        self.dialog.edit_timer.stop()
        before = self.dialog.store.path.read_bytes() if self.dialog.store.path.exists() else None
        with patch.object(self.dialog.store, 'save', side_effect=OSError('synthetic disk full')):
            self.dialog.close()
        self.assertTrue(self.dialog.isVisible(), 'save failure must not hide the only copy of pending work')
        self.assertEqual(self.dialog.layer_panel.current()['x'], 83)
        self.assertIn('保存失败', self.dialog.status.text())
        after = self.dialog.store.path.read_bytes() if self.dialog.store.path.exists() else None
        self.assertEqual(before, after)

    def test_plugin_stop_reports_failed_save_instead_of_allowing_app_exit(self):
        with patch.object(self.dialog.store, 'save', side_effect=PermissionError('synthetic locked volume')):
            result = self.plugin.stop()
        self.assertIs(result, False)
        self.assertTrue(self.dialog.isVisible())

    def test_retry_after_storage_recovery_saves_and_hides_normally(self):
        with patch.object(self.dialog.store, 'save', side_effect=OSError('synthetic disk full')):
            self.dialog.close()
        self.assertTrue(self.dialog.isVisible())
        self.dialog.close()
        self.assertFalse(self.dialog.isVisible())
        self.assertTrue(self.dialog.store.path.is_file())
        self.assertIsNot(self.plugin.stop(), False)

    def test_host_handle_does_not_hide_a_still_running_local_preview(self):
        handle = Mock(spec=BackgroundTaskHandle)
        self.assertIsInstance(handle, BackgroundTaskHandle)
        # The host handle can already be terminal while its UI callback is queued.
        handle.isRunning.return_value = False
        self.dialog.worker = handle
        original = self.dialog.overlay_worker
        self.dialog.overlay_worker = Mock()
        self.dialog.edit_timer.start(600)
        try:
            allowed, message = self.plugin.can_close()
            self.assertFalse(allowed, 'local workers must be checked even when a host handle is present')
            self.assertTrue(message)
            handle.requestInterruption.assert_not_called()
            self.assertFalse(self.dialog.edit_timer.isActive())
        finally:
            self.dialog.overlay_worker = original
            self.dialog.worker = None

    def test_host_owned_job_can_continue_when_plugin_window_hides(self):
        handle = Mock(spec=BackgroundTaskHandle)
        handle.isRunning.return_value = True
        self.dialog.worker = handle
        allowed, _ = self.plugin.can_close()
        self.assertTrue(allowed)
        with patch.object(self.dialog, 'persist') as save:
            self.assertIsNot(self.plugin.stop(), False)
        self.assertFalse(self.dialog.isVisible())
        handle.requestInterruption.assert_not_called()
        save.assert_not_called()

    def test_real_local_thread_is_joined_before_shutdown_is_allowed(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        class Preview(QtCore.QThread):
            def __init__(self, parent):
                super().__init__(parent)
                self.cancel = threading.Event()
            def run(self):
                entered.set()
                release.wait(3)
        worker = Preview(self.dialog)
        self.dialog.overlay_worker = worker
        connect_thread_finished(worker, self.dialog, 'overlay_worker', self.dialog._overlay_finished)
        handle = Mock(spec=BackgroundTaskHandle)
        self.dialog.worker = handle
        worker.start()
        try:
            self.wait(entered.is_set)
            self.assertFalse(self.plugin.can_close()[0])
            self.assertTrue(worker.cancel.is_set())
            self.assertTrue(worker.isRunning())
            handle.requestInterruption.assert_not_called()
        finally:
            release.set()
            self.wait(lambda: self.dialog.overlay_worker is None)
            self.dialog.worker = None
        self.assertTrue(self.plugin.can_close()[0])


if __name__ == '__main__':
    unittest.main()
