import copy
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from model import DailyQuantityStats as stats
from model.VideoUploadHistory import all_video_upload_records
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog, DailyQuantityInspectionThread


class InspectionModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state.json"
        self.config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人",
                       "task_table_file_name": "tasks.ods"}
        self.scope = stats._scope_key(self.config, self.root)
        self.addCleanup(patch.stopall)
        patch("model.DailyQuantityStats.read_review_history", return_value={"items": {}}).start()
        patch("model.DailyQuantityStats.CategoryStore.load", return_value=({"统计": ["甲", "乙"]}, None)).start()

    def state(self, rows, scans=None):
        data = {self.scope: {"external_videos": rows, "daily_scans": scans or {}, "cells": {"preserve": {"written": 7}}}}
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def row(self, id="a", day="2026-10-01", **fields):
        return {"id": id, "drive_file_id": id, "file_name": id+".mp4", "batch_date": day,
                "batch_slot": "03", "included": True, "sheet": "", "category": "", **fields}

    def inspect(self, records=()):
        return stats.inspect_daily_quantity(self.config, self.root, self.path, records=list(records))

    def upload(self, category="", file_id="u"):
        project = self.root / "1001"
        project.mkdir(exist_ok=True)
        (project/"tasks.ods").touch()
        record = {"source": "upload", "logical_key": "same", "drive_file_id": file_id,
                  "file_name": "a.mp4", "local_file": str(project/"result"/"a.mp4"),
                  "batch_date": "2026-10-01", "batch_slot": "02", "task": {"id": "7", "row": 2}}
        task = types.SimpleNamespace(task_id="7", source_row=2, daily_stat_sheet="统计", daily_stat_category=category)
        report = {"matched_headers": {"daily_stat_sheet": ["分页"], "daily_stat_category": ["类别"]}}
        patch("model.DailyQuantityStats.ReadTaskOds2", return_value=([task], report)).start()
        patch("model.DailyQuantityStats.load_task_table_schema", return_value={}).start()
        return record, task

    def test_group_all_dates_and_read_only(self):
        self.state([self.row(), self.row("b", "2026-10-02"), self.row("c", sheet="统计", category="甲")])
        before = self.path.read_bytes()
        with patch("model.DailyQuantityStats.load_sheets_service") as sheets:
            r = self.inspect()
        self.assertEqual([i["date"] for i in r["dates"]], ["2026-10-02", "2026-10-01"])
        self.assertEqual(len(r["pending"]), 2)
        self.assertEqual(r["checked"], 3)
        self.assertEqual(self.path.read_bytes(), before)
        sheets.assert_not_called()

    def test_ignore_survives_reload_and_restore(self):
        self.state([self.row()])
        stats.set_daily_quantity_ignored(self.config, self.root, ["a"], state_path=self.path)
        r = self.inspect()
        self.assertFalse(r["pending"])
        self.assertEqual(len(r["ignored"]), 1)
        self.assertFalse(stats._included(r["ignored"][0]))
        self.assertEqual(json.loads(self.path.read_text())[self.scope]["cells"]["preserve"]["written"], 7)
        stats.set_daily_quantity_ignored(self.config, self.root, ["a"], False, state_path=self.path)
        r = self.inspect()
        self.assertEqual(len(r["pending"]), 1)
        self.assertTrue(r["pending"][0]["included"])

    def test_ignored_cannot_be_reincluded_by_review_migration_or_editor(self):
        self.state([self.row(review_path=True, daily_scan_date="2026-10-01")])
        stats.set_daily_quantity_ignored(self.config, self.root, ["a"], state_path=self.path)
        stats.update_external_video_records(self.config, self.root, [self.row(included=True, sheet="统计", category="甲")], self.path)
        row = stats.external_video_records(self.config, self.root, self.path)[0]
        self.assertFalse(stats._included(row))
        self.assertTrue(row["quantity_ignored"])
        self.assertEqual(stats.preview_external_day([row], row["batch_date"])["missing_category"], 0)

    def test_same_drive_id_ignored_across_duplicate_imports(self):
        row = self.row(sheet="统计", category="甲")
        other = dict(row, id="another", quantity_ignored=True)
        groups, warnings = stats._external_assignments({"external_videos": [row, other]}, "本人", {})
        self.assertFalse(groups)
        self.assertFalse(warnings)
        self.state([other, row])
        self.assertFalse(self.inspect()["pending"])
        stats.set_daily_quantity_ignored(self.config, self.root, ["a"], False, state_path=self.path)
        self.assertFalse(self.inspect()["ignored"])

    def test_ignore_decreases_previous_program_count_only_on_explicit_refresh(self):
        from tests.test_daily_quantity_progress import fake_sheet
        service, grid = fake_sheet()
        record, _task = self.upload(category="甲")
        record["batch_date"] = "2026-09-26"
        self.state([self.row("u", "2026-09-26", sheet="统计", category="甲")])
        data = json.loads(self.path.read_text())
        data[self.scope]["cells"] = {}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        stats.reconcile_daily_quantity(self.config, self.root, service=service, records=[record], state_path=self.path)
        self.assertEqual(grid[4][6], 1)
        stats.set_daily_quantity_ignored(self.config, self.root, ["u"], state_path=self.path)
        self.assertEqual(grid[4][6], 1)  # Ignore itself is local, no network writes.
        result = stats.reconcile_daily_quantity(self.config, self.root, service=service, records=[record], state_path=self.path)
        self.assertEqual(result["counted"], 0)
        self.assertEqual(grid[4][6], 0)
        self.assertFalse(result["warnings"])

    def test_stale_removed_files_not_reported(self):
        self.state([self.row(missing_from_daily=True), self.row("b", outside_daily_scan=True)])
        self.assertFalse(self.inspect()["pending"])

    def test_invalid_catalog_category_is_listed_without_automatic_changes(self):
        self.state([self.row(sheet="统计", category="过期类别")])
        row = self.inspect()["pending"][0]
        self.assertEqual(row["category"], "过期类别")
        self.assertIn("当前分类列表", row["classification_issue"])

    def test_unknown_ignore_ids_fail_without_writing(self):
        self.state([self.row()])
        before = self.path.read_bytes()
        with patch("model.DailyQuantityStats.all_video_upload_records", return_value=[]):
            with self.assertRaises(ValueError):
                stats.set_daily_quantity_ignored(self.config, self.root, ["missing"], state_path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_state_fails_closed_and_is_never_overwritten(self):
        self.path.write_text('{broken', encoding='utf-8')
        for operation in (lambda: self.inspect(), lambda: stats.set_daily_quantity_ignored(
            self.config, self.root, ['a'], state_path=self.path), lambda: stats.update_external_video_records(
            self.config, self.root, [self.row()], state_path=self.path)):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.path.read_text(), '{broken')

    def test_upload_history_missing_category_included_and_first_date_retained(self):
        record, _task = self.upload()
        r = self.inspect([record, dict(record, batch_date="2026-10-02", drive_file_id="revision")])
        self.assertEqual(len(r["pending"]), 1)
        self.assertEqual(r["pending"][0]["batch_date"], "2026-10-01")
        self.assertEqual(r["pending"][0]["drive_file_id"], "u")

    def test_scanned_day_supersedes_upload_history(self):
        record, _task = self.upload()
        self.state([self.row("physical", sheet="统计", category="甲", daily_scan_date="2026-10-01")],
                   {"2026-10-01": {}})
        r = self.inspect([record])
        self.assertFalse(r["pending"])
        self.assertEqual(r["checked"], 1)

    def test_history_ignore_removes_count_and_warnings(self):
        record, _task = self.upload()
        item_id = self.inspect([record])["pending"][0]["id"]
        with patch("model.DailyQuantityStats.all_video_upload_records", return_value=[record]):
            stats.set_daily_quantity_ignored(self.config, self.root, [item_id], state_path=self.path)
        r = self.inspect([record])
        self.assertFalse(r["pending"])
        self.assertEqual(len(r["ignored"]), 1)
        groups, warnings = stats.collect_assignments(self.config, self.root, [record], ignored_file_ids={"u"})
        self.assertFalse(groups)
        self.assertFalse(warnings)

    def test_history_classification_can_be_saved_and_local_correction_still_wins(self):
        record, task = self.upload()
        row = self.inspect([record])["pending"][0]
        edit = dict(row, sheet="统计", category="甲")
        with patch("model.DailyQuantityStats.all_video_upload_records", return_value=[record]):
            stats.update_external_video_records(self.config, self.root, [edit], self.path)
        r = self.inspect([record])
        self.assertFalse(r["pending"])
        self.assertEqual(r["records"][0]["category"], "甲")
        saved = stats.external_video_records(self.config, self.root, self.path)[0]
        groups, warnings = stats.collect_assignments(self.config, self.root, [record], classification_overrides={"u": saved})
        self.assertEqual(sum(map(len, groups.values())), 1)
        extra, _ = stats._external_assignments({"external_videos": [saved]}, "本人", groups)
        self.assertFalse(extra)
        self.assertFalse(warnings)
        task.daily_stat_category = "乙"
        self.assertEqual(self.inspect([record])["records"][0]["category"], "乙")
        with self.assertRaises(ValueError):
            stats.update_external_video_records(self.config, self.root, [dict(edit, batch_date="2026-10-02")], self.path)

    def test_ignore_survives_drive_folder_rescan(self):
        self.state([self.row(folder_id="folderId123456", relative_path="a.mp4")])
        stats.set_daily_quantity_ignored(self.config, self.root, ["a"], state_path=self.path)
        with patch("model.MaterialDriveSync._execute_with_retry", return_value={"mimeType": "application/vnd.google-apps.folder", "name": "folder"}), \
             patch("model.MaterialDriveSync._collect_remote_files", return_value=[{"id": "a", "name": "a.mp4", "mimeType": "video/mp4"}]):
            stats.scan_external_video_folder(self.config, self.root, "https://drive.google.com/drive/folders/folderId123456", "2026-10-01", "03", service=MagicMock(), state_path=self.path)
        self.assertFalse(self.inspect()["pending"])
        self.assertEqual(len(self.inspect()["ignored"]), 1)

    def test_read_only_upload_history_never_archives_or_saves(self):
        with patch("model.VideoUploadHistory.load_video_upload_history", return_value={"records": []}), \
             patch("model.VideoUploadHistory._migrate_task_submission_audit", return_value=1), \
             patch("model.VideoUploadHistory._archive_expired_records") as archive, \
             patch("model.VideoUploadHistory._save_state") as write, \
             patch("model.VideoUploadHistory._all_archive_records", return_value=[{"id": "old"}]):
            self.assertEqual(all_video_upload_records({}, read_only=True), [{"id": "old"}])
        archive.assert_not_called()
        write.assert_not_called()


class InspectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_date_overview_drills_down_and_has_all_pending(self):
        d = DailyQuantityDialog()
        self.addCleanup(d.close)
        requests = []
        d.view_pending_requested.connect(lambda day, ignored: requests.append((day, ignored)))
        d.show_inspection({"dates": [{"date": "2026-10-01", "pending": 4, "ignored": 2}], "pending": [1]*4, "ignored": [1]*2, "checked": 6})
        d._open_pending_date(0)
        d.all_pending_button.click()
        d.show_ignored.setChecked(True)
        d.all_pending_button.click()
        self.assertEqual(requests, [("2026-10-01", False), ("", False), ("", True)])
        self.assertIn("4", d.inspection_status.text())

    def test_ignore_confirmation_and_recovery_actions(self):
        d = DailyQuantityDialog()
        self.addCleanup(d.close)
        d.show_external_records([{"id": "a", "batch_date": "2026-10-01", "batch_slot": "03", "file_name": "a.mp4"}])
        requests = []
        d.ignore_requested.connect(lambda ids, ignored: requests.append((ids, ignored)))
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
            d._request_ignore([0], True)
        self.assertFalse(requests)
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            d._request_ignore([0], True)
            d._request_ignore([0], False)
        self.assertEqual(requests, [(["a"], True), (["a"], False)])
        labels = [a.text() for a in d._external_context_menu([0]).actions()]
        self.assertIn("恢复所选忽略", labels)

    def test_ignored_checkbox_disabled_but_copy_still_works(self):
        d = DailyQuantityDialog()
        self.addCleanup(d.close)
        d.show_external_records([{"id": "a", "file_name": "a.mp4", "drive_link": "https://drive.google.com/file/d/a/view", "quantity_ignored": True}])
        d._set_external_inclusion([0], "check")
        self.assertFalse(d.external_edits()[0]["included"])
        with patch("app_plugins.builtin.task_delivery_daily_quantity.set_internal_clipboard_text") as copy_text:
            d._copy_external_values([0], "name")
        copy_text.assert_called_once_with("a.mp4")

    def test_open_triggers_inspection_without_cloud_refresh(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        parent = QtWidgets.QWidget()
        parent.task_path_edit = QtWidgets.QLineEdit("root", parent)
        p = TaskDeliveryPlugin()
        p.context = types.SimpleNamespace(parent_widget=parent, load_config=lambda: {"daily_quantity_sheet_url": "fake-id"}, log=MagicMock())
        p.controller = MagicMock()
        with patch("app_plugins.builtin.task_delivery.external_video_records", return_value=[]), \
             patch("app_plugins.builtin.task_delivery.external_video_sources", return_value=[]), \
             patch.object(p, "refresh_daily_quantity_review_queue"), \
             patch.object(p, "load_daily_quantity_categories"), patch.object(p, "_load_daily_quantity_progress"), \
             patch.object(p, "inspect_daily_quantity") as inspect, patch.object(p, "refresh_daily_quantity") as refresh:
            d = p.open_daily_quantity()
            inspect.assert_called_once()
            refresh.assert_not_called()
            self.assertEqual(d.tabs.currentIndex(), d.pending_tab_index)
        d.close()
        parent.deleteLater()

    def test_worker_only_reads_model_and_emits_result(self):
        t = DailyQuantityInspectionThread({}, "root")
        results = []
        t.completed.connect(results.append)
        with patch("app_plugins.builtin.task_delivery_daily_quantity.inspect_daily_quantity", return_value={"checked": 5}) as inspect:
            t.run()
        inspect.assert_called_once_with({}, "root")
        self.assertEqual(results, [{"checked": 5}])

    def test_background_completion_updates_main_thread_and_releases_worker(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        with tempfile.TemporaryDirectory() as root:
            parent = QtWidgets.QWidget()
            parent.task_path_edit = QtWidgets.QLineEdit(root, parent)
            p = TaskDeliveryPlugin()
            p.context = types.SimpleNamespace(parent_widget=parent, load_config=lambda: {"daily_quantity_sheet_url": "fake-id"}, log=MagicMock())
            p.daily_quantity_dialog = DailyQuantityDialog(parent)
            loop = QtCore.QEventLoop()
            main_threads = []
            original = p._daily_quantity_inspection_completed
            def completed(result):
                main_threads.append(QtCore.QThread.currentThread() == self.app.thread())
                original(result)
            p._daily_quantity_inspection_completed = completed
            result = {"records": [], "pending": [], "ignored": [], "dates": [], "checked": 0}
            with patch("app_plugins.builtin.task_delivery_daily_quantity.inspect_daily_quantity", return_value=result):
                self.assertTrue(p.inspect_daily_quantity())
                self.assertFalse(p.daily_quantity_dialog.inspect_button.isEnabled())
                worker = p.daily_quantity_inspection_thread
                worker.finished.connect(loop.quit)
                QtCore.QTimer.singleShot(3000, loop.quit)
                loop.exec()
            self.assertFalse(worker.isRunning())
            self.assertEqual(main_threads, [True])
            self.assertIsNone(p.daily_quantity_inspection_thread)
            self.assertTrue(p.daily_quantity_dialog.inspect_button.isEnabled())
            p.daily_quantity_dialog.close()
            parent.deleteLater()

    def test_date_drill_down_changes_selected_delivery_date(self):
        d = DailyQuantityDialog()
        self.addCleanup(d.close)
        d.show_inspection({"pending": [{"id": "a", "batch_date": "2026-10-01"}]})
        d.show_pending_records("2026-10-01")
        self.assertEqual(d.folder_day.date().toString("yyyy-MM-dd"), "2026-10-01")
        self.assertEqual(d.external_table.rowCount(), 1)


if __name__ == "__main__":
    unittest.main()
