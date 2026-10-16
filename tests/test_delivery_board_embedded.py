import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.task_delivery import BOARD_EXPANDED_CONFIG_KEY, TaskDeliveryPlugin
from app_plugins.builtin.task_delivery_board import DeliveryBoardWidget
from model.DeliveryTodoStore import DeliveryTodoStore, collect_delivery_todos


class EmbeddedDeliveryBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = {"untouched": "keep"}
        self.window = QtWidgets.QWidget()
        self.window.resize(520, 600)
        self.plugin = TaskDeliveryPlugin()
        self.plugin.todo_store = DeliveryTodoStore(Path(self.directory.name) / "todos.sqlite3")
        self.saved = []

        def patch_config(updates, *, expected):
            self.assertEqual(expected, self.config)
            self.saved.append(dict(updates))
            self.config.update(updates)
            return True
        self.plugin.context = SimpleNamespace(
            load_config=lambda: dict(self.config), patch_config=patch_config,
            parent_widget=self.window, log=mock.Mock(),
        )
        self.plugin.controller = SimpleNamespace(daily_link_history={})
        self.history_patch = mock.patch("app_plugins.builtin.task_delivery.load_video_upload_history",
                                        return_value={"records": []})
        self.review_patch = mock.patch("app_plugins.builtin.task_delivery.review_history_snapshot",
                                       return_value={"all": []})
        self.history_patch.start()
        self.review_patch.start()
        self.layout = QtWidgets.QVBoxLayout(self.window)
        self.panel = self.plugin.create_quick_actions(self.window)
        self.layout.addWidget(self.panel)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.history_patch.stop()
        self.review_patch.stop()
        self.directory.cleanup()

    def expand(self):
        self.panel.todo_toggle.click()
        self.app.processEvents()
        return self.panel.board_widget

    def test_lazy_expand_and_collapse_reuses_same_widget_and_preserves_search(self):
        self.assertIsNone(self.panel.board_widget)
        board = self.expand()
        self.assertIsInstance(board, DeliveryBoardWidget)
        self.assertFalse(isinstance(board, QtWidgets.QDialog))
        board.search.setText("管理员")
        self.panel.todo_toggle.click()
        self.assertFalse(board.isVisible())
        self.assertEqual(self.panel.todo_toggle.arrowType(), QtCore.Qt.ArrowType.RightArrow)
        self.assertIs(self.expand(), board)
        self.assertEqual(board.search.text(), "管理员")
        self.assertEqual(self.saved, [{BOARD_EXPANDED_CONFIG_KEY: True},
                                      {BOARD_EXPANDED_CONFIG_KEY: False},
                                      {BOARD_EXPANDED_CONFIG_KEY: True}])
        self.assertEqual(self.config["untouched"], "keep")

    def test_restore_expansion_does_not_write_config(self):
        self.panel.restore_expanded(True)
        self.assertIsNotNone(self.panel.board_widget)
        self.assertTrue(self.panel.todo_toggle.isChecked())
        self.assertEqual(self.saved, [])

    def test_inline_completion_refreshes_full_window_and_badge(self):
        task_id = self.plugin.todo_store.add_manual("发给管理员", quadrant=0)
        board = self.expand()
        dialog = self.plugin.open_delivery_inbox()
        self.app.processEvents()
        self.assertEqual(dialog.lists[0].count(), 1)
        board.lists[0].item(0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.app.processEvents()
        self.assertEqual(board.lists[0].count(), 0)
        self.assertEqual(dialog.lists[0].count(), 0)
        self.assertEqual(dialog.completed_tree.topLevelItemCount(), 1)
        self.assertIn("0 项", self.panel.todo_toggle.text())
        self.assertEqual(self.plugin.todo_store.list_tasks("completed")[0]["id"], task_id)

    def test_full_window_changes_refresh_inline_and_collapsed_badge(self):
        task_id = self.plugin.todo_store.add_manual("发送文件", quadrant=0)
        board = self.expand()
        dialog = self.plugin.open_delivery_inbox()
        dialog.move_task(task_id, 2)
        self.assertEqual(board.lists[0].count(), 0)
        self.assertEqual(board.lists[2].count(), 1)
        self.panel.todo_toggle.click()
        dialog._complete_id(task_id)
        self.assertIn("0 项", self.panel.todo_toggle.text())
        self.assertFalse(board.isVisible())
        dialog.tabs.setCurrentWidget(dialog.completed_tree)
        dialog.completed_tree.setCurrentItem(dialog.completed_tree.topLevelItem(0))
        dialog.restore_selected()
        self.assertIn("1 项", self.panel.todo_toggle.text())
        self.assertEqual(self.expand().lists[2].count(), 1)

    def test_local_source_sync_updates_collapsed_badge_without_auto_expansion(self):
        with mock.patch("app_plugins.builtin.task_delivery.collect_delivery_todos", return_value=[{
            "id": "new", "kind": "manual", "title": "新任务", "quadrant": 1,
        }]), mock.patch("app_plugins.builtin.task_delivery_board.load_drive_service") as network:
            self.plugin.sync_delivery_todos()
            self.assertIn("1 项", self.panel.todo_toggle.text())
            self.assertIsNone(self.panel.board_widget)
            self.assertEqual(self.expand().lists[1].count(), 1)
            network.assert_not_called()

    def test_same_database_completion_persists_without_deleting_task(self):
        task_id = self.plugin.todo_store.add_manual("完成后保留", quadrant=3)
        board = self.expand()
        board._complete_id(task_id)
        reopened = DeliveryTodoStore(Path(self.directory.name) / "todos.sqlite3")
        self.assertEqual(len(reopened.list_tasks("open")), 0)
        self.assertEqual(reopened.list_tasks("completed")[0]["id"], task_id)

    def test_compact_height_and_all_quadrants_visible_at_narrow_width(self):
        for quadrant in range(4):
            self.plugin.todo_store.add_manual("任务 " * 30, quadrant=quadrant)
        board = self.expand()
        self.window.resize(450, 550)
        self.app.processEvents()
        self.assertEqual(board.height(), 360)
        self.assertTrue(all(widget.isVisible() for widget in board.lists))
        self.assertTrue(all(widget.count() == 1 for widget in board.lists))
        self.assertTrue(all(widget.width() > 80 for widget in board.lists))
        self.assertFalse(board.tabs.tabBar().isVisible())
        self.assertFalse(board.isWindow())

    def test_inline_ctrl_c_preserves_links(self):
        self.plugin.todo_store.add_sources([{"id": "linked", "kind": "send", "title": "文件",
            "link": "https://drive.google.com/file/d/synthetic/view", "quadrant": 0}])
        board = self.expand()
        listing = board.lists[0]
        listing.setCurrentItem(listing.item(0))
        listing.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier))
        self.assertEqual(self.app.clipboard().text(), "https://drive.google.com/file/d/synthetic/view")

    def test_rerender_preserves_selection(self):
        for index in range(15):
            self.plugin.todo_store.add_manual(f"条目 {index}", quadrant=0)
        board = self.expand()
        listing = board.lists[0]
        listing.setCurrentItem(listing.item(8))
        selected_id = listing.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)
        board.render()
        self.assertEqual(listing.currentItem().data(QtCore.Qt.ItemDataRole.UserRole), selected_id)

    def test_failed_save_does_not_destroy_open_board(self):
        self.plugin.context.patch_config = mock.Mock(return_value=False)
        board = self.expand()
        self.assertTrue(board.isVisible())
        self.plugin.context.log.assert_called()

    def test_repeated_toggle_creates_only_one_board(self):
        board = self.expand()
        for _ in range(30):
            self.panel.todo_toggle.click()
            self.assertIs(self.expand(), board)
        self.assertEqual(len(self.panel.findChildren(DeliveryBoardWidget)), 1)

    def test_group_cards_keep_administrator_date_and_video_count_visible(self):
        records = [{"event_id": "synthetic", "file_name": "A.mp4", "drive_file_id": "a",
                    "drive_link": "https://drive.google.com/file/d/a/view", "batch_date": "2026-10-08",
                    "batch_slot": "1", "task": {"admin": "张管理员"}, "review_routed": False}]
        self.plugin.todo_store.add_sources(collect_delivery_todos(records, [], {}))
        board = self.expand()
        item = next(widget.item(0) for widget in board.lists if widget.count())
        self.assertTrue(item.text().startswith("张管理员"))
        self.assertIn("10-08", item.text())
        self.assertIn("待发送 1 个", item.text())

    def test_factory_restores_saved_state_without_saving_again(self):
        self.config[BOARD_EXPANDED_CONFIG_KEY] = True
        second = self.plugin.create_quick_actions(self.window)
        self.layout.addWidget(second)
        self.app.processEvents()
        self.assertTrue(second.todo_toggle.isChecked())
        self.assertIsNotNone(second.board_widget)
        self.assertEqual(self.saved, [])

    def test_existing_group_gains_video_without_new_card_and_refreshes_both_views(self):
        def source(items):
            return {"id": "same-group", "kind": "send", "title": "2026-10-08 · 发送 · 管理员",
                    "admin": "管理员", "quadrant": 0, "items": items}
        items = [{"id": "first", "name": "A.mp4"}]
        with mock.patch("app_plugins.builtin.task_delivery.collect_delivery_todos",
                        side_effect=lambda *_args, **_kwargs: [source(items)]):
            self.plugin.sync_delivery_todos()
            board = self.expand()
            dialog = self.plugin.open_delivery_inbox()
            items.append({"id": "second", "name": "B.mp4"})
            self.assertEqual(self.plugin.sync_delivery_todos(), 0)
        self.assertEqual(board.lists[0].count(), 1)
        self.assertIn("2 个", board.lists[0].item(0).text())
        self.assertIn("2 个", dialog.lists[0].item(0).toolTip())

    def test_main_window_scrolls_plugin_cards_without_pushing_out_footer(self):
        from PYUI.main_pyui import MainDialog
        from app_plugins.host import PluginHost
        with mock.patch.object(PluginHost, "start_all"), mock.patch.object(MainDialog, "setupNotificationTray"), \
             mock.patch.object(MainDialog, "startGoogleSheetMonitor"), mock.patch.object(MainDialog, "startFlowParameterGuard"), \
             mock.patch.object(MainDialog, "registerLoadTaskGlobalHotkey", return_value=True), \
             mock.patch.object(MainDialog, "saveCurrentConfig", return_value=True):
            window = MainDialog()
            try:
                window.task_delivery_plugin.todo_store = self.plugin.todo_store
                window.resize(1000, 650)
                window.show()
                window.task_delivery_plugin.quick_actions.restore_expanded(True)
                self.app.processEvents()
                self.assertIs(window.plugin_main_scroll.widget(), window.plugin_main_frame)
                self.assertGreater(window.plugin_main_scroll.verticalScrollBar().maximum(), 0)
                position = window.setting_btn.mapTo(window.common_tools, QtCore.QPoint())
                self.assertTrue(window.common_tools.rect().contains(position))
                self.assertEqual(window.plugin_main_scroll.horizontalScrollBar().maximum(), 0)
            finally:
                window.config_reload.timer.stop()
                window.deleteLater()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
