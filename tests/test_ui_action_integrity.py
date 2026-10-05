"""Check live connections without triggering downloads, uploads or model inference."""
from contextlib import ExitStack
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from PYUI.main_pyui import MainDialog
from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.chrome_runner_pyui import ChromeRunnerDialog
from app_plugins.host import PluginHost


class FakeHotkey(QtCore.QObject):
    activated = QtCore.pyqtSignal()
    def __init__(self, parent=None, **kwargs):
        super().__init__(parent)
        self.last_error = ''
    def register(self, *args):
        return True
    def close(self):
        pass


class UIActionIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def assert_buttons_wired(self, window):
        missing = []
        for button in window.findChildren(QtWidgets.QAbstractButton):
            # Tabs, spinner arrows, drag handles and menu-open buttons have native behavior.
            if not isinstance(button, (QtWidgets.QPushButton, QtWidgets.QToolButton)):
                continue
            if not button.isVisibleTo(window) or button.menu():
                continue
            if button.objectName() in {'qt_tabwidget_leftcorner', 'qt_tabwidget_rightcorner',
                                       'ScrollLeftButton', 'ScrollRightButton'}:
                continue
            if type(button).__name__ == '_PluginWidgetDragHandle':
                continue
            if button.receivers(button.clicked) + button.receivers(button.toggled) == 0:
                missing.append((button.objectName(), button.text(), type(button).__name__))
        self.assertEqual(missing, [], 'Buttons with no connected action: ' + repr(missing))

    def test_main_buttons_and_registered_menus_have_live_connections(self):
        def start_without_services(host):
            host.plugin('music_ducker')._bind_main_controls()
        with ExitStack() as stack:
            for module in ('PYUI.main_pyui', 'app_plugins.builtin.inventory',
                           'app_plugins.builtin.chrome_launcher',
                           'app_plugins.builtin.task_delivery_controller'):
                stack.enter_context(patch(module + '.GlobalHotkeyManager', FakeHotkey))
            stack.enter_context(patch.object(PluginHost, 'start_all', start_without_services))
            for method in ('setupNotificationTray', 'startGoogleSheetMonitor', 'startFlowParameterGuard',
                           'saveCurrentConfig'):
                stack.enter_context(patch.object(MainDialog, method, return_value=True))
            stack.enter_context(patch.object(ChromeRunnerDialog, 'load_profiles'))
            window = MainDialog()
            try:
                window.show()
                self.app.processEvents()
                self.assert_buttons_wired(window)
                checked = 0
                def inspect_menu(menu):
                    nonlocal checked
                    for action in menu.actions():
                        if action.isSeparator():
                            continue
                        if action.menu():
                            inspect_menu(action.menu())
                        else:
                            self.assertGreater(action.receivers(action.triggered), 0, action.text())
                            checked += 1
                for action in window.main_menu_bar.actions():
                    if action.menu():
                        inspect_menu(action.menu())
                self.assertGreater(checked, 20)
                task_menu = QtWidgets.QMenu(window)
                actions = window.plugin_host.populate_task_context_menu(task_menu, [0])
                self.assertGreater(len(actions), 10)
                self.assertTrue(all(action.receivers(action.triggered) > 0 for action in actions))
            finally:
                window.config_reload.timer.stop()
                window.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_chrome_dialog_buttons_have_live_connections(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ChromeRunnerDialog, 'load_profiles'):
            dialog = ChromeRunnerDialog(config_path=os.path.join(folder, 'config.json'))
            try:
                self.assert_buttons_wired(dialog)
            finally:
                dialog.deleteLater()

    def test_settings_buttons_have_live_connections(self):
        dialog = MainSettingDialog()
        try:
            for index in range(dialog.settingTabWidget.count()):
                dialog.settingTabWidget.setCurrentIndex(index)
                self.assert_buttons_wired(dialog)
        finally:
            dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
