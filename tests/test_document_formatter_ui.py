"""Real host queue, safe Qt callbacks, working controls and plugin lifecycle."""
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from model.AppDataStorage import ApplicationStorage
from model.BackgroundTasks import BackgroundTaskManager
from app_plugins.builtin.document_formatter.plugin import DocumentFormatterPlugin
from app_plugins.builtin.document_formatter.ui import DocumentFormatterDialog
from app_plugins.builtin.document_formatter.odt import inspect_odt
from test_document_formatter import fixture_odt


class DocumentFormatterGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.window = QtWidgets.QWidget(); self.manager = BackgroundTaskManager(self.window)
        self.storage = ApplicationStorage(self.root/'private'); self.release = threading.Event()
        self.context = SimpleNamespace(parent_widget=self.window, background_tasks=self.manager,
                                       storage=self.storage.for_plugin('document_formatter'), register_command=Mock(), notify=Mock())
        self.context.submit_background_task = lambda **kw: self.manager.submit(owner='document_formatter', **kw)
        self.dialogs = []
        for name in ('warning', 'information'):
            p = patch.object(QtWidgets.QMessageBox, name); setattr(self, name, p.start()); self.addCleanup(p.stop)
        p = patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes)
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.release.set(); self.manager.cancel_all(); self.wait(lambda: not self.manager.is_busy()); self.manager.shutdown()
        for dialog in self.dialogs: dialog.stopping = True; dialog.close(); dialog.deleteLater()
        self.window.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents(); self.temp.cleanup()

    def wait(self, predicate, seconds=8):
        end = time.monotonic()+seconds
        while not predicate() and time.monotonic() < end: self.app.processEvents(); time.sleep(.005)
        self.assertTrue(predicate())

    def dialog(self):
        value = DocumentFormatterDialog(self.context, self.window); self.dialogs.append(value); return value

    def load_fixture(self, dialog):
        source = self.root/'fixture.odt'; fixture_odt(source)
        dialog.sources.append(str(source)); dialog.mode_changed(); dialog.read_document()
        self.wait(lambda: dialog.handle is None)
        self.assertTrue(dialog.current_snapshot())
        return source

    def test_plugin_registers_one_command_without_startup_network_or_data_writes(self):
        plugin = DocumentFormatterPlugin(); plugin.register(self.context); plugin.start()
        self.assertEqual(self.context.register_command.call_count, 1)
        self.assertEqual(plugin.plugin_id, 'document_formatter'); self.assertFalse((self.root/'private').exists())
        dialog = plugin.open_dialog(); self.dialogs.append(dialog)
        plugin.stop(); self.assertTrue(dialog.stopping); self.assertFalse(dialog.isVisible())

    def test_all_visible_buttons_and_menu_items_work_and_layout_fits(self):
        dialog = self.dialog(); dialog.resize(1150, 800); dialog.show(); self.app.processEvents()
        for button in dialog.findChildren(QtWidgets.QAbstractButton):
            if button.isVisible() and isinstance(button, (QtWidgets.QPushButton, QtWidgets.QCheckBox)):
                self.assertGreater(button.receivers(button.clicked) + button.receivers(button.toggled), 0, button.text())
        for top in dialog.menu.actions():
            for action in top.menu().actions():
                if not action.isSeparator(): self.assertGreater(action.receivers(action.triggered), 0, action.text())
        self.assertLessEqual(dialog.minimumSizeHint().width(), 1150); self.assertLessEqual(dialog.minimumSizeHint().height(), 800)
        if os.environ.get('FORMATTER_QA'):
            font_id = QtGui.QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\msyh.ttc')
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families: dialog.setFont(QtGui.QFont(families[0], 9))
            self.load_fixture(dialog); self.app.processEvents()
            path = Path(os.environ['FORMATTER_QA']); path.mkdir(parents=True, exist_ok=True)
            self.assertTrue(dialog.grab().save(str(path/'document-formatter.png')))

    def test_local_read_roles_save_and_export_use_real_host_jobs_preserving_source(self):
        dialog = self.dialog(); source = self.load_fixture(dialog); before = source.read_bytes()
        self.assertEqual(dialog.table.rowCount(), 4)
        dialog.table.cellWidget(0, 1).setCurrentIndex(1)
        self.assertEqual(dialog.roles[dialog.active_source]['p0'], 'body')
        dialog.fields['title_pt'].setValue(15); dialog.save_rules()
        self.assertEqual(self.context.storage.file('preferences.json').read_json()['title_pt'], 15)
        dialog.format_current(); self.wait(lambda: dialog.handle is None)
        self.assertEqual(source.read_bytes(), before); self.assertEqual(len(dialog.outputs), 1)
        self.assertTrue(Path(dialog.outputs[0]).is_file()); self.assertEqual(dialog.progress.value(), 100)
        self.assertTrue(dialog.location_button.isEnabled())
        with patch('app_plugins.builtin.document_formatter.ui.open_file_location', return_value=True) as reveal:
            dialog.location_button.click(); reveal.assert_called_once_with(dialog.outputs[0], dialog)

    def test_batch_reads_second_document_automatically_without_manual_previews(self):
        dialog = self.dialog(); first = self.load_fixture(dialog)
        other = self.root/'unpreviewed.odt'; fixture_odt(other); dialog.sources.append(str(other))
        dialog.format_all(); self.wait(lambda: dialog.handle is None)
        self.assertEqual(len(dialog.outputs), 2); self.assertTrue(first.exists()); self.assertTrue(other.exists())

    def test_hidden_window_keeps_host_job_and_cancel_restores_controls(self):
        dialog = self.dialog()
        def work(_payload, context):
            while not self.release.wait(.02): context.check_canceled()
            return {'kind': 'auth', 'paths': []}
        with patch('app_plugins.builtin.document_formatter.ui.run_document_job', work):
            dialog.submit('authorize'); handle = dialog.handle
            dialog.close(); self.assertFalse(dialog.isVisible()); self.assertTrue(handle.isRunning())
            dialog.cancel_job(); self.wait(lambda: dialog.handle is None)
        self.assertTrue(dialog.read_button.isEnabled()); self.assertIn('取消', dialog.status.text())

    def test_gui_errors_are_contained_and_native_deleted_callbacks_are_ignored(self):
        dialog = self.dialog(); dialog.format_current()
        self.warning.assert_called_once(); self.assertIn('先读取', dialog.status.text())
        dialog.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        dialog.completed({'kind': 'auth'}); dialog.report('late', 1); dialog.failed('late')
        self.dialogs.remove(dialog)

    def test_google_url_change_cannot_format_previously_loaded_document(self):
        from test_document_formatter import FakeDocs
        from app_plugins.builtin.document_formatter.google_docs import inspect_document
        dialog = self.dialog(); dialog.mode.setCurrentIndex(1)
        snapshot = inspect_document(FakeDocs(), 'fixture_document_123')
        dialog.completed(snapshot); dialog.google_url.setText('https://docs.google.com/document/d/different_document_456/edit')
        with patch.object(dialog, 'submit') as submit: dialog.format_current()
        submit.assert_not_called(); self.assertIn('链接已改变', dialog.status.text())


if __name__ == '__main__': unittest.main()
