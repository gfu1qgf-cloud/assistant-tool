import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import ApplicationStorage
from model.GoogleAccounts import GoogleAccountManager
from PYUI.google_accounts_pyui import GoogleAccountsPage


class GoogleAccountsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.manager = GoogleAccountManager(ApplicationStorage(self.temp.name))
        self.page = GoogleAccountsPage(manager=self.manager)

    def tearDown(self):
        self.page.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_construct_and_show_empty_page_do_not_write_credentials(self):
        self.page.show(); self.app.processEvents()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertFalse(self.page.login_button.isEnabled())
        self.assertEqual(self.page.accounts.rowCount(), 0)

    def test_core_settings_contains_google_page_not_plugin(self):
        from PYUI.main_setting_pyui import MainSettingDialog
        with patch.object(MainSettingDialog, 'load_config'):
            dialog = MainSettingDialog()
        try:
            index = dialog.settingTabWidget.indexOf(dialog.google_accounts_page)
            self.assertEqual(dialog.settingTabWidget.tabText(index), 'Google 账号')
            self.assertFalse(dialog.google_accounts_page.property('settings_plugin_id'))
            self.assertEqual(dialog.settings_navigation.items[index].parent().text(0), '程序设置')
        finally:
            dialog.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_reload_lists_only_public_account_data_and_no_autobind(self):
        from test_google_accounts import CLIENT, IDENTITY, token
        key = self.manager.add_client(CLIENT, 'Fixture')
        self.manager.save_login(key, IDENTITY, token())
        before = self.manager.file.path.read_bytes()
        self.page.reload()
        self.assertEqual(self.page.accounts.item(0, 0).text(), IDENTITY['email'])
        self.assertNotIn('fixture-secret', self.page.accounts.item(0, 1).text())
        self.assertEqual(self.page.bindings['drive'].currentData(), '')
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_cancel_client_selection_keeps_store_absent(self):
        with patch.object(QtWidgets.QFileDialog, 'getOpenFileName', return_value=('', '')):
            self.page.import_client()
        self.assertFalse(self.manager.file.exists())

    def test_corrupt_store_error_is_inline_not_crash(self):
        self.manager.file.path.write_text('broken')
        self.page.reload()
        self.assertIn('未覆盖', self.page.status.text())

    def test_no_permission_does_not_submit_background_task(self):
        self.page.tasks = Mock()
        for box in self.page.permissions.values(): box.setChecked(False)
        self.page.login()
        self.page.tasks.submit.assert_not_called()
        self.assertIn('至少', self.page.status.text())

    def test_host_login_runs_off_gui_and_window_can_be_destroyed(self):
        import threading
        import time
        from model.BackgroundTasks import BackgroundTaskManager
        from test_google_accounts import CLIENT
        self.manager.add_client(CLIENT, 'Fixture')
        tasks = BackgroundTaskManager(logger=lambda *_args: None)
        release = threading.Event(); entered = threading.Event(); thread_ids = []
        def run(payload, context):
            thread_ids.append(threading.get_ident()); entered.set()
            release.wait(3); context.check_canceled()
            return {'account_id': 'synthetic'}
        page = GoogleAccountsPage(tasks=tasks, manager=self.manager)
        page.reload()
        try:
            with patch('PYUI.google_accounts_pyui.authorize', new=run):
                page.login(); page.login()
                self.assertEqual(len(tasks.records()), 1)
                self.assertFalse(page.login_button.isEnabled())
                deadline = time.monotonic() + 5
                while not entered.is_set() and time.monotonic() < deadline:
                    self.app.processEvents(); time.sleep(.005)
                self.assertTrue(entered.is_set())
                self.assertNotEqual(thread_ids[0], threading.get_ident())
                page.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                release.set()
                while tasks.is_busy() and time.monotonic() < deadline:
                    self.app.processEvents(); time.sleep(.005)
                self.assertFalse(tasks.is_busy())
                self.assertEqual(tasks.records()[0]['status'], 'succeeded')
        finally:
            release.set(); tasks.cancel_all()
            deadline = time.monotonic() + 5
            while tasks.is_busy() and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.005)
            tasks.shutdown(); tasks.deleteLater(); self.app.processEvents()

    def test_native_finish_owns_page_until_finished_signal(self):
        # A succeeded result may arrive before the native worker finishes.
        self.page.handle = Mock()
        self.page.handle.isRunning.return_value = False
        self.page.update_buttons()
        self.assertFalse(self.page.login_button.isEnabled())
        old = self.page.handle
        self.page.finished()  # Not emitted by the owning handle: ignore it.
        self.assertIs(self.page.handle, old)


if __name__ == '__main__': unittest.main()
