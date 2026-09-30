import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from app_plugins.builtin.task_delivery_inbox import DeliveryInboxDialog, build_delivery_rows
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController


class DeliveryInboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_unrelated_model_setting_does_not_restart_review_monitor(self):
        context = mock.Mock()
        controller = TaskDeliveryController(
            SimpleNamespace(quick_actions=None), context
        )
        with mock.patch.object(controller, "restart_review_status_monitor") as restart:
            self.assertTrue(controller.apply_settings({
                "smart_video_editor": {"whisper_model_size": "large-v3"}
            }))
        restart.assert_not_called()

    def test_unchanged_occupied_hotkey_does_not_open_warning_on_settings_save(self):
        context = mock.Mock()
        controller = TaskDeliveryController(SimpleNamespace(quick_actions=None), context)
        controller.hotkey_manager = mock.Mock(is_registered=False)
        with mock.patch.object(controller, "register_hotkey") as register:
            self.assertTrue(controller.apply_settings({
                "task_result_global_hotkey": controller.global_hotkey,
            }))
        register.assert_not_called()

    def test_new_conflicting_hotkey_is_reported_without_nested_modal(self):
        context = mock.Mock()
        controller = TaskDeliveryController(SimpleNamespace(quick_actions=None), context)
        controller.hotkey_manager = mock.Mock(is_registered=False)
        with mock.patch.object(controller, "register_hotkey", return_value=False) as register:
            self.assertFalse(controller.apply_settings({
                "task_result_global_hotkey": "Ctrl+Alt+F10",
            }))
        register.assert_called_once_with("Ctrl+Alt+F10", show_error=False)

    def test_manual_review_check_refreshes_quantity_even_without_status_transition(self):
        delivery = SimpleNamespace(daily_quantity_dialog=None,
                                   refresh_daily_quantity=mock.Mock())
        context = mock.Mock()
        context.parent_widget = SimpleNamespace(task_delivery_plugin=delivery)
        context.load_config.return_value = {"daily_quantity_sheet_url": "fake-id"}
        controller = TaskDeliveryController(SimpleNamespace(quick_actions=None), context)
        controller._manual_review_quantity_refresh = True
        controller.on_review_status_checked({"all": []})
        controller.on_review_status_checked({"all": []})
        delivery.refresh_daily_quantity.assert_called_once_with()

    def test_joins_exact_drive_file_and_keeps_current_upload(self):
        records = [
            {"logical_key": "video.mp4", "file_name": "video.mp4", "recorded_at": "2026-09-24T12:00:00",
             "drive_file_id": "old", "drive_link": "https://drive.google.com/file/d/old/view",
             "replacement": {"state": "replaced"}},
            {"logical_key": "video.mp4", "file_name": "video.mp4", "recorded_at": "2026-09-25T12:00:00",
             "drive_file_id": "new", "drive_link": "https://drive.google.com/file/d/new/view",
             "batch_date": "2026-09-25", "batch_slot": "1", "task": {"admin": "A"},
             "task_submission": {"status": "failed", "reason": "写入失败"}},
        ]
        reviews = [{"name": "video.mp4", "link": "https://drive.google.com/open?id=new",
                    "status": "passed", "acknowledged_status": "", "submitted_at": 1}]
        history = {"2026-09-25": {"people": {}, "task_sheet_failures": {
            "1": {"video.mp4": {"reason": "写入失败", "saved_at": "2026-09-25T12:01:00"}}}}}
        rows = build_delivery_rows(records, reviews, history)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flags"], {"任务表待核对", "通过待发送"})
        self.assertIn("/new/", rows[0]["link"])

    def test_unmatched_review_not_attached_by_filename(self):
        records = [{"logical_key": "same.mp4", "file_name": "same.mp4",
                    "drive_link": "https://drive.google.com/file/d/one/view"}]
        reviews = [{"name": "same.mp4", "link": "https://drive.google.com/file/d/two/view",
                    "status": "needs_changes", "acknowledged_status": ""}]
        rows = build_delivery_rows(records, reviews, {})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum("审核需修改" in row["flags"] for row in rows), 1)

    def test_dialog_filters_pending(self):
        dialog = DeliveryInboxDialog({}, None)
        dialog.rows = [
            {"time": "2026-09-25", "name": "todo.mp4", "admin": "A", "batch": "1",
             "link": "", "local_file": "", "sheet": "failed", "flags": {"任务表待核对"}, "notes": []},
            {"time": "2026-09-24", "name": "done.mp4", "admin": "B", "batch": "1",
             "link": "", "local_file": "", "sheet": "confirmed", "flags": set(), "notes": []},
        ]
        dialog.render()
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        dialog.filter_box.setCurrentText("全部")
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        dialog.search.setText("todo")
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
