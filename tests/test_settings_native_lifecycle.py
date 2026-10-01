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
                assert 'waste_reminder.tonight' in window.plugin_host.main_widget_order()
                assert window.plugin_host.main_widget_frame('waste_reminder', 'tonight') is not None
                assert window.waste_reminder_plugin.main_panel is not None
                assert any('AI 密钥管理' in action.text() for action in window.tools_menu.actions())
                assert window.task_delivery_plugin.context.gemini_keys is window.gemini_keys
                assert window.video_prompt_assistant_plugin.context.gemini_keys is window.gemini_keys
                assert window.cooking_assistant_plugin.context.gemini_keys is window.gemini_keys
                assert any('做饭小助手' in action.text() for action in window.plugin_host.menu.actions())
                window.gemini_keys.apply_config({'gemini_api_keys': ['test-key']})
                worker = threading.Thread(target=lambda: window.gemini_keys.report_failure('test-key', 'test-model', 429))
                worker.start()
                worker.join()
                assert not window.gemini_save_timer.isActive()
                app.processEvents()
                assert window.gemini_save_timer.isActive()
                window.gemini_save_timer.stop()
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
                            assert dialog.settingTabWidget.indexOf(dialog.gemini_keys_editor) >= 0
                            assert not hasattr(dialog, 'task_result_gemini_keys_edit')
                            assert '做饭小助手' in [dialog.settingTabWidget.tabText(i) for i in range(dialog.settingTabWidget.count())]
                            dialog.done(QtWidgets.QDialog.DialogCode.Accepted)
                        QtCore.QTimer.singleShot(0, close_settings)
                        window.openSettings()
                        app.processEvents()
                old = {'gemini_api_keys': ['old']}
                new = {'gemini_api_keys': ['new']}
                with mock.patch.object(MainDialog, 'load_config', side_effect=[old, new]), \\
                     mock.patch.object(MainSettingDialog, 'get_settings', return_value=new), \\
                     mock.patch.object(PluginHost, 'apply_settings') as plugins, \\
                     mock.patch.object(MainDialog, 'restartFlowParameterGuard') as guard:
                    window.openSettings()
                    assert window.gemini_keys.request_key('test-model') == 'new'
                    plugins.assert_not_called()
                    guard.assert_not_called()
                old = {'cooking_assistant': {'model': 'old-model'}}
                new = {'cooking_assistant': {'model': 'new-model'}}
                with mock.patch.object(MainDialog, 'load_config', side_effect=[old, new]), \\
                     mock.patch.object(MainSettingDialog, 'get_settings', return_value=new), \\
                     mock.patch.object(PluginHost, 'apply_settings') as plugins, \\
                     mock.patch.object(MainDialog, 'restartFlowParameterGuard') as guard:
                    window.openSettings()
                    assert window.cooking_assistant_plugin.settings['model'] == 'new-model'
                    plugins.assert_not_called()
                    guard.assert_not_called()
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
