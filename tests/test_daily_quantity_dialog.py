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

    def test_unmodified_no_slot_video_does_not_block_day_switch(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([{
                "id": "outside-slot", "file_name": "outside.mp4",
                "batch_date": "2026-09-26", "batch_slot": "00",
                "daily_scan_date": "2026-09-26", "included": False,
            }])
            self.assertEqual(dialog.external_table.item(0, 4).text(), "")
            self.assertIn("时段待确认", dialog.external_table.item(0, 7).text())
            self.assertEqual(dialog.changed_external_edits(), [])
            dialog.external_table.cellWidget(0, 5).setCurrentIndex(0)
            self.assertEqual(dialog.changed_external_edits(), [])
            dialog.external_table.item(0, 4).setText("02")
            self.assertEqual(dialog.changed_external_edits()[0]["batch_slot"], "02")
        finally:
            dialog.close()

    def test_context_menu_copies_file_names_and_links(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([
                {"id": "one", "file_name": "one.mp4", "relative_path": "01/one.mp4",
                 "drive_link": "https://drive.google.com/file/d/one/view"},
                {"id": "two", "file_name": "two.mp4", "relative_path": "01/two.mp4",
                 "drive_link": "https://drive.google.com/file/d/two/view"},
            ])
            menu = dialog._external_context_menu([0, 1])
            self.assertIn("复制文件名", [action.text() for action in menu.actions()])
            self.assertIn("复制网盘链接", [action.text() for action in menu.actions()])
            menu.actions()[0].trigger()
            self.assertEqual(
                self.app.clipboard().text(), "one.mp4\ntwo.mp4"
            )
            menu.actions()[1].trigger()
            self.assertEqual(
                self.app.clipboard().text(),
                "https://drive.google.com/file/d/one/view\n"
                "https://drive.google.com/file/d/two/view",
            )
        finally:
            dialog.close()

    def test_bulk_count_actions_skip_videos_without_period(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([
                {"id": "one", "file_name": "one.mp4", "batch_slot": "01",
                 "batch_date": "2026-09-26", "included": False},
                {"id": "two", "file_name": "two.mp4", "batch_slot": "",
                 "batch_date": "2026-09-26", "included": False},
            ])
            dialog._set_external_inclusion([0, 1], "check")
            self.assertTrue(dialog.external_edits()[0]["included"])
            self.assertFalse(dialog.external_edits()[1]["included"])
            self.assertIn("缺少有效时段", dialog.folder_status.text())
            menu = dialog._external_context_menu([0])
            count_menu = next(action.menu() for action in menu.actions()
                              if action.menu() is not None)
            count_menu.actions()[2].trigger()
            self.assertFalse(dialog.external_edits()[0]["included"])
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
