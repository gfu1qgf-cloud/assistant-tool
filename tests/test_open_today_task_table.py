import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from PYUI.main_pyui import MainDialog


class TaskTableWindow(QtWidgets.QDialog):
    """Exercise the real shortcut without starting plugins or background services."""

    def __init__(self, root):
        super().__init__()
        self.task_path_edit = QtWidgets.QLineEdit(str(root), self)
        self.dateEdit = QtWidgets.QDateEdit(QtCore.QDate(2026, 9, 17), self)
        self.task_list_header_layout = QtWidgets.QHBoxLayout(self)
        self.task_list_header_layout.addWidget(QtWidgets.QLabel("任务列表", self))
        self.task_list_header_layout.addStretch(1)
        self.config = {}
        self.logs = []
        self.errors = []
        MainDialog.setupTaskTableShortcut(self)

    def getTodayDir(self):
        return MainDialog.getTodayDir(self)

    def openTodayTaskTable(self):
        return MainDialog.openTodayTaskTable(self)

    def load_config(self):
        return dict(self.config)

    def appendLog(self, message, **kwargs):
        self.logs.append((message, kwargs))

    def Critical(self, message):
        self.errors.append(message)


class OpenTodayTaskTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "0917"
        self.project.mkdir()
        self.window = TaskTableWindow(self.root)

    def tearDown(self):
        self.window.close()
        self.temp.cleanup()

    def test_header_button_opens_selected_date_not_computer_date(self):
        table = self.project / "tasks.ods"
        table.write_bytes(b"existing-workbook")
        with patch("PYUI.main_pyui.QtGui.QDesktopServices.openUrl", return_value=True) as opener:
            self.window.open_today_table_btn.click()
        self.assertEqual(self.window.task_list_header_layout.indexOf(self.window.open_today_table_btn), 1)
        self.assertEqual(self.window.open_today_table_btn.text(), "打开今日表格")
        self.assertIn("目标日期", self.window.open_today_table_btn.toolTip())
        self.assertEqual(Path(opener.call_args.args[0].toLocalFile()), table.resolve())
        self.assertEqual(table.read_bytes(), b"existing-workbook")

    def test_custom_file_name_and_changed_target_date_are_read_on_each_click(self):
        self.window.config["task_table_file_name"] = "  我的登记表.ods  "
        self.window.dateEdit.setDate(QtCore.QDate(2026, 10, 2))
        project = self.root / "1002"
        project.mkdir()
        table = project / "我的登记表.ods"
        table.touch()
        with patch("PYUI.main_pyui.QtGui.QDesktopServices.openUrl", return_value=True) as opener:
            self.assertTrue(self.window.openTodayTaskTable())
        self.assertEqual(Path(opener.call_args.args[0].toLocalFile()), table.resolve())

    def test_missing_table_shows_warning_and_does_not_create_or_launch(self):
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch(
            "PYUI.main_pyui.QtGui.QDesktopServices.openUrl"
        ) as opener:
            self.assertFalse(self.window.openTodayTaskTable())
        opener.assert_not_called()
        self.assertIn("初始化项目目录", warning.call_args.args[2])
        self.assertFalse((self.project / "tasks.ods").exists())

    def test_directory_named_like_workbook_is_not_opened(self):
        (self.project / "tasks.ods").mkdir()
        with patch("PYUI.main_pyui.QMessageBox.warning"), patch(
            "PYUI.main_pyui.QtGui.QDesktopServices.openUrl"
        ) as opener:
            self.assertFalse(self.window.openTodayTaskTable())
        opener.assert_not_called()

    def test_invalid_root_uses_existing_error_handling(self):
        self.window.task_path_edit.setText(str(self.root / "missing"))
        with patch("PYUI.main_pyui.QtGui.QDesktopServices.openUrl") as opener:
            self.assertFalse(self.window.openTodayTaskTable())
        opener.assert_not_called()
        self.assertTrue(self.window.errors)

    def test_file_name_cannot_escape_date_directory(self):
        for name in ("../outside.ods", str(self.root / "outside.ods"), ".", "..", "   "):
            with self.subTest(name=name):
                self.window.config["task_table_file_name"] = name
                with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch(
                    "PYUI.main_pyui.QtGui.QDesktopServices.openUrl"
                ) as opener:
                    self.assertFalse(self.window.openTodayTaskTable())
                opener.assert_not_called()
                self.assertIn("文件名无效", warning.call_args.args[2])

    def test_no_file_association_is_reported_without_crashing(self):
        (self.project / "tasks.ods").touch()
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch(
            "PYUI.main_pyui.QtGui.QDesktopServices.openUrl", return_value=False
        ), patch("PYUI.main_pyui.logging.exception"):
            self.assertFalse(self.window.openTodayTaskTable())
        self.assertIn("关联", warning.call_args.args[2])
        self.assertTrue(self.window.logs)

    def test_launch_exception_is_logged_and_shown(self):
        (self.project / "tasks.ods").touch()
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch(
            "PYUI.main_pyui.QtGui.QDesktopServices.openUrl", side_effect=OSError("launch failed")
        ), patch("PYUI.main_pyui.logging.exception"):
            self.assertFalse(self.window.openTodayTaskTable())
        self.assertIn("launch failed", warning.call_args.args[2])
        self.assertIn("launch failed", self.window.logs[-1][0])

    def test_file_access_error_is_reported_without_crashing(self):
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch(
            "PYUI.main_pyui.pathlib.Path.is_file", side_effect=PermissionError("access denied")
        ), patch("PYUI.main_pyui.QtGui.QDesktopServices.openUrl") as opener, patch(
            "PYUI.main_pyui.logging.exception"
        ):
            self.assertFalse(self.window.openTodayTaskTable())
        opener.assert_not_called()
        self.assertIn("access denied", warning.call_args.args[2])

    def test_config_read_error_is_reported_without_crashing(self):
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch.object(
            self.window, "load_config", side_effect=ValueError("invalid config")
        ), patch("PYUI.main_pyui.QtGui.QDesktopServices.openUrl") as opener, patch(
            "PYUI.main_pyui.logging.exception"
        ):
            self.assertFalse(self.window.openTodayTaskTable())
        opener.assert_not_called()
        self.assertIn("invalid config", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
