import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app_plugins.builtin.chrome_launcher import ChromeLauncherPlugin
from app_plugins.builtin.inventory import InventoryPlugin
from model.GlobalHotkey import hotkey_setting_changed


class HotkeySettingsTests(unittest.TestCase):
    def test_equivalent_shortcut_is_not_a_change(self):
        self.assertFalse(hotkey_setting_changed("Ctrl+Alt+R", "Ctrl+Alt+R"))
        self.assertTrue(hotkey_setting_changed("Ctrl+Alt+F10", "Ctrl+Alt+R"))

    def test_chrome_does_not_retry_unchanged_occupied_shortcut(self):
        plugin = SimpleNamespace(
            global_hotkey="Ctrl+Alt+N", _register_hotkey=mock.Mock(return_value=False),
        )
        self.assertTrue(ChromeLauncherPlugin.apply_settings(plugin, {
            "chrome_next_global_hotkey": "Ctrl+Alt+N",
        }))
        plugin._register_hotkey.assert_not_called()
        self.assertFalse(ChromeLauncherPlugin.apply_settings(plugin, {
            "chrome_next_global_hotkey": "Ctrl+Alt+F10",
        }))
        plugin._register_hotkey.assert_called_once_with("Ctrl+Alt+F10", show_error=False)

    def test_inventory_updates_other_options_without_retrying_unchanged_shortcut(self):
        plugin = SimpleNamespace(
            global_hotkey="Ctrl+Alt+I", _register_hotkey=mock.Mock(return_value=False),
            set_clipboard_monitor=mock.Mock(), _configure_material_sync_timer=mock.Mock(),
        )
        self.assertTrue(InventoryPlugin.apply_settings(plugin, {
            "inventory_manager_global_hotkey": "Ctrl+Alt+I",
            "clipboard_google_drive_monitor_enabled": True,
        }))
        plugin._register_hotkey.assert_not_called()
        plugin.set_clipboard_monitor.assert_called_once_with(True, save=False)


if __name__ == "__main__":
    unittest.main()
