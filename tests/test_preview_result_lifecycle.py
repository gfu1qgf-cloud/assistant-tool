"""Poster results distinguish not-yet-shown editors from explicitly closed ones."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.store import Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class PreviewResultLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_valid_constructor_result_is_accepted_but_closed_and_obsolete_results_are_not(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(BatchTextVideoDialog,'request_background'), \
                patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=Store(Path(folder)))
            try:
                access=dialog.association_access
                job=dialog.active_entry()
                context=access.context(job,dialog.read_settings())
                result=dict(association_context=context,associations=dict(dependency_files=[]))
                self.assertFalse(dialog.isVisible())
                self.assertTrue(access.accept(result))
                stale=dict(result,association_context=dict(context,profile='another-project'))
                self.assertFalse(access.accept(stale))
                stale=dict(result,association_context=dict(context,token='obsolete-binding'))
                self.assertFalse(access.accept(stale))
                dialog.close()
                self.assertFalse(access.accept(result))
                dialog.show()
                self.assertTrue(access.accept(result))
            finally:
                dialog.close();dialog.deleteLater()
                self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_refresh_of_pathless_task_uses_empty_background_without_callback_error(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(BatchTextVideoDialog,'request_background') as request, \
                patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=Store(folder))
            try:
                request.reset_mock()
                with patch.object(dialog,'active_entry',return_value={'task_dir':folder}):
                    dialog.refresh_preview()
                request.assert_called_once_with('',force=True)
            finally:
                dialog.close();dialog.deleteLater()
                self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
                self.app.processEvents()
