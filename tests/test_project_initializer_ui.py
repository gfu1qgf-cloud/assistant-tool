import ast
import inspect
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P
from qt_compat import QtCore, QtWidgets
from QTUI.main_ui import Ui_MainDialog
from PYUI.main_pyui import MainDialog
from model.TaskTableSchema import normalize_task_table_schema

TEST_SCHEMA = normalize_task_table_schema({"header_row": 1, "fields": {"task_id": {"aliases": ["任务编号"]}}})


class InitializerWindow(QtWidgets.QDialog, Ui_MainDialog):
    initializeProjectDirectory = MainDialog.initializeProjectDirectory
    getTodayDir = MainDialog.getTodayDir

    def __init__(self, root):
        super().__init__()
        self.setupUi(self)
        self.task_path_edit.setText(str(root))
        self.dateEdit.setDate(QtCore.QDate(2026, 10, 8))
        self.config = {"task_table_file_name": "登记表.ods"}
        self.errors = []
        self.logs = []
        self.file_explorer_tree_view = Mock()
        MainDialog.setupProjectInitializer(self)

    def load_config(self):
        return self.config.copy()

    def Critical(self, text):
        self.errors.append(text)

    def appendLog(self, text, **kwargs):
        self.logs.append(text)


class ProjectInitializerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = InitializerWindow(self.root)
        document = OpenDocumentSpreadsheet()
        table = Table(name="工作表1")
        row = TableRow()
        cell = TableCell()
        cell.addElement(P(text="任务编号"))
        row.addElement(cell)
        table.addElement(row)
        document.spreadsheet.addElement(table)
        document.save(str(self.root / "任务登记表格.ods"))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def test_real_constructor_registers_initializer_once(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(MainDialog.__init__)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)]
        self.assertEqual(sum(node.func.attr == "setupProjectInitializer" for node in calls), 1)
        self.assertEqual(sum(node.func.attr == "initializeProjectDirectory" for node in calls), 0)

    def test_actual_button_creates_selected_date_copies_template_and_keeps_existing_data(self):
        with patch("PYUI.main_pyui.APP_ROOT", self.root), patch(
            "PYUI.main_pyui.load_task_table_schema", return_value=TEST_SCHEMA
        ), patch("PYUI.main_pyui.QMessageBox.information") as information:
            self.window.create_id_folder_btn.click()
            table = self.root / "1008" / "登记表.ods"
            self.assertTrue(table.is_file())
            self.assertTrue((table.parent / "result").is_dir())
            self.assertIn(str(table), information.call_args.args[2])
            self.assertIn("已复制模板", information.call_args.args[2])
            table.write_bytes(b"user-data-must-survive")
            self.window.create_id_folder_btn.click()
            self.assertEqual(table.read_bytes(), b"user-data-must-survive")
            self.assertIn("保留原内容", information.call_args.args[2])
        self.assertFalse(self.window.errors)
        self.window.file_explorer_tree_view.load_directory.assert_called_with(self.root / "1008")
        self.assertIn("目标日期", self.window.create_id_folder_btn.toolTip())

    def test_button_uses_internal_template_in_frozen_layout(self):
        bundle = self.root / "_internal"
        bundle.mkdir()
        (self.root / "任务登记表格.ods").rename(bundle / "任务登记表格.ods")
        with patch("PYUI.main_pyui.APP_ROOT", self.root), patch(
            "PYUI.main_pyui.sys._MEIPASS", str(bundle), create=True
        ), patch("PYUI.main_pyui.load_task_table_schema", return_value=TEST_SCHEMA), patch(
            "PYUI.main_pyui.QMessageBox.information"
        ):
            self.window.create_id_folder_btn.click()
        self.assertTrue((self.root / "1008" / "登记表.ods").is_file())
        self.assertFalse(self.window.errors)

    def test_missing_template_lists_actual_search_and_destination_paths(self):
        (self.root / "任务登记表格.ods").unlink()
        with patch("PYUI.main_pyui.APP_ROOT", self.root), patch(
            "PYUI.main_pyui.load_task_table_schema", return_value=TEST_SCHEMA
        ), patch("PYUI.main_pyui.QMessageBox.information") as information:
            self.window.create_id_folder_btn.click()
        information.assert_not_called()
        self.assertIn(str(self.root / "任务登记表格.ods"), self.window.errors[-1])
        self.assertIn(str(self.root / "1008"), self.window.errors[-1])
        self.assertIn("尚未生成登记表", self.window.errors[-1])
        self.assertFalse((self.root / "1008" / "登记表.ods").exists())


if __name__ == "__main__":
    unittest.main()
