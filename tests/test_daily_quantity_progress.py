import copy
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from model.DailyQuantityStats import (
    _scope_key, _sync_progress_summary, daily_quantity_sync_progress, reconcile_daily_quantity,
)
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog


def key(day, category="甲"):
    return json.dumps(["统计", day, "02", category, "本人"], ensure_ascii=False)


def fake_sheet():
    grid = [
        ["", "", "", "", "2026-09-26", "", "", ""],
        ["组别", "名字", "尽本分时间", "定额", "一天总数", "中午12点", "中午18点", "晚上24点"],
        [], ["AI组", "本人"], ["", "", "甲", 50, "=SUM(F5:H5)", "", "", ""],
    ]
    service = MagicMock()
    api = service.spreadsheets.return_value
    api.get.return_value.execute.return_value = {"sheets": [{"properties": {
        "title": "统计", "gridProperties": {"rowCount": len(grid), "columnCount": 8},
    }}]}
    api.values.return_value.batchGet.return_value.execute.side_effect = lambda: {"valueRanges": [{"values": copy.deepcopy(grid)}]}
    def write():
        body = api.values.return_value.batchUpdate.call_args.kwargs["body"]
        for item in body["data"]:
            address = item["range"].split("!")[1]
            row = int("".join(c for c in address if c.isdigit())) - 1
            col = ord(address[0]) - ord("A")
            grid[row][col] = item["values"][0][0]
        return {"totalUpdatedCells": len(body["data"])}
    api.values.return_value.batchUpdate.return_value.execute.side_effect = write
    return service, grid


class QuantityProgressModelTests(unittest.TestCase):
    def test_old_state_shows_latest_committed_date_not_latest_scan(self):
        state = {"cells": {key("2026-09-25"): {"written": 2}, key("2026-09-26"): {"written": 0}},
                 "daily_scans": {"2026-09-27": {}}, "updated_at": "2026-09-27T10:20:30+02:00"}
        p = _sync_progress_summary(state)
        self.assertEqual(p["latest_date"], "2026-09-26")
        self.assertEqual(p["first_date"], "2026-09-25")
        self.assertEqual(len(p["dates"]), 2)
        self.assertIsNone(p["warning_count"])
        self.assertEqual(state["cells"][key("2026-09-25")]["written"], 2)

    def test_malformed_and_future_records_do_not_advance_progress(self):
        future = date.today().replace(year=date.today().year + 1).isoformat()
        p = _sync_progress_summary({"cells": {
            "bad-json": {}, key(future): {"written": 1}, key("2026-09-26"): {},
            key("2026-09-25"): {"written": 1},
        }})
        self.assertEqual(p["latest_date"], "2026-09-25")

    def test_progress_is_scoped_and_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            config = {"daily_quantity_sheet_url": "one-id"}
            scope = _scope_key(config, root)
            state = {scope: {"cells": {key("2026-09-26"): {"written": 2}}, "last_refresh_warning_count": 3},
                     "other": {"cells": {key("2026-10-04"): {"written": 5}}}}
            path.write_text(json.dumps(state), encoding="utf-8")
            before = path.read_bytes()
            p = daily_quantity_sync_progress(config, root, path)
            self.assertEqual(p["latest_date"], "2026-09-26")
            self.assertEqual(p["warning_count"], 3)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(daily_quantity_sync_progress({"daily_quantity_sheet_url": "two-id"}, root, path)["latest_date"], "")

    def test_success_and_zero_update_refresh_persist_progress(self):
        service, grid = fake_sheet()
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        group = {("统计", "2026-09-26", "02", "甲", "本人"): [{"identity": "v1"}]}
        with tempfile.TemporaryDirectory() as root, patch("model.DailyQuantityStats.collect_assignments", return_value=(group, [])):
            path = Path(root) / "state.json"
            first = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            second = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            self.assertEqual(first["sync_progress"]["latest_date"], "2026-09-26")
            self.assertFalse(second["updated"])
            self.assertEqual(second["sync_progress"]["latest_date"], "2026-09-26")
            self.assertEqual(second["sync_progress"]["warning_count"], 0)
            self.assertEqual(grid[4][6], 1)
            saved = daily_quantity_sync_progress(config, root, path)
            self.assertEqual(saved, second["sync_progress"])

    def test_failed_matching_does_not_mark_new_date_as_synced(self):
        service, _ = fake_sheet()
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        groups = {("统计", "2026-09-27", "02", "甲", "本人"): [{"identity": "v1"}]}
        with tempfile.TemporaryDirectory() as root, patch("model.DailyQuantityStats.collect_assignments", return_value=(groups, [])):
            path = Path(root) / "state.json"
            scope = _scope_key(config, root)
            path.write_text(json.dumps({scope: {"cells": {key("2026-09-26"): {"written": 0, "videos": []}}}}), encoding="utf-8")
            result = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            self.assertTrue(result["warnings"])
            self.assertEqual(result["sync_progress"]["latest_date"], "2026-09-26")
            self.assertGreater(result["sync_progress"]["warning_count"], 0)

    def test_failed_batch_write_and_dry_run_do_not_change_stored_progress(self):
        service, _ = fake_sheet()
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        groups = {("统计", "2026-09-26", "02", "甲", "本人"): [{"identity": "v1"}]}
        with tempfile.TemporaryDirectory() as root, patch("model.DailyQuantityStats.collect_assignments", return_value=(groups, [])):
            path = Path(root) / "state.json"
            before = b'{"other": {"keep": true}}'
            path.write_bytes(before)
            preview = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[], dry_run=True)
            self.assertEqual(preview["sync_progress"]["latest_date"], "")
            self.assertEqual(path.read_bytes(), before)
            service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = RuntimeError("network")
            with self.assertRaises(RuntimeError):
                reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            self.assertEqual(path.read_bytes(), before)

    def test_empty_refresh_records_warnings_but_no_false_date(self):
        service, _ = fake_sheet()
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        with tempfile.TemporaryDirectory() as root, patch("model.DailyQuantityStats.collect_assignments", return_value=({}, ["缺分类"])):
            path = Path(root) / "state.json"
            result = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            self.assertEqual(result["sync_progress"]["latest_date"], "")
            self.assertEqual(daily_quantity_sync_progress(config, root, path)["warning_count"], 1)
            self.assertFalse(result["updated"])


class QuantityProgressUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def progress(self):
        return {"latest_date": "2026-09-26", "first_date": "2026-09-25",
                "updated_at": "2026-09-27T10:20:30+02:00", "warning_count": 3,
                "dates": [{"date": "2026-09-25", "cells": 1}, {"date": "2026-09-26", "cells": 2}]}

    def test_progress_stays_visible_when_selected_day_or_tab_changes(self):
        d = DailyQuantityDialog()
        try:
            d.show_sync_progress(self.progress())
            d.folder_day.setDate(QtCore.QDate(2026, 9, 25))
            self.assertIn("统计记录至：2026-09-26", d.sync_progress_heading.text())
            self.assertIn("当前所选 2026-09-25", d.sync_progress_note.text())
            self.assertIn("3 条待处理", d.sync_progress_note.text())
            self.assertIn("不能视为全部完成", d.sync_progress_note.text())
            d.show()
            d.tabs.setCurrentIndex(d.video_tab_index)
            self.assertTrue(d.sync_progress_heading.isVisible())
        finally:
            d.close()

    def test_jump_changes_date_and_loads_stored_list_without_sync_write(self):
        d = DailyQuantityDialog()
        try:
            events, refreshes = [], []
            d.view_date_requested.connect(events.append)
            d.refresh_requested.connect(lambda: refreshes.append(True))
            d.show_sync_progress(self.progress())
            d.latest_date_button.click()
            self.assertEqual(d.folder_day.date(), QtCore.QDate(2026, 9, 26))
            self.assertEqual(events, ["2026-09-26"])
            self.assertFalse(refreshes)
            self.assertEqual(d.tabs.currentIndex(), 0)
        finally:
            d.close()

    def test_refresh_failure_and_local_scan_do_not_advance_progress(self):
        d = DailyQuantityDialog()
        try:
            d.show_sync_progress(self.progress())
            d.show_result({"counted": 2, "warnings": [], "updated": [],
                           "daily_counts": [{"date": "2026-09-27", "total": 2, "01": 2}]})
            self.assertIn("2026-09-26", d.sync_progress_heading.text())
            d.show_error("network")
            self.assertIn("2026-09-26", d.sync_progress_heading.text())
            self.assertIn("此前成功同步", d.sync_progress_note.text())
            d.show_sync_progress({})
            self.assertIn("尚无成功同步记录", d.sync_progress_heading.text())
            self.assertFalse(d.latest_date_button.isEnabled())
        finally:
            d.close()

    def test_pending_review_count_is_visible_and_updates(self):
        d = DailyQuantityDialog()
        try:
            p = self.progress()
            p["warning_count"] = 0
            d.show_sync_progress(p)
            d.show_pending_reviews([{"file_name": "test.mp4", "status": "pending"}])
            self.assertIn("1 条待审核未计数", d.sync_progress_note.text())
            d.show_pending_reviews([])
            self.assertNotIn("条待审核未计数", d.sync_progress_note.text())
        finally:
            d.close()

    def test_open_reused_window_reloads_current_scope_progress(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        parent = QtWidgets.QWidget()
        parent.task_path_edit = QtWidgets.QLineEdit("root", parent)
        plugin = TaskDeliveryPlugin()
        config = {"daily_quantity_sheet_url": "one-id"}
        plugin.context = types.SimpleNamespace(parent_widget=parent, load_config=lambda: dict(config), log=Mock())
        plugin.controller = Mock()
        try:
            with patch("app_plugins.builtin.task_delivery.external_video_records", return_value=[]), \
                 patch("app_plugins.builtin.task_delivery.external_video_sources", return_value=[]), \
                 patch.object(plugin, "refresh_daily_quantity_review_queue"), \
                 patch.object(plugin, "load_daily_quantity_categories"), \
                 patch("app_plugins.builtin.task_delivery.daily_quantity_sync_progress", side_effect=[self.progress(), {}]) as read:
                d = plugin.open_daily_quantity()
                self.assertIn("2026-09-26", d.sync_progress_heading.text())
                d.close()
                config["daily_quantity_sheet_url"] = "other-id"
                self.assertIs(plugin.open_daily_quantity(), d)
                self.assertIn("尚无成功同步记录", d.sync_progress_heading.text())
                self.assertFalse(d.latest_date_button.isEnabled())
                self.assertEqual(read.call_args.args[0]["daily_quantity_sheet_url"], "other-id")
                self.assertEqual(read.call_count, 2)
        finally:
            if plugin.daily_quantity_dialog is not None:
                plugin.daily_quantity_dialog.close()
            parent.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
