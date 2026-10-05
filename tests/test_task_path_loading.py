"""Task-root picker and actionable missing-workbook diagnostics; no services started."""

import ast
import inspect
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtTest, QtWidgets
from QTUI.main_ui import Ui_MainDialog
from PYUI.main_pyui import MainDialog


class TaskLoadWindow(QtWidgets.QDialog, Ui_MainDialog):
    def __init__(self, root):
        super().__init__()
        self.setupUi(self)
        self.config = {"task_table_file_name": "任务登记表格.ods"}
        self.logs = []
        self.errors = []
        self.task_list = ["previous-task"]
        self.loaded_project_dir = Path(root) / "old"
        self.google_sheet_monitor_thread = Mock()
        self.task_path_edit.setText(str(root))
        self.dateEdit.setDate(QtCore.QDate(2026, 10, 5))
        self.file_explorer_tree_view = Mock()
        self.refreshTaskWidget = Mock()
        self.updateGoogleSheetDownloadTarget = Mock()
        self.startTaskReferenceDownloads = Mock()
        MainDialog.setupTaskPathPicker(self)
        self.task_path_edit.textChanged.connect(self.clearLoadedProject)
        self.dateEdit.dateChanged.connect(self.clearLoadedProject)

    chooseTaskPath = MainDialog.chooseTaskPath
    getTodayDir = MainDialog.getTodayDir
    loadTask = MainDialog.loadTask
    clearLoadedProject = MainDialog.clearLoadedProject

    def load_config(self):
        return dict(self.config)

    def appendLog(self, message, **kwargs):
        self.logs.append((message, kwargs))

    def Critical(self, message):
        self.errors.append(message)


class TaskPathLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "任务"
        self.root.mkdir()
        self.window = TaskLoadWindow(self.root)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def test_real_constructor_registers_picker_after_ui_creation(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(MainDialog.__init__)))
        calls = [node.value.func.attr for node in tree.body[0].body
                 if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and isinstance(node.value.func, ast.Attribute)]
        self.assertEqual(calls.count("setupTaskPathPicker"), 1)
        self.assertLess(calls.index("setupUi"), calls.index("setupTaskPathPicker"))

    def test_click_three_dots_selects_root_and_invalidates_old_loaded_project(self):
        selected = Path(self.temp.name) / "新任务"
        selected.mkdir()
        with patch("PYUI.main_pyui.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(selected)) as chooser:
            QtTest.QTest.mouseClick(self.window.open_task_path_btn, QtCore.Qt.MouseButton.LeftButton)
        chooser.assert_called_once()
        self.assertEqual(Path(chooser.call_args.args[2]), self.root)
        self.assertEqual(Path(self.window.task_path_edit.text()), selected)
        self.assertIsNone(self.window.loaded_project_dir)
        self.window.google_sheet_monitor_thread.set_project_dir.assert_called_once_with(None)
        self.assertIn("根目录", self.window.open_task_path_btn.toolTip())
        self.window.refreshTaskWidget.assert_not_called()
        self.assertEqual(self.window.config, {"task_table_file_name": "任务登记表格.ods"})

    def test_picker_cancel_leaves_path_and_loaded_project_unchanged(self):
        previous = self.window.loaded_project_dir
        with patch("PYUI.main_pyui.QtWidgets.QFileDialog.getExistingDirectory", return_value=""):
            self.window.open_task_path_btn.click()
        self.assertEqual(Path(self.window.task_path_edit.text()), self.root)
        self.assertEqual(self.window.loaded_project_dir, previous)
        self.window.google_sheet_monitor_thread.set_project_dir.assert_not_called()

    def test_picker_missing_current_path_starts_in_existing_parent(self):
        self.window.task_path_edit.setText(str(self.root / "missing"))
        with patch("PYUI.main_pyui.QtWidgets.QFileDialog.getExistingDirectory", return_value="") as chooser:
            self.window.open_task_path_btn.click()
        self.assertEqual(Path(chooser.call_args.args[2]), self.root)

    def test_picker_exception_is_logged_not_propagated(self):
        with patch("PYUI.main_pyui.QtWidgets.QFileDialog.getExistingDirectory", side_effect=OSError("dialog failed")), patch(
            "PYUI.main_pyui.logging.exception"
        ) as logger:
            self.window.open_task_path_btn.click()
        logger.assert_called_once()
        self.assertIn("dialog failed", self.window.errors[-1])
        self.assertIn("dialog failed", self.window.logs[-1][0])

    def test_missing_table_shows_exact_directory_name_and_action_instead_of_only_log(self):
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch("PYUI.main_pyui.ReadTaskOds2") as reader:
            self.window.loadTask()
        warning.assert_called_once()
        message = warning.call_args.args[2]
        self.assertIn(str(self.root / "1005" / "任务登记表格.ods"), message)
        self.assertIn("文件名必须为：任务登记表格.ods", message)
        self.assertIn("目标日期目录", message)
        self.assertIn("创建任务文件夹", message)
        self.assertEqual(self.window.logs[-1][0], message)
        reader.assert_not_called()
        self.assertFalse((self.root / "1005").exists())
        self.assertEqual(self.window.task_list, ["previous-task"])

    def test_table_in_root_is_not_silently_loaded_or_moved(self):
        table = self.root / "任务登记表格.ods"
        table.write_bytes(b"root-workbook")
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch("PYUI.main_pyui.ReadTaskOds2") as reader:
            self.window.loadTask()
        reader.assert_not_called()
        self.assertIn(str(self.root / "1005" / table.name), warning.call_args.args[2])
        self.assertEqual(table.read_bytes(), b"root-workbook")

    def test_selected_date_folder_explains_double_date_and_correct_parent(self):
        date_dir = self.root / "1005"
        date_dir.mkdir()
        self.window.task_path_edit.setText(str(date_dir))
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning:
            self.window.loadTask()
        message = warning.call_args.args[2]
        self.assertIn(str(date_dir / "1005" / "任务登记表格.ods"), message)
        self.assertIn("导致日期重复", message)
        self.assertIn(str(self.root), message)

    def test_directory_named_like_table_is_not_read_as_workbook(self):
        (self.root / "1005" / "任务登记表格.ods").mkdir(parents=True)
        with patch("PYUI.main_pyui.QMessageBox.warning") as warning, patch("PYUI.main_pyui.ReadTaskOds2") as reader:
            self.window.loadTask()
        warning.assert_called_once()
        reader.assert_not_called()

    def test_blank_or_missing_root_is_actionable_and_does_not_create_date_folder(self):
        for root in ("  ", str(self.root / "missing")):
            with self.subTest(root=root):
                self.window.task_path_edit.setText(root)
                with patch("PYUI.main_pyui.ReadTaskOds2") as reader:
                    self.window.loadTask()
                self.assertIn("任务根目录", self.window.errors[-1])
                self.assertIn("…", self.window.errors[-1])
                reader.assert_not_called()
        self.assertFalse((self.root / "missing").exists())

    def test_invalid_configured_filename_cannot_escape_date_directory(self):
        for name in ("../outside.ods", str(self.root / "outside.ods"), ".", "..", "   "):
            with self.subTest(name=name):
                self.window.config["task_table_file_name"] = name
                with patch("PYUI.main_pyui.ReadTaskOds2") as reader:
                    self.window.loadTask()
                self.assertIn("文件名无效", self.window.errors[-1])
                reader.assert_not_called()

    def test_successful_load_uses_trimmed_configured_name_and_existing_pipeline(self):
        project = self.root / "1005"
        project.mkdir()
        table = project / "我的登记表.ods"
        table.write_bytes(b"user-workbook")
        self.window.config["task_table_file_name"] = "  我的登记表.ods  "
        self.window.task_path_edit.setText("  " + str(self.root) + "  ")
        with patch("PYUI.main_pyui.ReadTaskOds2", return_value=(["new-task"], {})) as reader, patch(
            "PYUI.main_pyui.load_task_table_schema", return_value={}
        ), patch("PYUI.main_pyui.format_task_table_report", return_value="report"), patch(
            "PYUI.main_pyui.QMessageBox.warning"
        ) as warning:
            self.window.loadTask()
        self.assertEqual(reader.call_args.args[0], table)
        self.assertEqual(self.window.task_list, ["new-task"])
        self.assertEqual(self.window.loaded_project_dir, project.resolve())
        self.window.refreshTaskWidget.assert_called_once_with()
        self.window.file_explorer_tree_view.load_directory.assert_called_once_with(project)
        self.window.updateGoogleSheetDownloadTarget.assert_called_once_with()
        self.window.startTaskReferenceDownloads.assert_called_once_with(project)
        self.assertTrue((project / "result").is_dir())
        self.assertEqual(table.read_bytes(), b"user-workbook")
        warning.assert_not_called()

    def test_read_failure_includes_source_path_and_preserves_existing_tasks(self):
        project = self.root / "1005"
        project.mkdir()
        table = project / "任务登记表格.ods"
        table.touch()
        with patch("PYUI.main_pyui.ReadTaskOds2", side_effect=ValueError("bad workbook")), patch(
            "PYUI.main_pyui.load_task_table_schema", return_value={}
        ):
            self.window.loadTask()
        self.assertIn(str(table), self.window.errors[-1])
        self.assertIn("bad workbook", self.window.errors[-1])
        self.assertEqual(self.window.task_list, ["previous-task"])
        self.window.startTaskReferenceDownloads.assert_not_called()


if __name__ == "__main__":
    unittest.main()
