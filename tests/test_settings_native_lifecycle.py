"""Exercise the real Qt settings dialog in a child process.

A Qt fatal error terminates the interpreter, so an in-process test would take
the entire test runner down with it.
"""

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SettingsNativeLifecycleTests(unittest.TestCase):
    def test_repeated_settings_close_keeps_qt_process_alive(self):
        script = textwrap.dedent("""
            import sys
            import threading
            from unittest import mock
            from qt_compat import QtCore, QtWidgets
            from PYUI.main_pyui import MainDialog
            from app_plugins.host import PluginHost
            from PYUI.main_setting_pyui import MainSettingDialog
            from model.AppTheme import apply_ui_theme

            app = QtWidgets.QApplication([])
            apply_ui_theme(app, 'light')
            with mock.patch.object(PluginHost, 'start_all'), \\
                 mock.patch.object(MainDialog, 'startGoogleSheetMonitor'), \\
                 mock.patch.object(MainDialog, 'startFlowParameterGuard'), \\
                 mock.patch.object(MainDialog, 'setupNotificationTray'), \\
                 mock.patch.object(MainDialog, 'registerLoadTaskGlobalHotkey', return_value=True):
                window = MainDialog()
                worker = threading.Thread(target=lambda: window.appendLog('[test] worker log'))
                worker.start()
                worker.join()
                app.processEvents()
                assert '[test] worker log' in window.log_text_edit.toPlainText()
                with mock.patch.object(PluginHost, 'apply_settings', side_effect=[
                        [], [('task_delivery', False)], [],
                     ]), \\
                     mock.patch.object(MainDialog, 'restartFlowParameterGuard', return_value=True), \\
                     mock.patch.object(MainDialog, 'saveCurrentConfig', return_value=True), \\
                     mock.patch.object(MainDialog, 'showDesktopNotification'):
                    for _ in range(3):
                        def close_settings():
                            dialog = app.activeModalWidget()
                            assert isinstance(dialog, MainSettingDialog)
                            dialog.done(QtWidgets.QDialog.DialogCode.Accepted)
                        QtCore.QTimer.singleShot(0, close_settings)
                        window.openSettings()
                        app.processEvents()
                window.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(
                    None, QtCore.QEvent.Type.DeferredDelete
                )
                app.processEvents()
            print('settings-cycle-ok')
        """)
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertEqual(
            result.returncode, 0,
            f"Qt process exited {result.returncode}:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("settings-cycle-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
