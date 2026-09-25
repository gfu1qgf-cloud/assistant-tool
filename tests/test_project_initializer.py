import tempfile
import unittest
from pathlib import Path

from odf import teletype
from odf.opendocument import OpenDocumentSpreadsheet, load
from odf.table import Table, TableCell, TableRow
from odf.text import P

from model.ProjectInitializer import initialize_project_directory
from model.TaskTableAugment import add_daily_stat_headers_to_new_copy


class ProjectInitializerTests(unittest.TestCase):
    def test_new_project_only_appends_daily_statistics_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            template = root / "template.ods"
            document = OpenDocumentSpreadsheet()
            sheet = Table(name="工作表1")
            document.spreadsheet.addElement(sheet)
            header = TableRow()
            for title in ("类型", "任务类型", "子类别"):
                cell = TableCell()
                cell.addElement(P(text=title))
                header.addElement(cell)
            header.addElement(TableCell(numbercolumnsrepeated=4))
            sheet.addElement(header)
            document.save(str(template))
            original_bytes = template.read_bytes()

            result = initialize_project_directory(
                root / "0918", "tasks.ods", [template], add_daily_stat_headers=True,
            )
            self.assertTrue(result["copied"])
            self.assertEqual(template.read_bytes(), original_bytes)
            copied = load(str(root / "0918" / "tasks.ods"))
            cells = copied.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow)[0].getElementsByType(TableCell)
            self.assertEqual(
                [teletype.extractText(cell) for cell in cells[:5]],
                ["类型", "任务类型", "子类别", "每日统计分页", "每日统计类别"],
            )
            copied_bytes = (root / "0918" / "tasks.ods").read_bytes()
            self.assertFalse(add_daily_stat_headers_to_new_copy(root / "0918" / "tasks.ods"))
            self.assertEqual((root / "0918" / "tasks.ods").read_bytes(), copied_bytes)

            (root / "0918" / "tasks.ods").write_bytes(b"existing-user-table")
            second = initialize_project_directory(
                root / "0918", "tasks.ods", [template], add_daily_stat_headers=True,
            )
            self.assertFalse(second["copied"])
            self.assertEqual((root / "0918" / "tasks.ods").read_bytes(), b"existing-user-table")

    def test_copies_template_once_and_never_overwrites_existing_table(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            template = root / "template.ods"
            template.write_bytes(b"template-v1")
            project = root / "0918"

            first = initialize_project_directory(
                project,
                "tasks.ods",
                [template],
            )
            self.assertTrue(first["copied"])
            self.assertEqual((project / "tasks.ods").read_bytes(), b"template-v1")
            self.assertTrue((project / "result").is_dir())

            (project / "tasks.ods").write_bytes(b"user-data")
            template.write_bytes(b"template-v2")
            second = initialize_project_directory(
                project,
                "tasks.ods",
                [template],
            )
            self.assertFalse(second["copied"])
            self.assertEqual((project / "tasks.ods").read_bytes(), b"user-data")

    def test_creates_project_but_reports_missing_template(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "0918"
            result = initialize_project_directory(
                project,
                "tasks.ods",
                [Path(temp) / "missing.ods"],
            )
            self.assertTrue(result["missing_template"])
            self.assertTrue(project.is_dir())
            self.assertFalse((project / "tasks.ods").exists())


if __name__ == "__main__":
    unittest.main()
