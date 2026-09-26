import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog


class DailyQuantityDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_row_dropdowns_follow_sheet_and_save_selected_values(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([{
                "id": "video-1", "file_name": "video.mp4", "included": True,
                "batch_date": "2026-09-26", "batch_slot": "01",
            }])
            dialog.set_category_options({"短视频": ["口播", "剧情"], "图片": ["静态"]})
            sheet = dialog.external_table.cellWidget(0, 5)
            category = dialog.external_table.cellWidget(0, 6)
            self.assertIsInstance(sheet, QtWidgets.QComboBox)
            self.assertIsInstance(category, QtWidgets.QComboBox)
            self.assertFalse(sheet.isEditable())
            sheet.setCurrentIndex(sheet.findData("短视频"))
            self.assertGreaterEqual(category.findData("口播"), 0)
            self.assertEqual(category.findData("静态"), -1)
            category.setCurrentIndex(category.findData("口播"))
            self.assertEqual(dialog.external_edits()[0]["sheet"], "短视频")
            self.assertEqual(dialog.external_edits()[0]["category"], "口播")
            sheet.setCurrentIndex(sheet.findData("图片"))
            self.assertEqual(category.currentData(), "")
            self.assertGreaterEqual(category.findData("静态"), 0)
        finally:
            dialog.close()

    def test_saved_unknown_classification_is_not_silently_lost(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([{
                "id": "video-2", "file_name": "video.mp4",
                "sheet": "旧分页", "category": "旧类别",
            }])
            dialog.set_category_options({"短视频": ["口播"]})
            self.assertEqual(dialog.external_edits()[0]["sheet"], "旧分页")
            self.assertEqual(dialog.external_edits()[0]["category"], "旧类别")
            self.assertIn("未找到", dialog.external_table.cellWidget(0, 5).currentText())
        finally:
            dialog.close()

    def test_bulk_dropdown_applies_to_selected_rows(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.set_category_options({"短视频": ["口播"]})
            dialog.show_external_records([
                {"id": "one", "file_name": "one.mp4"},
                {"id": "two", "file_name": "two.mp4"},
            ])
            dialog.bulk_sheet.setCurrentIndex(dialog.bulk_sheet.findData("短视频"))
            dialog.bulk_category.setCurrentIndex(dialog.bulk_category.findData("口播"))
            dialog.external_table.selectRow(0)
            dialog._apply_bulk_category()
            edits = dialog.external_edits()
            self.assertEqual((edits[0]["sheet"], edits[0]["category"]), ("短视频", "口播"))
            self.assertEqual((edits[1]["sheet"], edits[1]["category"]), ("", ""))
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
