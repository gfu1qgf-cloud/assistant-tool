import os
import unittest
from copy import deepcopy
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from app_plugins.api import (MAIN_MENU, TASK_CONTEXT_MENU, TOOLS_MENU, PluginCommand,
                             PluginMainWidget, PluginSettingsPage, PluginTabPage)
from app_plugins.host import PluginHost, PLUGIN_ENABLED_CONFIG_KEY, plugin_enabled_states
from PYUI.plugin_manager_pyui import PluginManagerDialog


class Window(QtWidgets.QWidget):
    def __init__(self, config=None):
        super().__init__()
        self.config = deepcopy(config or {})
        self.logs = []
        self.save_ok = True

    def load_config(self):
        return deepcopy(self.config)

    def appendLog(self, text, **_kwargs):
        self.logs.append(text)

    def saveCurrentConfig(self, config_updates=None, expected_config=None):
        if not self.save_ok:
            return False
        for key in config_updates or {}:
            if self.config.get(key) != expected_config.get(key):
                return False
        self.config.update(deepcopy(config_updates or {}))
        return True


class CountingPlugin:
    display_name = "测试提醒"
    version = "1.0"

    def __init__(self, plugin_id="demo"):
        self.plugin_id = plugin_id
        self.calls = []
        self.context = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand("open", self.display_name, self.invoke,
            frozenset({MAIN_MENU, TASK_CONTEXT_MENU, TOOLS_MENU})))
        context.register_main_widget(PluginMainWidget("card", self.widget))
        context.register_tab_page(PluginTabPage("tab", "测试分页", self.widget))
        context.register_settings_page(PluginSettingsPage("settings", "测试设置", self.widget))

    def widget(self, parent):
        self.calls.append("widget")
        return QtWidgets.QPushButton("测试", parent)

    def invoke(self, rows):
        self.calls.append("invoke")

    def start(self):
        self.calls.append("start")

    def apply_settings(self, _config):
        self.calls.append("apply")
        return True

    def update_config(self, config):
        self.calls.append("update")
        config["private_history"] = []

    def can_close(self):
        self.calls.append("can_close")
        return True, ""

    def stop(self):
        self.calls.append("stop")


class PluginManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def make_host(self, config=None):
        window = Window(config)
        self.addCleanup(window.deleteLater)
        return window, PluginHost(window)

    def test_missing_config_and_invalid_non_booleans_keep_legacy_enabled(self):
        self.assertEqual(plugin_enabled_states({}), {})
        self.assertEqual(plugin_enabled_states({PLUGIN_ENABLED_CONFIG_KEY: ["demo"]}), {})
        states = plugin_enabled_states({PLUGIN_ENABLED_CONFIG_KEY: {
            "demo": False, "string": "false", "number": 0, "yes": True}})
        self.assertEqual(states, {"demo": False, "yes": True})
        window, host = self.make_host()
        plugin = host.install(CountingPlugin())
        host.start_all()
        host.start_all()
        self.assertEqual(plugin.calls, ["start"])

    def test_disabled_plugin_does_not_start_update_close_or_accept_jobs(self):
        config = {PLUGIN_ENABLED_CONFIG_KEY: {"demo": False}, "private_history": ["old"]}
        window, host = self.make_host(config)
        plugin = host.install(CountingPlugin())
        host.start_all()
        host.apply_settings(config)
        saved = deepcopy(config)
        host.update_runtime_config(saved)
        self.assertEqual(saved["private_history"], ["old"])
        self.assertEqual(host.can_close_all(), (True, ""))
        host.stop_all()
        host.invoke("demo.open")
        with self.assertRaisesRegex(RuntimeError, "禁用"):
            plugin.context.submit_background_task(title="disabled", runner=lambda context: None)
        self.assertIsNone(host._background_tasks)
        self.assertEqual(plugin.calls, [])

    def test_all_disabled_still_have_manager_but_no_other_ui_factories(self):
        window, host = self.make_host({PLUGIN_ENABLED_CONFIG_KEY: {"demo": False}})
        plugin = host.install(CountingPlugin())
        menu = host.attach_main_menu(QtWidgets.QMenuBar(window))
        self.assertIn("插件管理…", [action.text() for action in menu.actions()])
        self.assertNotIn(plugin.display_name, [action.text() for action in menu.actions()])
        tools = QtWidgets.QMenu(window)
        host.attach_tools_menu(tools)
        self.assertFalse(tools.actions())
        self.assertFalse(host.populate_task_context_menu(QtWidgets.QMenu(window), [0]))
        tabs = QtWidgets.QTabWidget(window)
        placeholder = QtWidgets.QWidget(tabs)
        tabs.addTab(placeholder, "达芬奇遥控器")
        host.attach_tab_area(tabs, placeholder)
        self.assertFalse(tabs.isTabVisible(0))
        host.create_settings_pages(window, tabs)
        host.attach_main_widget_area(QtWidgets.QFrame(window))
        self.assertEqual(plugin.calls, [])
        self.assertEqual(host.main_widget_order(), ["demo.card"])

    def test_only_legacy_compatibility_controls_are_created_hidden_and_disabled(self):
        window, host = self.make_host({PLUGIN_ENABLED_CONFIG_KEY: {"demo": False}})
        plugin = host.install(CountingPlugin())
        host.attach_main_widget_area(QtWidgets.QFrame(window), compatibility_ids=("demo.card",))
        card = host.main_widget_frame("demo", "card")
        self.assertTrue(card.isHidden())
        self.assertFalse(card.isEnabled())
        self.assertEqual(plugin.calls, ["widget"])

    def test_disabled_card_position_and_unknown_plugin_choices_survive_save(self):
        window, host = self.make_host({PLUGIN_ENABLED_CONFIG_KEY: {"demo": False, "future": False},
            "plugin_main_widget_order": ["other.card", "demo.card"]})
        host.install(CountingPlugin())
        host.install(CountingPlugin("other"))
        host.attach_main_widget_area(QtWidgets.QFrame(window))
        saved = window.load_config()
        host.update_runtime_config(saved)
        self.assertEqual(saved["plugin_main_widget_order"], ["other.card", "demo.card"])
        expected = window.load_config()
        self.assertTrue(host.save_plugin_states({"demo": True}, expected=expected))
        self.assertEqual(window.config[PLUGIN_ENABLED_CONFIG_KEY], {"demo": True, "future": False})
        self.assertFalse(host.is_enabled("demo"))
        next_host = PluginHost(window)
        self.assertTrue(next_host.is_enabled("demo"))

    def test_conflicting_external_change_is_not_overwritten(self):
        window, host = self.make_host({PLUGIN_ENABLED_CONFIG_KEY: {"demo": True}})
        host.install(CountingPlugin())
        expected = window.load_config()
        window.config[PLUGIN_ENABLED_CONFIG_KEY]["future"] = False
        self.assertFalse(host.save_plugin_states({"demo": False}, expected=expected))
        self.assertEqual(window.config[PLUGIN_ENABLED_CONFIG_KEY], {"demo": True, "future": False})

    def test_ui_bulk_actions_only_change_visible_selected_rows(self):
        window, host = self.make_host()
        host.install(CountingPlugin())
        host.install(CountingPlugin("other"))
        dialog = PluginManagerDialog(host, window)
        dialog.reload()
        dialog.search.setText("demo")
        dialog.select_visible()
        dialog.set_selected(False)
        self.assertEqual(dialog.choices(), {"demo": False, "other": True})
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertTrue(host.is_enabled("demo"))
        self.assertNotIn(PLUGIN_ENABLED_CONFIG_KEY, window.config)

    def test_ui_save_failure_does_not_change_baseline_or_running_state(self):
        window, host = self.make_host()
        host.install(CountingPlugin())
        dialog = PluginManagerDialog(host, window)
        dialog.reload()
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        window.save_ok = False
        dialog.save()
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertTrue(host.is_enabled("demo"))
        self.assertEqual(window.config, {})

    def test_ui_success_saves_only_policy_not_credentials_or_business_data(self):
        window, host = self.make_host({"private_history": ["old"], "gemini_api_keys": ["test-only"]})
        plugin = host.install(CountingPlugin())
        host.start_all()
        dialog = PluginManagerDialog(host, window)
        dialog.reload()
        dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        with patch.object(QtWidgets.QMessageBox, "information"):
            dialog.save()
        self.assertEqual(window.config[PLUGIN_ENABLED_CONFIG_KEY], {"demo": False})
        self.assertEqual(window.config["private_history"], ["old"])
        self.assertEqual(window.config["gemini_api_keys"], ["test-only"])
        self.assertTrue(host.is_enabled("demo"))
        self.assertEqual(plugin.calls, ["start"])
        self.assertIn("重启后改变 1 项", dialog.status.text())
        self.assertFalse(dialog.save_button.isEnabled())

    def test_failed_start_visible_and_does_not_block_other_plugin(self):
        window, host = self.make_host()
        bad = host.install(CountingPlugin())
        good = host.install(CountingPlugin("other"))
        with patch.object(bad, "start", side_effect=RuntimeError("example failure")):
            host.start_all()
        entries = host.plugin_entries()
        self.assertEqual(entries[0]["error"], "example failure")
        self.assertEqual(good.calls, ["start"])

    def test_invalid_states_are_rejected_and_mixed_policy_starts_only_enabled(self):
        window, host = self.make_host({PLUGIN_ENABLED_CONFIG_KEY: {"demo": False}})
        disabled = host.install(CountingPlugin())
        enabled = host.install(CountingPlugin("other"))
        host.start_all()
        self.assertEqual(disabled.calls, [])
        self.assertEqual(enabled.calls, ["start"])
        for updates in ({"unknown": False}, {"demo": "false"}):
            with self.assertRaises(ValueError):
                host.save_plugin_states(updates, expected=window.load_config())
        self.assertEqual(window.config[PLUGIN_ENABLED_CONFIG_KEY], {"demo": False})

    def test_real_host_starts_with_all_plugins_disabled_and_preserves_settings(self):
        from app_plugins import builtin
        from PYUI.main_pyui import MainDialog
        from PYUI.main_setting_pyui import MainSettingDialog
        from contextlib import ExitStack

        # Legacy component classes can remain importable without being host
        # plugins. Test the actual assembly rather than the export namespace.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(__import__(MainDialog.__module__, fromlist=["MainDialog"])))
        names = {node.args[0].func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "install" and node.args
                 and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Name)}
        ids = {getattr(builtin, name).plugin_id for name in names
               if getattr(builtin, name) is not None}
        config = {PLUGIN_ENABLED_CONFIG_KEY: dict.fromkeys(ids, False),
                  "waste_reminder": {"enabled": True},
                  "daily_link_history": {"2026-10-01": {"test": "preserved"}},
                  "task_audio_subtitle": {"base_language": "sk"},
                  "subtitle_max_words_per_block": 3,
                  "subtitle_include_line_breaks": True, "subtitle_block_gap_ms": 50}
        with ExitStack() as stack:
            stack.enter_context(patch.object(MainDialog, "load_config", lambda self: deepcopy(config)))
            stack.enter_context(patch.object(MainDialog, "saveCurrentConfig", lambda self, **kwargs: True))
            stack.enter_context(patch.object(MainDialog, "setupNotificationTray", lambda self: None))
            stack.enter_context(patch.object(MainDialog, "registerLoadTaskGlobalHotkey", return_value=True))
            for name in builtin.__all__:
                cls = getattr(builtin, name)
                if cls is not None and hasattr(cls, "start"):
                    stack.enter_context(patch.object(cls, "start", side_effect=AssertionError("disabled start")))
            window = MainDialog()
            try:
                host = window.plugin_host
                self.assertEqual(len(host.plugin_entries()), len(ids))
                self.assertFalse(host._started)
                self.assertFalse(host._start_errors)
                self.assertIsNone(window.waste_reminder_plugin.timer)
                self.assertIsNone(window.waste_reminder_plugin.main_panel)
                self.assertIsNone(window.inventory_plugin.hotkey_manager)
                self.assertIsNone(window.chrome_plugin.dialog)
                self.assertIsNone(window.task_delivery_plugin.controller.hotkey_manager)
                self.assertIsNone(window.cooking_assistant_plugin.timer)
                self.assertEqual(window.subtitle_max_words_spinbox.value(), 3)
                self.assertTrue(window.subtitle_line_break_checkbox.isChecked())
                self.assertEqual(window.subtitle_gap_ms_spinbox.value(), 50)
                self.assertTrue(all(frame.isHidden() and not frame.isEnabled()
                                    for frame in host._main_widget_frames.values()))
                self.assertFalse(host.populate_task_context_menu(QtWidgets.QMenu(window), [0]))
                settings = MainSettingDialog(window, plugin_host=host)
                try:
                    self.assertFalse(host._settings_controllers)
                finally:
                    settings.deleteLater()
                saved = deepcopy(config)
                host.update_runtime_config(saved)
                for key in ("waste_reminder", "daily_link_history", "task_audio_subtitle"):
                    self.assertEqual(saved[key], config[key])
                window.startReviewStatusMonitor()
                self.assertIsNone(window.task_delivery_plugin.controller.review_status_thread)
                from tempfile import TemporaryDirectory
                from pathlib import Path
                import json
                with TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    window.config_name = str(path)
                    dumped = window.dump()
                    for key in ("waste_reminder", "daily_link_history", "task_audio_subtitle",
                                "subtitle_include_line_breaks", "subtitle_max_words_per_block",
                                "subtitle_block_gap_ms"):
                        self.assertEqual(dumped[key], config[key])
            finally:
                window.close()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
