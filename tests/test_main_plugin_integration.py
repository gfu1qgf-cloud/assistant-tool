import os
import unittest
from unittest.mock import patch, MagicMock, sentinel

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtTest, QtWidgets

from PYUI.main_pyui import MainDialog
from PYUI.main_setting_pyui import MainSettingDialog
from model.UiInterval import IntervalPrompt


class FakeHotkeyManager(QtCore.QObject):
    activated = QtCore.pyqtSignal()

    def __init__(self, parent=None, hotkey_id=0):
        super().__init__(parent)
        self.sequence = None
        self.last_error = ""

    def register(self, shortcut):
        self.sequence = shortcut
        return True

    def close(self):
        self.sequence = None


class MainPluginIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_background_task_exit_choices_are_cooperative_and_do_not_wait_in_gui(self):
        for choice in (sentinel.wait, sentinel.cancel, sentinel.keep):
            window = MagicMock()
            window._background_exit_pending = False
            window.background_tasks.is_busy.return_value = True
            event = MagicMock()
            with patch("PYUI.main_pyui.QMessageBox") as message_box:
                box = message_box.return_value
                box.addButton.side_effect = [sentinel.wait, sentinel.cancel, sentinel.keep]
                box.clickedButton.return_value = choice
                MainDialog.closeEvent(window, event)
            event.ignore.assert_called_once()
            window.background_tasks.wait.assert_not_called()
            if choice == sentinel.keep:
                self.assertFalse(window._background_exit_pending)
                window.background_tasks.stop_accepting.assert_not_called()
            else:
                self.assertTrue(window._background_exit_pending)
                window.background_tasks.stop_accepting.assert_called_once()
            if choice == sentinel.cancel:
                window.background_tasks.cancel_all.assert_called_once()
            else:
                window.background_tasks.cancel_all.assert_not_called()

    @unittest.skipIf(
        os.environ.get("GITHUB_ACTIONS") == "true",
        "requires an interactive Windows Qt desktop; covered by the local release suite",
    )
    def test_inventory_is_reached_from_plugin_menu_and_settings_page(self):
        with patch("PYUI.main_pyui.GlobalHotkeyManager", FakeHotkeyManager), patch(
            "app_plugins.builtin.inventory.GlobalHotkeyManager",
            FakeHotkeyManager,
        ), patch(
            "app_plugins.builtin.chrome_launcher.GlobalHotkeyManager",
            FakeHotkeyManager,
        ), patch(
            "app_plugins.builtin.task_delivery_controller.GlobalHotkeyManager",
            FakeHotkeyManager,
        ), patch(
            "app_plugins.builtin.chrome_launcher.ChromeRunnerDialog.load_profiles",
            lambda self: None,
        ), patch(
            "app_plugins.builtin.waste_reminder.plugin.WasteReminderPlugin.start",
            lambda self: None,
        ), patch.object(MainDialog, "setupNotificationTray", lambda self: None), patch.object(
            MainDialog,
            "saveCurrentConfig",
            lambda self: True,
        ), patch.object(
            MainDialog, "startFlowParameterGuard", lambda self: None,
        ), patch.object(
            MainDialog, "startGoogleSheetMonitor", lambda self: None,
        ):
            window = MainDialog()
            try:
                background_index = window.tabWidget.indexOf(window.background_tasks_page)
                self.assertEqual(background_index, window.tabWidget.indexOf(window.tab_2) + 1)
                self.assertEqual(window.tabWidget.tabText(background_index), "后台任务")
                self.assertIs(window.background_tasks, window.plugin_host.background_tasks)
                menu_titles = [action.text() for action in window.main_menu_bar.actions()]
                tool_titles = [action.text() for action in window.tools_menu.actions()]
                plugin_titles = [action.text() for action in window.plugin_host.menu.actions()]
                self.assertIn("插件", menu_titles)
                self.assertIn("任务提交表自查", tool_titles)
                self.assertIn("批量切分音频…", tool_titles)
                self.assertIn("库存与素材管理器", plugin_titles)
                self.assertNotIn("素材整理…", plugin_titles)
                self.assertNotIn("图片/视频素材分类…", plugin_titles)
                plugin_ids = {entry["plugin_id"] for entry in window.plugin_host.plugin_entries()}
                self.assertNotIn("material_organizer", plugin_ids)
                self.assertNotIn("image_classifier", plugin_ids)
                self.assertIs(window.material_organizer_plugin, window.inventory_plugin.material_tools.organizer)
                self.assertIs(window.image_classifier_plugin, window.inventory_plugin.material_tools.classifier)
                self.assertIn("智能搜索…", plugin_titles)
                self.assertNotIn("智能搜图…", plugin_titles)
                self.assertNotIn("智能搜音乐…", plugin_titles)
                self.assertIn("smart_search", plugin_ids)
                self.assertNotIn("smart_image_search", plugin_ids)
                self.assertNotIn("smart_music_search", plugin_ids)
                self.assertIn("批量文案视频…", plugin_titles)
                self.assertIn("智能文案分段助手…", plugin_titles)
                self.assertIn("监听剪贴板中的 Google 链接", plugin_titles)
                self.assertIn("Chrome 与 Flow 助手…", plugin_titles)
                self.assertNotIn("Chrome 启动器", plugin_titles)
                self.assertNotIn('chrome_launcher', plugin_ids)
                self.assertNotIn("启动下一个 Chrome", plugin_titles)
                self.assertIn("Codex 多账号切换…", plugin_titles)
                self.assertIsNotNone(window.antigravity_account_switcher_plugin,
                    "已接入的 Antigravity 插件缺失不能作为成功的集成测试")
                self.assertIn("Antigravity 多账号切换…", plugin_titles)
                self.assertIn("antigravity_account_switcher", plugin_ids)
                self.assertIsNone(window.antigravity_account_switcher_plugin.store)
                self.assertNotIn("Flow 视频助手…", plugin_titles)
                self.assertIn("flow_assistant", plugin_ids)
                self.assertIn("智能剪辑", plugin_titles)
                self.assertNotIn("智能剪辑并生成 SRT…", plugin_titles)
                self.assertNotIn("剪辑气口…", plugin_titles)
                self.assertIn("每日任务管理", plugin_titles)
                self.assertIn("任务交付", plugin_titles)
                delivery_action = next(
                    action
                    for action in window.plugin_host.menu.actions()
                    if action.text() == "任务交付"
                )
                self.assertEqual(
                    [action.text() for action in delivery_action.menu().actions()],
                    ["整理任务结果", "查看每日链接", "审核提醒", "交付待办…", "简易上传…", "每日数量统计…"],
                )
                self.assertIsNotNone(window.task_delivery_plugin.quick_actions)
                self.assertIsNotNone(window.task_audio_subtitle_plugin.quick_actions)
                self.assertIsNotNone(window.chrome_plugin.quick_actions)
                self.assertIsNotNone(window.music_ducker_plugin.quick_actions)
                self.assertEqual(len(window.plugin_host.main_widget_order()), 5)
                self.assertIn("waste_reminder.tonight", window.plugin_host.main_widget_order())
                self.assertTrue(all(
                    window.plugin_host.main_widget_frame(*full_id.split(".", 1))
                    is not None
                    for full_id in window.plugin_host.main_widget_order()
                ))
                self.assertEqual(len({
                    window.plugin_host.main_widget_frame(*full_id.split(".", 1))
                    .card_colors()[0].name()
                    for full_id in window.plugin_host.main_widget_order()
                }), 5)
                self.assertIs(
                    window.gen_audio_btn,
                    window.task_audio_subtitle_plugin.quick_actions.audio_button,
                )
                self.assertIs(
                    window.open_chrome_btn,
                    window.chrome_plugin.quick_actions.open_button,
                )
                self.assertIs(
                    window.music_ducker_checkbox,
                    window.music_ducker_plugin.quick_actions.checkbox,
                )
                self.assertIs(
                    window.tidy_task_result_btn,
                    window.task_delivery_plugin.quick_actions.organize_button,
                )
                self.assertIs(
                    window.daily_links_btn,
                    window.task_delivery_plugin.quick_actions.daily_links_button,
                )
                self.assertIs(
                    window.review_status_btn,
                    window.task_delivery_plugin.quick_actions.review_status_button,
                )
                self.assertFalse(hasattr(window, "inventory_manager_btn"))
                self.assertFalse(hasattr(window, "split_audio_btn"))
                self.assertFalse(hasattr(window, "split_len_sbox"))
                self.assertFalse(hasattr(window, "smart_video_editor_thread"))
                self.assertIsNone(window.material_organizer_plugin.store)
                self.assertIsNone(window.smart_image_search_plugin.dialog)
                self.assertIsNone(window.smart_music_search_plugin.dialog)
                self.assertIsNotNone(window.chrome_plugin.dialog)
                self.assertIsNotNone(window.audio_splitter_plugin.dialog)

                task_menu = QtWidgets.QMenu(window)
                task_actions = window.plugin_host.populate_task_context_menu(
                    task_menu,
                    [0],
                )
                self.assertIn(
                    "切分任务音频…",
                    [action.text() for action in task_actions],
                )
                self.assertIn(
                    "智能剪辑并生成 SRT…",
                    [action.text() for action in task_actions],
                )
                self.assertIn(
                    "剪辑气口…",
                    [action.text() for action in task_actions],
                )
                task_action_titles = [action.text() for action in task_actions]
                self.assertIn("智能文案分段…", task_action_titles)
                self.assertIn("生成任务音频【不带字幕】", task_action_titles)
                self.assertIn("生成任务音频【带字幕】", task_action_titles)
                self.assertIn("生成任务音频【使用任务名】", task_action_titles)
                self.assertIn("生成任务字幕", task_action_titles)
                self.assertIn("生成任务字幕【不使用默认配置】", task_action_titles)
                submenu_titles = [
                    action.menu().title()
                    for action in task_menu.actions()
                    if action.menu() is not None
                ]
                self.assertIn("音频与字幕", submenu_titles)

                with patch.object(window, "_showAuxNotice") as notice:
                    for _index in range(10):
                        QtTest.QTest.mouseClick(
                            window.about_btn,
                            QtCore.Qt.RightButton,
                        )
                    self.app.processEvents()
                    notice.assert_called_once_with()

                prompt = IntervalPrompt(window, seconds=1)
                with patch.object(QtWidgets.QMessageBox, "information") as information:
                    prompt._start()
                    QtTest.QTest.qWait(1050)
                    self.assertEqual(prompt.result(), QtWidgets.QDialog.Accepted)
                    information.assert_called_once()

                settings = MainSettingDialog(window, plugin_host=window.plugin_host)
                try:
                    tab_titles = [
                        settings.settingTabWidget.tabText(index)
                        for index in range(settings.settingTabWidget.count())
                    ]
                    self.assertIn("库存插件", tab_titles)
                    self.assertIn("素材整理", tab_titles)
                    self.assertIn("智能剪辑", tab_titles)
                    self.assertIn("Chrome启动与快捷键", tab_titles)
                    self.assertIn("切分音频插件", tab_titles)
                    self.assertIn("音频与字幕插件", tab_titles)
                    self.assertNotIn("生成音频设置", tab_titles)
                    self.assertNotIn("切分音频设置", tab_titles)
                    self.assertFalse(hasattr(settings, "inventory_manager_hotkey_edit"))
                    self.assertTrue(settings.chrome_hotkey_edit.isHidden())
                finally:
                    settings.deleteLater()
            finally:
                window.close()
                self.app.processEvents()
                # Destroy the test-owned window while config writes are still
                # mocked; suite-wide Qt cleanup must not close it a second time
                # after those mocks have gone out of scope.
                window.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
