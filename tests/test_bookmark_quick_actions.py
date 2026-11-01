"""Bookmark shortcut: no new store, service, import or browser launch."""
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch

from qt_compat import QtCore, QtGui, QtTest, QtWidgets
from app_plugins.builtin.chrome_launcher import ChromeLauncherPlugin, ChromeLauncherQuickActions


class BookmarkQuickActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        if os.environ.get('BOOKMARK_CARD_SCREENSHOT'):
            font_id=QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
            families=QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families: cls.app.setFont(QtGui.QFont(families[0],9))

    def fake_plugin(self,flow=True):
        return SimpleNamespace(open_launcher=Mock(),launch_next_profile=Mock(),open_bookmarks=Mock(),
                               flow_controller=SimpleNamespace(open_flow_queue=Mock()) if flow else None)

    def test_flow_card_has_a_direct_bookmark_action_in_compact_grid(self):
        plugin=self.fake_plugin();panel=ChromeLauncherQuickActions(plugin)
        try:
            self.assertIsInstance(panel.layout(),QtWidgets.QGridLayout)
            self.assertIs(panel.layout().itemAtPosition(1,0).widget(),panel.bookmarks_button)
            self.assertIs(panel.layout().itemAtPosition(1,1).widget(),panel.flow_button)
            panel.bookmarks_button.click()
            plugin.open_bookmarks.assert_called_once_with()
            plugin.open_launcher.assert_not_called();plugin.launch_next_profile.assert_not_called()
            plugin.flow_controller.open_flow_queue.assert_not_called()
            panel.open_button.click();plugin.open_launcher.assert_called_once_with()
            panel.next_button.click();plugin.launch_next_profile.assert_called_once_with()
            panel.flow_button.click();plugin.flow_controller.open_flow_queue.assert_called_once_with()
            destination=os.environ.get('BOOKMARK_CARD_SCREENSHOT')
            if destination:
                panel.resize(390,95);panel.show();self.app.processEvents();panel.grab().save(destination)
        finally:panel.close()

    def test_standalone_chrome_keeps_shortcuts_and_full_width_bookmark_row(self):
        plugin=self.fake_plugin(flow=False);panel=ChromeLauncherQuickActions(plugin)
        try:
            self.assertIs(panel.layout().itemAtPosition(1,0).widget(),panel.bookmarks_button)
            self.assertIs(panel.layout().itemAtPosition(1,1).widget(),panel.bookmarks_button)
            panel.bookmarks_button.click();plugin.open_bookmarks.assert_called_once_with()
        finally:panel.close()

    def test_bookmarks_route_reuses_existing_panel_without_refreshing_flow(self):
        from model.AppDataStorage import ApplicationStorage
        from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
        from PYUI.browser_bookmarks_pyui import BrowserBookmarksPanel
        from PYUI.chrome_runner_pyui import ChromeRunnerDialog
        with tempfile.TemporaryDirectory() as temp:
            storage=ApplicationStorage(Path(temp)/'private')
            parent=QtWidgets.QWidget();plugin=FlowAssistantPlugin()
            context=SimpleNamespace(storage=storage.for_plugin('flow_assistant'),parent_widget=parent,
                register_command=MagicMock(),register_settings_page=MagicMock(),register_main_widget=MagicMock(),
                load_config=lambda:{},log=MagicMock(),current_task_targets=lambda:[])
            plugin.register(context)
            with patch.object(ChromeRunnerDialog,'load_profiles',lambda _self:None):
                manager=ChromeRunnerDialog(config_path=str(Path(temp)/'config.json'),
                    bookmark_context=plugin.browser_launcher.context,
                    language_storage=plugin.browser_launcher.context.storage)
            plugin.browser_launcher.dialog=manager
            plugin.refresh_tasks=Mock()
            panel=plugin.browser_launcher.create_quick_actions(parent)
            try:
                self.assertIsInstance(manager.bookmarks_panel,BrowserBookmarksPanel)
                data=manager.bookmarks_panel.context.storage.file('bookmarks/library.json')
                manager.bookmarks_panel.library.add('示例','https://example.test/bookmark')
                before=data.path.read_bytes()
                panel.bookmarks_button.click();window=plugin.dialog
                self.assertIs(window.sections.currentWidget(),window.chrome_section)
                self.assertIs(manager.environment_tabs.currentWidget(),manager.bookmarks_panel)
                self.assertEqual(manager.bookmarks_panel.table.rowCount(),1)
                self.assertEqual(data.path.read_bytes(),before)
                self.assertEqual(manager.bookmarks_panel.context.storage.path,
                                 storage.for_plugin('chrome_launcher').path)
                self.assertFalse(storage.for_plugin('flow_assistant').file('bookmarks/library.json').exists())
                window.show_flow_page(0)
                panel.bookmarks_button.click()
                self.assertIs(plugin.dialog,window)
                self.assertIs(plugin.browser_launcher.dialog,manager)
                self.assertIs(manager.environment_tabs.currentWidget(),manager.bookmarks_panel)
                plugin.refresh_tasks.assert_not_called()
                self.assertEqual(data.path.read_bytes(),before)
            finally:
                plugin.stop();parent.close()
                if plugin.dialog: plugin.dialog.deleteLater()
                manager.deleteLater();parent.deleteLater()
                self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)


if __name__=='__main__':unittest.main()
