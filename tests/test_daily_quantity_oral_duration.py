import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from model.DailyQuantityStats import (
    _adjust_fl_oral_counts, _duration_fields, _valid_video_duration,
    collect_assignments, external_video_records, preview_external_day, reconcile_daily_quantity,
    scan_external_video_folder,
)


DAY = "2026-10-01"
SHORT = ("统计", DAY, "02", "FL 短口播", "本人")
LONG = ("统计", DAY, "02", "FL 长口播", "本人")
OPTIONS = {"统计": ["FL 短口播", "FL 长口播", "其他类别"]}


def video(file_id="video-id", duration=None, version=""):
    return dict(drive_file_id=file_id, file_name="example.mp4",
                duration_millis=duration, duration_version=version)


def fake_sheet(grid):
    service = MagicMock()
    service.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {
        "title": "统计", "gridProperties": {"rowCount": len(grid), "columnCount": 8}}}]}
    service.spreadsheets().values().batchGet().execute.side_effect = (
        lambda: {"valueRanges": [{"values": grid}]})
    def write():
        changes = service.spreadsheets().values().batchUpdate.call_args.kwargs["body"]["data"]
        for item in changes:
            address = item["range"].split("!")[1]
            row = int("".join(char for char in address if char.isdigit())) - 1
            grid[row][ord(address[0]) - ord("A")] = item["values"][0][0]
        return {"totalUpdatedCells": len(changes)}
    service.spreadsheets().values().batchUpdate().execute.side_effect = write
    return service


