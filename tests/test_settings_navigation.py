import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from app_plugins.api import PluginSettingsPage
from app_plugins.host import PluginHost
from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.settings_navigation import SettingsNavigation


class FakeMain(QtWidgets.QWidget):
    def load_config(self):
        return {}

    def appendLog(self, *args, **kwargs):
        pass


class FuturePlugin:
    plugin_id = "future_plugin"
    display_name = "未来插件"
    required_api_version = 1

    def register(self, context):
        context.register_settings_page(PluginSettingsPage("first", "未来插件 · 一", QtWidgets.QWidget))
        context.register_settings_page(PluginSettingsPage("second", "未来插件 · 二", QtWidgets.QWidget))


class SettingsNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.old_cwd = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        os.chdir(self.old_cwd)
        self.directory.cleanup()

    def dialog(self, host=None):
        dialog = MainSettingDialog(plugin_host=host)
        self.windows.append(dialog)
        return dialog

    def test_core_settings_are_separate_and_original_page_widgets_retained(self):
        dialog = self.dialog()
        nav = dialog.settings_navigation
        self.assertEqual(nav.program_group.text(0), "程序设置")
        self.assertEqual(nav.plugin_group.text(0), "插件设置")
        self.assertTrue(nav.plugin_group.isHidden())
        self.assertTrue(dialog.settingTabWidget.tabBar().isHidden())
        self.assertEqual(dialog.settingTabWidget.indexOf(dialog.tab_2), -1)
        self.assertGreaterEqual(dialog.settingTabWidget.indexOf(dialog.gemini_keys_editor), 0)
        self.assertGreaterEqual(dialog.settingTabWidget.indexOf(dialog.tab_4), 0)

    def test_new_plugin_pages_automatically_group_without_core_edits(self):
        main = FakeMain()
        self.windows.append(main)
        host = PluginHost(main)
        host.install(FuturePlugin())
        dialog = self.dialog(host)
        nav = dialog.settings_navigation
        group = nav.plugin_group.child(0)
        self.assertEqual(group.text(0), "未来插件")
        self.assertEqual(group.childCount(), 2)
        for index in range(2):
            item = group.child(index)
            page_index = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            widget = dialog.settingTabWidget.widget(page_index)
            self.assertEqual(widget.property("settings_plugin_id"), "future_plugin")
            self.assertTrue(widget.property("settings_page_id").startswith("future_plugin."))
            nav.tree.setCurrentItem(item)
            self.assertIs(dialog.settingTabWidget.currentWidget(), widget)
            self.assertIn("插件设置 / 未来插件", nav.heading.text())

    def test_search_filters_by_page_and_plugin_name(self):
        main = FakeMain()
        self.windows.append(main)
        host = PluginHost(main)
        host.install(FuturePlugin())
        dialog = self.dialog(host)
        nav = dialog.settings_navigation
        nav.search.setText("未来插件")
        visible = [item for item in nav.items.values() if not item.isHidden()]
        self.assertEqual(len(visible), 2)
        self.assertTrue(nav.program_group.isHidden())
        nav.search.setText("绝不存在")
        self.assertTrue(nav.empty_label.isVisibleTo(nav))
        nav.search.clear()
        self.assertFalse(nav.program_group.isHidden())

    def test_validation_focus_request_clears_filter_and_selects_correct_item(self):
        dialog = self.dialog()
        nav = dialog.settings_navigation
        nav.search.setText("外观")
        index = dialog.settingTabWidget.indexOf(dialog.task_result_tab)
        self.assertTrue(nav.items[index].isHidden())
        dialog.settingTabWidget.setCurrentWidget(dialog.task_result_tab)
        self.assertEqual(nav.search.text(), "")
        self.assertIs(nav.tree.currentItem(), nav.items[index])
        self.assertFalse(nav.items[index].isHidden())

    def test_navigation_does_not_write_or_change_configuration(self):
        original = {"run_upload": False, "elevenlabs_api_keys": ["synthetic-key"],
                    "gemini_api_keys": ["synthetic-gemini"], "keep_business": {"id": 123}}
        Path("config.json").write_text(json.dumps(original), encoding="utf-8")
        dialog = self.dialog()
        before = dialog.get_config()
        for item in dialog.settings_navigation.items.values():
            dialog.settings_navigation.tree.setCurrentItem(item)
        self.assertEqual(dialog.get_config(), before)
        self.assertEqual(json.loads(Path("config.json").read_text()), original)
        dialog.ok_btn.click()
        self.app.processEvents()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        saved = json.loads(Path("config.json").read_text())
        self.assertEqual(saved["elevenlabs_api_keys"], original["elevenlabs_api_keys"])
        self.assertEqual(saved["gemini_api_keys"], original["gemini_api_keys"])
        self.assertEqual(saved["keep_business"], original["keep_business"])

    def test_delivery_legacy_form_is_classified_as_plugin_settings(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        main = FakeMain()
        self.windows.append(main)
        host = PluginHost(main)
        host.install(TaskDeliveryPlugin())
        dialog = self.dialog(host)
        index = dialog.settingTabWidget.indexOf(dialog.task_result_tab)
        item = dialog.settings_navigation.items[index]
        self.assertIs(item.parent(), dialog.settings_navigation.plugin_group)
        self.assertEqual(dialog.task_result_tab.property("settings_plugin_id"), "task_delivery")

    def test_cancel_preserves_config(self):
        original = {"run_upload": False, "elevenlabs_api_keys": ["synthetic-existing"]}
        Path("config.json").write_text(json.dumps(original), encoding="utf-8")
        dialog = self.dialog()
        dialog.elevenlabs_api_key_edit.setText("synthetic-unsaved")
        dialog.cancel_btn.click()
        self.assertEqual(json.loads(Path("config.json").read_text()), original)

    def test_program_heading_visible_on_first_show(self):
        dialog = self.dialog()
        dialog.show()
        self.app.processEvents()
        nav = dialog.settings_navigation
        self.assertEqual(nav.tree.verticalScrollBar().value(), 0)
        self.assertGreaterEqual(nav.tree.visualItemRect(nav.program_group).top(), 0)
        self.assertGreaterEqual(nav.splitter.sizes()[0], 170)


if __name__ == "__main__":
    unittest.main()
