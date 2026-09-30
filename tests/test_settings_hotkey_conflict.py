import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PYUI.main_pyui import MainDialog
from PYUI.main_setting_pyui import MainSettingDialog
from model.FlowParameterGuard import normalize_flow_guard_settings
from globalValue import globalValue


class SettingsHotkeyConflictTests(unittest.TestCase):
    def test_existing_hotkey_conflict_is_visible_without_nested_warning(self):
        settings = {
            "task_result_global_hotkey": "Ctrl+Alt+R",
            "load_task_global_hotkey": "Ctrl+Alt+L",
        }
        manager = lambda active: SimpleNamespace(is_registered=active)
        window = mock.Mock()
        window.load_config.side_effect = [dict(settings), dict(settings)]
        window.flow_guard_settings = normalize_flow_guard_settings({})
        window.load_task_global_hotkey = "Ctrl+Alt+L"
        window.load_task_hotkey_manager = manager(True)
        window.chrome_plugin.global_hotkey = "Ctrl+Alt+N"
        window.chrome_plugin.hotkey_manager = manager(True)
        window.task_delivery_plugin.global_hotkey = "Ctrl+Alt+R"
        window.task_delivery_plugin.controller.hotkey_manager = manager(False)
        window.inventory_plugin.global_hotkey = "Ctrl+Alt+I"
        window.inventory_plugin.hotkey_manager = manager(True)
        window.plugin_host.apply_settings.return_value = [("task_delivery", True)]
        with mock.patch.object(MainSettingDialog, "get_settings", return_value=settings), \
             mock.patch.object(globalValue, "loaded_whisper_model_name", return_value="base"), \
             mock.patch("PYUI.main_pyui.apply_ui_theme"), \
             mock.patch("PYUI.main_pyui.QMessageBox.information") as info, \
             mock.patch("PYUI.main_pyui.QMessageBox.warning") as warning:
            MainDialog.openSettings(window)
        window.registerLoadTaskGlobalHotkey.assert_not_called()
        warning.assert_not_called()
        info.assert_not_called()
        self.assertIn(
            "整理任务结果：Ctrl+Alt+R（未启用",
            window.appendLog.call_args.args[0],
        )

    def test_theme_is_not_reapplied_when_unchanged(self):
        settings = {
            "task_result_global_hotkey": "Ctrl+Alt+R",
            "load_task_global_hotkey": "Ctrl+Alt+L",
            "ui_theme": "light",
        }
        manager = SimpleNamespace(is_registered=True)
        window = mock.Mock()
        window.load_config.side_effect = [dict(settings), dict(settings)]
        window.flow_guard_settings = normalize_flow_guard_settings({})
        window.load_task_global_hotkey = "Ctrl+Alt+L"
        window.load_task_hotkey_manager = manager
        window.chrome_plugin.global_hotkey = "Ctrl+Alt+N"
        window.chrome_plugin.hotkey_manager = manager
        window.task_delivery_plugin.global_hotkey = "Ctrl+Alt+R"
        window.task_delivery_plugin.controller.hotkey_manager = manager
        window.inventory_plugin.global_hotkey = "Ctrl+Alt+I"
        window.inventory_plugin.hotkey_manager = manager
        window.plugin_host.apply_settings.return_value = []
        with mock.patch.object(MainSettingDialog, "get_settings", return_value=settings), \
             mock.patch.object(globalValue, "loaded_whisper_model_name", return_value="base"), \
             mock.patch("PYUI.main_pyui.apply_ui_theme") as apply_theme, \
             mock.patch("PYUI.main_pyui.QtCore.QTimer.singleShot") as single_shot:
            MainDialog.openSettings(window)
        apply_theme.assert_not_called()
        single_shot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