class OralCountTests(unittest.TestCase):
    def test_valid_duration_rejects_boolean_zero_negative_and_nonfinite(self):
        for value in (None, True, False, 0, -1, float("nan"), float("inf"), 60000.9, "bad"):
            self.assertIsNone(_valid_video_duration(value))
        self.assertEqual(_valid_video_duration("60001"), 60001)

    def test_threshold_space_aliases_and_only_fl_short_promoted(self):
        groups = {SHORT: [video("a", 59999), video("b", 60000), video("c", 60001)],
                  ("统计", DAY, "02", "FL短口播", "本人"): [video("d", 70000)],
                  ("统计", DAY, "02", "其他类别", "本人"): [video("e", 90000)],
                  LONG: [video("f", 50000)]}
        result, warnings, changes = _adjust_fl_oral_counts(groups, OPTIONS)
        self.assertEqual(len(result[SHORT]), 2)
        self.assertEqual(len(result[LONG]), 3)
        self.assertEqual(len(changes), 2)
        self.assertFalse(warnings)
        self.assertEqual(groups[SHORT][-1]["duration_millis"], 60001)

    def test_metadata_lookup_cached_for_unchanged_version_not_new_revision(self):
        service = MagicMock()
        service.files().get().execute.return_value = {"id": "a", "videoMediaMetadata": {"durationMillis": "65000"}}
        cache = {}
        with patch("model.MaterialDriveSync._execute_with_retry", side_effect=lambda factory: factory().execute()):
            for _ in range(2):
                result, warnings, _ = _adjust_fl_oral_counts(
                    {SHORT: [video("a", version="version-1")]}, OPTIONS, cache, service, True)
                self.assertIn(LONG, result)
                self.assertFalse(warnings)
            self.assertEqual(service.files().get().execute.call_count, 1)
            service.files().get().execute.return_value = {"videoMediaMetadata": {"durationMillis": "40000"}}
            result, _, _ = _adjust_fl_oral_counts(
                {SHORT: [video("a", version="version-2")]}, OPTIONS, cache, service, True)
            self.assertIn(SHORT, result)
            self.assertEqual(service.files().get().execute.call_count, 2)

    def test_lookup_failure_keeps_counts_and_does_not_retry_each_video(self):
        service = MagicMock()
        groups = {SHORT: [video("a"), video("b"), video("c", 80000)]}
        with patch("model.MaterialDriveSync._execute_with_retry", side_effect=OSError("synthetic failure")) as request, patch("model.DailyQuantityStats.logging"):
            result, warnings, changes = _adjust_fl_oral_counts(groups, OPTIONS, {}, service, True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(result[SHORT]), 2)
        self.assertEqual(len(result[LONG]), 1)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(len(changes), 1)

    def test_deleted_file_does_not_prevent_other_duration_checks(self):
        class MissingFile(Exception):
            resp = SimpleNamespace(status=404)
        with patch("model.MaterialDriveSync._execute_with_retry", side_effect=[MissingFile(), {"videoMediaMetadata": {"durationMillis": "70000"}}]) as request, patch("model.DailyQuantityStats.logging"):
            result, warnings, _ = _adjust_fl_oral_counts(
                {SHORT: [video("a"), video("b")]}, OPTIONS, {}, MagicMock(), True)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(result[LONG]), 1)
        self.assertEqual(len(warnings), 1)

    def test_missing_long_category_preserves_original_count_and_warns(self):
        for labels in (["FL 短口播"], ["FL 长口播", "FL长口播"]):
            result, warnings, changes = _adjust_fl_oral_counts(
                {SHORT: [video(duration=70000)]}, {"统计": labels})
            self.assertIn(SHORT, result)
            self.assertNotIn(LONG, result)
            self.assertTrue(warnings)
            self.assertFalse(changes)

    def test_saved_external_preview_uses_duration_without_network(self):
        item = dict(video(duration=61000), id="entry", batch_date=DAY,
                    batch_slot="02", sheet="统计", category="FL 短口播", included=True)
        with patch("model.GoogleDriveHelper.load_drive_service", side_effect=AssertionError("preview must be offline")):
            preview = preview_external_day([item], DAY)
        self.assertEqual(preview["counted"], 1)
        self.assertEqual(preview["daily_counts"][0]["category"], "FL 长口播")
        self.assertEqual(item["category"], "FL 短口播")

    def test_reopened_list_reuses_duration_cache_for_offline_summary(self):
        item = dict(video(version="v1"), id="entry", batch_date=DAY,
                    batch_slot="02", sheet="统计", category="FL 短口播", included=True)
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state.json"
            path.write_text(json.dumps({f"fake-id|{root.resolve()}": {
                "external_videos": [item], "duration_cache": {json.dumps(["video-id", "v1"]): 70000}}}), encoding="utf-8")
            records = external_video_records(config, root, path)
            self.assertEqual(records[0]["duration_millis"], 70000)
            self.assertEqual(preview_external_day(records, DAY)["daily_counts"][0]["category"], "FL 长口播")

    def test_first_delivery_date_kept_but_latest_revision_duration_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "1001"
            project.mkdir()
            (project / "tasks.ods").touch()
            task = SimpleNamespace(task_id="7", source_row=2, daily_stat_sheet="统计",
                                   daily_stat_category="FL 短口播")
            report = {"matched_headers": {"daily_stat_sheet": ["每日统计分页"],
                                           "daily_stat_category": ["每日统计类别"]}}
            base = dict(source="upload", logical_key="same.mp4", file_name="same.mp4",
                        local_file=str(project / "result" / "same.mp4"), batch_slot="02",
                        task={"id": "7", "row": 2}, drive_file_id="drive-id")
            records = [dict(base, batch_date=DAY, duration_millis=40000),
                       dict(base, batch_date="2026-10-02", duration_millis=80000)]
            with patch("model.DailyQuantityStats.ReadTaskOds2", return_value=([task], report)), patch("model.DailyQuantityStats.load_task_table_schema", return_value={}), patch("model.DailyQuantityStats.read_review_history", return_value={"items": {}}):
                groups, warnings = collect_assignments(
                    {"task_table_file_name": "tasks.ods", "task_submission_creator": "本人"},
                    root, records, allowed_dates={DAY})
            self.assertFalse(warnings)
            corrected, _, _ = _adjust_fl_oral_counts(groups, OPTIONS)
            self.assertEqual(list(corrected), [LONG])
            self.assertEqual(len(corrected[LONG]), 1)
            self.assertEqual(task.daily_stat_category, "FL 短口播")

    def test_folder_rescan_replacement_clears_prior_duration(self):
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        service = MagicMock()
        service.files().get().execute.return_value = {"id": "exampleFolder123", "name": "示例", "mimeType": "application/vnd.google-apps.folder"}
        old = dict(id="old", name="a.mp4", mimeType="video/mp4", modifiedTime="old-version", videoMediaMetadata={"durationMillis": "80000"})
        new = dict(id="new", name="a.mp4", mimeType="video/mp4", modifiedTime="new-version")
        with tempfile.TemporaryDirectory() as directory:
            for item in (old, new):
                with patch("model.MaterialDriveSync._collect_remote_files", return_value=[item]):
                    result = scan_external_video_folder(config, directory, "exampleFolder123", DAY, "02", service=service, state_path=Path(directory) / "state.json")
            entry = result["records"][0]
            self.assertEqual(result["replaced"], 1)
            self.assertIsNone(entry["duration_millis"])
            self.assertEqual(entry["duration_drive_file_id"], "new")
            self.assertIn("new-version", entry["duration_version"])

    def test_reconcile_moves_old_count_to_long_without_duplicate_or_formula_edits(self):
        grid = [["", "", "", "", DAY, "", "", ""],
                ["组别", "名字", "类别", "定额", "合计", "12点", "18点", "24点"],
                [], ["AI组", "本人"],
                ["", "", "FL 短口播", 10, "=SUM(F5:H5)", "", "", ""],
                ["", "", "FL 长口播", 5, "=SUM(F6:H6)", "", "", ""]]
        service = fake_sheet(grid)
        current = {SHORT: [video(duration=60000)]}
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        with tempfile.TemporaryDirectory() as directory, patch("model.DailyQuantityStats.collect_assignments", side_effect=lambda *args, **kwargs: (current, [])), patch("model.DailyQuantityStats.read_review_history", return_value={"items": {}}), patch("model.GoogleDriveHelper.load_drive_service", side_effect=AssertionError("stored duration must not access Drive")):
            state_path = Path(directory) / "state.json"
            first = reconcile_daily_quantity(config, directory, service=service, records=[], state_path=state_path)
            self.assertEqual((grid[4][6], grid[5][6]), (1, ""))
            current[SHORT][0]["duration_millis"] = 60001
            second = reconcile_daily_quantity(config, directory, service=service, records=[], state_path=state_path)
            self.assertEqual((grid[4][6], grid[5][6]), (0, 1))
            self.assertEqual(second["counted"], 1)
            self.assertEqual(len(second["automatic_classifications"]), 1)
            third = reconcile_daily_quantity(config, directory, service=service, records=[], state_path=state_path)
            self.assertFalse(third["updated"])
            self.assertEqual(grid[4][3:5], [10, "=SUM(F5:H5)"])
            self.assertEqual(grid[5][3:5], [5, "=SUM(F6:H6)"])

    def test_result_dialog_and_program_log_show_adjusted_filename(self):
        from qt_compat import QtWidgets
        from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        dialog = DailyQuantityDialog()
        result = {"automatic_classifications": [{"file_name": "example.mp4", "duration_millis": 61000,
                                                 "from": "FL 短口播", "to": "FL 长口播"}],
                  "warnings": [], "daily_counts": [], "counted": 1}
        try:
            dialog.show_result(result)
            self.assertIn("example.mp4（61.000 秒）", dialog.details.toPlainText())
            fake = SimpleNamespace(daily_quantity_dialog=None, context=MagicMock(),
                                   refresh_daily_quantity_review_queue=lambda: None)
            TaskDeliveryPlugin._daily_quantity_completed(fake, result)
            self.assertIn("FL 短口播 → FL 长口播", fake.context.log.call_args.args[0])
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
