import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets

from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog


class DailyQuantityDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_review_status_refresh_keeps_unsaved_classification(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([{
                "id": "review-1", "drive_file_id": "drive-1", "file_name": "a.mp4",
                "batch_date": "2026-09-26", "batch_slot": "02", "included": True,
                "review_path": True, "review_status": "pending",
            }])
            dialog.external_table.cellWidget(0, 4).setCurrentIndex(1)
            with patch("app_plugins.builtin.task_delivery_daily_quantity.read_review_history",
                       return_value={"items": {"google:drive-1": {"status": "passed"}}}):
                dialog.refresh_review_statuses()
            self.assertIn("已通过", dialog.external_table.item(0, 7).text())
            self.assertEqual(dialog.external_edits()[0]["batch_slot"], "01")
        finally:
            dialog.close()

    def test_pending_review_list_is_visible_and_can_request_check(self):
        dialog = DailyQuantityDialog()
        try:
            requests = []
            dialog.review_check_requested.connect(lambda: requests.append(True))
            dialog.show_pending_reviews([{
                "file_name": "a.mp4", "batch_date": "2026-09-26", "batch_slot": "02",
                "status": "needs_changes", "drive_link": "https://drive.google.com/file/d/a/view",
            }])
            self.assertEqual(dialog.review_queue.rowCount(), 1)
            self.assertIn("待审核未计数：1", dialog.review_queue_toggle.text())
            self.assertEqual(dialog.tabs.tabText(dialog.review_tab_index), "待审核 · 1")
            self.assertEqual(dialog.review_queue.item(0, 2).text(), "需修改")
            dialog.review_queue_toggle.click()
            self.assertEqual(dialog.tabs.currentIndex(), dialog.review_tab_index)
            dialog.review_check_button.click()
            self.assertEqual(requests, [True])
        finally:
            dialog.close()

    def test_delivery_date_cannot_scroll_or_select_future_year(self):
        from unittest.mock import Mock
        dialog = DailyQuantityDialog()
        try:
            day = dialog.folder_day.date()
            event = Mock()
            dialog.folder_day.wheelEvent(event)
            event.ignore.assert_called_once()
            self.assertEqual(dialog.folder_day.date(), day)
            dialog.folder_day.setDate(QtCore.QDate.currentDate().addYears(1))
            self.assertLessEqual(dialog.folder_day.date(), QtCore.QDate.currentDate())
        finally:
            dialog.close()

    def test_zero_updates_with_warning_never_claims_sheet_is_current(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_result({'counted': 81, 'warnings': ['类别匹配到 0 行'], 'updated': [], 'daily_counts': []})
            self.assertIn('不能据此判断', dialog.status.text())
            self.assertNotIn('现有表格已是最新', dialog.details.toPlainText())
            self.assertIn('部分数量可能未写入', dialog.details.toPlainText())
        finally:
            dialog.close()

    def test_sheet_link_is_visible_and_change_button_opens_settings(self):
        dialog = DailyQuantityDialog()
        try:
            requests = []
            dialog.edit_sheet_requested.connect(lambda: requests.append(True))
            dialog.set_sheet_url("https://docs.google.com/spreadsheets/d/test/edit")
            self.assertEqual(
                dialog.sheet_url.text(),
                "https://docs.google.com/spreadsheets/d/test/edit",
            )
            self.assertTrue(dialog.sheet_url.isReadOnly())
            dialog.edit_sheet_button.click()
            self.assertEqual(requests, [True])
            dialog.show_category_options({"旧分页": ["旧类别"]})
            dialog.set_sheet_url("https://docs.google.com/spreadsheets/d/another/edit")
            self.assertEqual(dialog._category_options, {"旧分页": ["旧类别"]})
            dialog.show_category_options({"口播": ["短口播"]})
            self.assertEqual(dialog.bulk_sheet.findData("口播"), 1)
        finally:
            dialog.close()

    def test_summary_shows_selected_day_and_overall_total(self):
        dialog = DailyQuantityDialog()
        try:
            self.assertEqual(dialog.tabs.tabText(0), "数量概览")
            self.assertEqual(dialog.tabs.tabText(dialog.video_tab_index), "视频清单")
            dialog.folder_day.setDate(QtCore.QDate(2026, 9, 26))
            dialog.show_result({
                "counted": 7, "warnings": [], "category_options": {},
                "updated": [{"range": "'口播视频组'!DL29", "count": 0}],
                "daily_counts": [
                    {"date": "2026-09-26", "sheet": "口播视频组",
                     "category": "短口播", "01": 2, "02": 1, "03": 0, "total": 3},
                    {"date": "2026-09-26", "sheet": "效果视频组",
                     "category": "特效", "01": 0, "02": 0, "03": 1, "total": 1},
                    {"date": "2026-09-25", "sheet": "口播视频组",
                     "category": "短口播", "01": 0, "02": 0, "03": 3, "total": 3},
                ],
            })
            self.assertIn("合计 4 个", dialog.summary_heading.text())
            self.assertEqual(dialog.metric_values["total"].text(), "4")
            self.assertEqual(dialog.metric_values["01"].text(), "2")
            self.assertIn("全部日期总合计 7 个", dialog.summary_heading.text())
            self.assertEqual(dialog.summary_table.item(2, 5).text(), "4")
            self.assertIn("不是今日视频总数", dialog.details.toPlainText())
            dialog.folder_day.setDate(QtCore.QDate(2026, 9, 25))
            self.assertIn("合计 3 个", dialog.summary_heading.text())
            self.assertEqual(dialog.summary_table.item(1, 5).text(), "3")
        finally:
            dialog.close()

    def test_refresh_result_never_overwrites_fixed_catalog_or_unsaved_choices(self):
        dialog = DailyQuantityDialog()
        try:
            options = {"口播": ["短口播", "长口播"]}
            dialog.show_category_options(options)
            dialog.bulk_sheet.setCurrentIndex(dialog.bulk_sheet.findData("口播"))
            dialog.bulk_category.setCurrentIndex(dialog.bulk_category.findData("长口播"))
            for remote in ({}, {"未知分页": ["线上类别"]}):
                dialog.show_result({"category_options": remote, "warnings": [], "updated": [], "counted": 0})
                self.assertEqual(dialog._category_options, options)
                self.assertEqual(dialog.bulk_category.currentData(), "长口播")
            dialog.show_category_options({**options, "新增": ["静态"]})
            self.assertEqual(dialog.bulk_category.currentData(), "长口播")
            requests = []
            dialog.edit_categories_requested.connect(lambda: requests.append(True))
            dialog.edit_categories_button.click()
            self.assertEqual(requests, [True])
        finally:
            dialog.close()

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
            slot = dialog.external_table.cellWidget(0, 4)
            self.assertEqual(slot.currentData(), "03")
            self.assertNotIn("时段待确认", dialog.external_table.item(0, 7).text())
            self.assertTrue(slot.isEnabled())
            self.assertEqual(dialog.changed_external_edits(), [])
            dialog.external_table.cellWidget(0, 5).setCurrentIndex(0)
            self.assertEqual(dialog.changed_external_edits(), [])
            slot.setCurrentIndex(slot.findData("02"))
            self.assertEqual(dialog.changed_external_edits()[0]["batch_slot"], "02")
            self.assertTrue(dialog.external_edits()[0]["included"])
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

    def test_bulk_count_actions_default_missing_period_to_last(self):
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
            self.assertTrue(dialog.external_edits()[1]["included"])
            self.assertEqual(dialog.external_edits()[1]["batch_slot"], "03")
            self.assertNotIn("未勾选", dialog.folder_status.text())
            menu = dialog._external_context_menu([0])
            count_menu = next(action.menu() for action in menu.actions()
                              if action.text().startswith("计入每日数量"))
            count_menu.actions()[2].trigger()
            self.assertFalse(dialog.external_edits()[0]["included"])
        finally:
            dialog.close()

    def test_view_saved_day_previews_counts_and_slot_change_updates_total(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.folder_day.setDate(QtCore.QDate(2026, 9, 25))
            dialog.show_external_records([
                {"id": "a", "drive_file_id": "a", "file_name": "a.mp4",
                 "batch_date": "2026-09-25", "batch_slot": "01", "included": True,
                 "sheet": "口播", "category": "短"},
                {"id": "b", "drive_file_id": "b", "file_name": "b.mp4",
                 "batch_date": "2026-09-25", "batch_slot": "02", "included": True,
                 "sheet": "口播", "category": "短"},
                {"id": "c", "drive_file_id": "c", "file_name": "c.mp4",
                 "batch_date": "2026-09-25", "batch_slot": "", "included": False,
                 "sheet": "口播", "category": "短"},
            ], day="2026-09-25")
            self.assertIn("合计 2 个", dialog.summary_heading.text())
            self.assertIn("已存清单 3 条", dialog.summary_heading.text())
            self.assertIn("缺时段 0", dialog.summary_heading.text())
            slot = dialog.external_table.cellWidget(2, 4)
            slot.setCurrentIndex(slot.findData("01"))
            slot.setCurrentIndex(slot.findData("03"))
            self.assertIn("合计 3 个", dialog.summary_heading.text())
            self.assertEqual(dialog.summary_table.item(1, 5).text(), "3")
        finally:
            dialog.close()

    def test_bulk_slot_menu_assigns_missing_period_but_keeps_folder_period(self):
        dialog = DailyQuantityDialog()
        try:
            dialog.show_external_records([
                {"id": "a", "file_name": "a.mp4", "batch_date": "2026-09-26",
                 "batch_slot": "", "daily_scan_date": "2026-09-26", "included": False},
                {"id": "b", "file_name": "b.mp4", "batch_date": "2026-09-26",
                 "batch_slot": "02", "detected_batch_slot": "02",
                 "daily_scan_date": "2026-09-26", "included": True},
            ])
            menu = dialog._external_context_menu([0, 1])
            slot_menu = next(action.menu() for action in menu.actions()
                             if action.text().startswith("设置交付时段"))
            slot_menu.actions()[0].trigger()
            edits = dialog.external_edits()
            self.assertEqual(edits[0]["batch_slot"], "01")
            self.assertTrue(edits[0]["included"])
            self.assertEqual(edits[1]["batch_slot"], "02")
            self.assertIn("由网盘目录确定", dialog.folder_status.text())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
