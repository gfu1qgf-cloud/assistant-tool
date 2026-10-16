"""Resizable host panes must not clip plugin controls or touch business data."""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtTest import QTest
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.host import PluginHost
from PYUI.main_pyui import MainDialog
from PYUI.main_workspace_layout import MAIN_SPLITTERS_CONFIG_KEY


class MainWorkspaceLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.old_font = self.app.font()
        self.app.setFont(QtGui.QFont("Microsoft YaHei", 10))
        self.config = {"run_upload": False, "untouched": {"inventory": ["keep"], "keys": ["synthetic"]}}
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.patches = ExitStack()
        for method in ("setupNotificationTray", "startGoogleSheetMonitor", "startFlowParameterGuard",
                       "registerLoadTaskGlobalHotkey", "saveCurrentConfig"):
            self.patches.enter_context(mock.patch.object(MainDialog, method, return_value=True))
        self.patches.enter_context(mock.patch.object(PluginHost, "start_all"))
        self.window = MainDialog()
        self.window.resize(1100, 850)
        self.window.show()
        self.settle()

    def settle(self):
        for _ in range(4):
            self.app.processEvents()

    def tearDown(self):
        self.window.config_reload.timer.stop()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.settle()
        self.patches.close()
        self.app.setFont(self.old_font)
        os.chdir(self.cwd)
        self.temp.cleanup()

    def assert_no_horizontal_clipping(self):
        scroll = self.window.plugin_main_scroll
        self.assertGreaterEqual(scroll.viewport().width(), self.window.plugin_main_frame.minimumSizeHint().width())
        self.assertEqual(scroll.viewport().width(), self.window.plugin_main_frame.width())
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        for frame in self.window.plugin_host._main_widget_frames.values():
            if not frame.isVisible():
                continue
            for control in frame.findChildren(QtWidgets.QWidget):
                if not control.isVisible() or not isinstance(control, (
                    QtWidgets.QPushButton, QtWidgets.QToolButton, QtWidgets.QCheckBox,
                    QtWidgets.QSpinBox, QtWidgets.QLineEdit,
                )):
                    continue
                point = control.mapTo(frame, QtCore.QPoint())
                self.assertGreaterEqual(point.x(), 0, control.objectName())
                self.assertLessEqual(point.x() + control.width(), frame.width(), control.objectName())

    def test_reuses_task_table_directory_tabs_and_log(self):
        w = self.window
        self.assertEqual(w.main_splitter.count(), 2)
        self.assertIs(w.main_splitter.widget(1), w.tabWidget)
        self.assertIs(w.task_directory_splitter.widget(0), w.task_table_widget)
        self.assertIs(w.task_directory_splitter.widget(1), w.file_explorer_tree_view)
        self.assertIs(w.log_splitter.widget(0), w.main_splitter)
        self.assertEqual(w.log_splitter.orientation(), QtCore.Qt.Orientation.Vertical)
        self.assertIs(w.log_text_edit.parentWidget(), w.log_splitter.widget(1))
        self.assertEqual(w.task_table_widget.columnCount(), 5)

    def assert_workspace_fills_window(self):
        self.settle()
        w = self.window
        host = w.workspace_layout.host
        margins = w.gridLayout.contentsMargins()
        self.assertEqual(host.x(), margins.left())
        self.assertEqual(host.width(), w.width() - margins.left() - margins.right())
        self.assertLessEqual(w.task_status_label.y() - host.geometry().bottom(), 10)
        self.assertEqual(w.height() - w.task_status_label.geometry().bottom(), margins.bottom() + 1)
        child = w.workspace_layout.host_layout.itemAt(0).widget()
        self.assertEqual(child.geometry(), host.contentsRect())
        self.assertIs(child, w.log_splitter)
        self.assertEqual(w.log_splitter.geometry(), host.contentsRect())
        log = w.log_text_edit
        bottom = log.mapTo(w, QtCore.QPoint(0, log.height())).y()
        self.assertEqual(bottom, host.y() + host.height())

    def test_root_expanding_slot_survives_opening_node_workspace(self):
        root = self.window.gridLayout
        slot = root.itemAtPosition(1, 0)
        splitter = self.window.log_splitter
        self.window.node_workspace.show()
        self.settle()
        self.assertIs(root.itemAtPosition(1, 0), slot)
        self.assertIs(slot.widget(), self.window.workspace_layout.host)
        self.assertIs(self.window.workspace_layout.host_layout.itemAt(0).widget(), splitter)
        self.assertTrue(self.window.node_workspace.frame.isWindow())
        self.assertIsNot(self.window.node_workspace.frame.centralWidget(), splitter)
        self.assert_workspace_fills_window()

    def test_closed_node_panel_does_not_leave_blank_after_resizing(self):
        controller = self.window.node_workspace
        controller.show()
        self.settle()
        for width, height in ((1600, 1700), (1100, 800), (1920, 1080)):
            controller.frame.close()
            self.window.resize(width, height)
            QTest.qWait(250)
            self.assert_workspace_fills_window()
            controller.show()
            self.assert_workspace_fills_window()
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_attached_detached_and_hidden_panel_keep_workspace_expanding(self):
        controller = self.window.node_workspace
        controller.show()
        self.settle()
        for attached in (True, False, True, False):
            controller.frame.set_attached(attached)
            self.window.resize(1400, 1400)
            QTest.qWait(50)
            self.assert_workspace_fills_window()
            controller.frame.hide()
            self.window.resize(1250, 950)
            self.assert_workspace_fills_window()
            controller.frame.show()
            self.assert_workspace_fills_window()

    def test_maximize_fullscreen_and_restore_after_opening_nodes(self):
        controller = self.window.node_workspace
        controller.show()
        self.settle()
        controller.frame.close()
        for show in (self.window.showMaximized, self.window.showFullScreen):
            show()
            QTest.qWait(50)
            self.assert_workspace_fills_window()
            self.window.showNormal()
            self.window.resize(1400, 1200)
            self.assert_workspace_fills_window()
        self.assertNotIn(MAIN_SPLITTERS_CONFIG_KEY, self.window.dump())

    def test_mouse_drag_changes_ratio_and_remembers_only_layout(self):
        splitter = self.window.main_splitter
        before = splitter.sizes()
        handle = splitter.handle(1)
        start = handle.rect().center()
        QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(handle, start - QtCore.QPoint(90, 0))
        QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=start - QtCore.QPoint(90, 0))
        self.settle()
        self.assertGreater(splitter.sizes()[1], before[1])
        dumped = self.window.dump()
        self.assertEqual(dumped[MAIN_SPLITTERS_CONFIG_KEY]["workspace"], splitter.sizes())
        self.assertEqual(dumped["untouched"], self.config["untouched"])
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_dragging_to_smallest_right_pane_does_not_clip_controls(self):
        for width in (900, 1100, 1450):
            self.window.resize(width, 850)
            self.window.main_splitter.setSizes([10000, 1])
            self.settle()
            self.assert_no_horizontal_clipping()
            self.assertGreaterEqual(self.window.tabWidget.width(), self.window.tabWidget.minimumWidth())

    def test_both_main_panes_cannot_be_collapsed(self):
        splitter = self.window.main_splitter
        for sizes in ([0, 10000], [10000, 0]):
            splitter.setSizes(sizes)
            self.settle()
            self.assertTrue(all(size > 100 for size in splitter.sizes()))
            self.assert_no_horizontal_clipping()

    def test_expanded_quadrants_scroll_vertically_without_hiding_footer(self):
        self.window.resize(1000, 650)
        self.window.task_delivery_plugin.quick_actions.restore_expanded(True)
        self.settle()
        self.assert_no_horizontal_clipping()
        self.assertGreater(self.window.plugin_main_scroll.verticalScrollBar().maximum(), 0)
        for button in (self.window.setting_btn, self.window.about_btn, self.window.create_id_folder_btn):
            origin = button.mapTo(self.window.common_tools, QtCore.QPoint())
            self.assertTrue(self.window.common_tools.rect().contains(origin))
            self.assertTrue(self.window.common_tools.rect().contains(origin + QtCore.QPoint(button.width()-1, button.height()-1)))

    def test_scrollbar_disappears_when_cards_fit(self):
        self.window.resize(1400, 1400)
        self.window.log_splitter.setSizes([1200, 100])
        self.settle()
        self.assertEqual(self.window.plugin_main_scroll.verticalScrollBar().maximum(), 0)
        self.assertFalse(self.window.plugin_main_scroll.verticalScrollBar().isVisible())
        self.assert_no_horizontal_clipping()

    def test_future_plugin_width_changes_update_tab_and_splitter_constraint(self):
        extra = QtWidgets.QWidget(self.window.plugin_main_frame)
        extra.setMinimumWidth(600)
        extra.setMinimumHeight(40)
        self.window.plugin_main_layout.addWidget(extra)
        self.window.main_splitter.setSizes([10000, 1])
        self.settle()
        self.assert_no_horizontal_clipping()
        self.assertGreaterEqual(self.window.tabWidget.minimumWidth(), 620)
        extra.hide()
        self.settle()
        self.assert_no_horizontal_clipping()

    def test_large_ui_font_still_fits_smallest_tools_pane(self):
        self.app.setFont(QtGui.QFont("Microsoft YaHei", 14))
        self.window.resize(1400, 1000)
        self.window.main_splitter.setSizes([10000, 1])
        self.settle()
        self.assert_no_horizontal_clipping()

    def test_directory_toggle_uses_independent_splitter(self):
        self.window.workspace_layout.load({MAIN_SPLITTERS_CONFIG_KEY: {"directory": [300, 1000]}})
        self.window.task_directory_toggle_btn.setChecked(True)
        self.settle()
        self.assertTrue(self.window.file_explorer_tree_view.isVisible())
        self.assertTrue(all(size > 0 for size in self.window.task_directory_splitter.sizes()))
        self.assertGreater(self.window.file_explorer_tree_view.width(), self.window.task_table_widget.width())
        self.window.task_directory_toggle_btn.setChecked(False)
        self.settle()
        self.assertFalse(self.window.file_explorer_tree_view.isVisible())
        self.assert_no_horizontal_clipping()

    def test_saved_ratios_restore_and_invalid_input_is_ignored(self):
        layout = self.window.workspace_layout
        saved = {"workspace": [400, 700], "log": [600, 250], "directory": [500, 220], "future": "keep"}
        layout.load({MAIN_SPLITTERS_CONFIG_KEY: saved})
        self.settle()
        self.assertGreater(self.window.main_splitter.sizes()[1], self.window.main_splitter.sizes()[0])
        result = {"unrelated": ["keep"]}
        layout.update_config(result)
        self.assertEqual(result[MAIN_SPLITTERS_CONFIG_KEY], saved)
        self.assertEqual(result["unrelated"], ["keep"])
        for invalid in (None, [], "bad", {"workspace": [0, 1]}, {"workspace": [True, 5]}, {"log": "bad"}):
            layout.load({MAIN_SPLITTERS_CONFIG_KEY: invalid})
            self.settle()
            self.assert_no_horizontal_clipping()

    def test_opening_and_resizing_does_not_create_layout_setting(self):
        self.window.resize(1250, 900)
        self.settle()
        self.assertNotIn(MAIN_SPLITTERS_CONFIG_KEY, self.window.dump())
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)


if __name__ == "__main__":
    unittest.main()
