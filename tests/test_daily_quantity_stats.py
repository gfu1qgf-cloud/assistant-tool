import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from model.DailyQuantityStats import (
    _find_cell,
    _external_assignments,
    collect_assignments,
    external_video_records,
    external_video_sources,
    reconcile_daily_quantity,
    scan_external_video_folder,
    update_external_video_records,
)


class DailyQuantityTests(unittest.TestCase):
    def test_external_folder_does_not_double_count_a_normal_upload(self):
        key = ("统计", "2026-09-25", "02", "短口播", "本人")
        local = {key: [{"drive_file_id": "video-id-1"}]}
        scope = {"external_videos": [{
            "id": "entry-1", "drive_file_id": "video-id-1", "file_name": "a.mp4",
            "batch_date": "2026-09-25", "batch_slot": "02",
            "sheet": "统计", "category": "短口播", "included": True,
        }]}
        groups, warnings = _external_assignments(scope, "本人", local)
        self.assertFalse(groups)
        self.assertFalse(warnings)

    def test_folder_import_preserves_first_delivery_and_allows_later_classification(self):
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        folder_id = "exampleFolderId12345"
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": folder_id, "name": "零散任务", "mimeType": "application/vnd.google-apps.folder"
        }
        video = {"id": "video-id-1", "name": "a.mp4", "mimeType": "video/mp4",
                 "relative_parts": ("a.mp4",)}
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("model.MaterialDriveSync._collect_remote_files", return_value=[video]):
                first = scan_external_video_folder(
                    config, directory, f"https://drive.google.com/drive/folders/{folder_id}",
                    "2026-09-24", "02", service=service, state_path=state_path
                )
            self.assertEqual((first["found"], first["added"]), (1, 1))
            self.assertEqual(external_video_sources(config, directory, state_path)[0]["id"], folder_id)
            entry = first["records"][0]
            update_external_video_records(config, directory, [{
                "id": entry["id"], "batch_date": "2026-09-24", "batch_slot": "02",
                "sheet": "统计", "category": "短口播", "included": True,
            }], state_path=state_path)
            with patch("model.MaterialDriveSync._collect_remote_files", return_value=[video]):
                second = scan_external_video_folder(
                    config, directory, folder_id, "2026-09-25", "03",
                    service=service, state_path=state_path
                )
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["records"][0]["batch_date"], "2026-09-24")
            self.assertEqual(second["records"][0]["category"], "短口播")
            replacement = dict(video, id="video-id-2")
            with patch("model.MaterialDriveSync._collect_remote_files", return_value=[replacement]):
                third = scan_external_video_folder(
                    config, directory, folder_id, "2026-09-25", "03",
                    service=service, state_path=state_path
                )
            self.assertEqual(third["replaced"], 1)
            self.assertEqual(len(third["records"]), 1)
            self.assertEqual(third["records"][0]["id"], entry["id"])
            with patch("model.MaterialDriveSync._collect_remote_files", return_value=[]):
                scan_external_video_folder(config, directory, folder_id,
                                           "2026-09-25", "03", service=service,
                                           state_path=state_path)
            historical = external_video_records(config, directory, state_path)
            self.assertTrue(historical[0]["missing_from_folder"])
            self.assertTrue(historical[0]["included"])

    def test_external_video_reconciles_without_local_task_table(self):
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        day = "2026-09-25"
        grid = [["", "", "", "", 46290, "", "", ""],
                ["组别", "名字", "尽本分时间", "定额", "一天总数", "中午12点", "中午18点", "晚上24点"],
                [], ["AI组", "本人"],
                ["", "", "短口播", 50, "=SUM(F5:H5)", "", "", ""],
                ["", "", "长口播", 20, "=SUM(F6:H6)", "", "", ""]]
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "统计", "gridProperties": {
                "rowCount": len(grid), "columnCount": len(grid[1])}}}]
        }
        service.spreadsheets.return_value.values.return_value.batchGet.return_value.execute.side_effect = (
            lambda: {"valueRanges": [{"values": grid}]}
        )
        def write():
            body = service.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
            for item in body["data"]:
                address = item["range"].split("!")[1]
                row = int("".join(char for char in address if char.isdigit())) - 1
                col = ord(address[0]) - ord("A")
                grid[row][col] = item["values"][0][0]
            return {"totalUpdatedCells": len(body["data"])}
        service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = write
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            folder_id = "exampleFolderId12345"
            drive = MagicMock()
            drive.files.return_value.get.return_value.execute.return_value = {
                "id": folder_id, "name": "零散任务", "mimeType": "application/vnd.google-apps.folder"
            }
            with patch("model.MaterialDriveSync._collect_remote_files", return_value=[{
                "id": "video-id-1", "name": "a.mp4", "mimeType": "video/mp4",
                "relative_parts": ("a.mp4",),
            }]):
                imported = scan_external_video_folder(
                    config, directory, folder_id, day, "02", service=drive,
                    state_path=state_path
                )
            entry = imported["records"][0]
            update_external_video_records(config, directory, [{
                "id": entry["id"], "batch_date": day, "batch_slot": "02",
                "sheet": "统计", "category": "短口播", "included": True,
            }], state_path=state_path)
            first = reconcile_daily_quantity(config, directory, service=service,
                                             records=[], state_path=state_path)
            second = reconcile_daily_quantity(config, directory, service=service,
                                              records=[], state_path=state_path)
            self.assertEqual(first["counted"], 1)
            self.assertEqual(grid[4][6], 1)
            self.assertFalse(second["updated"])
            self.assertEqual(external_video_records(config, directory, state_path)[0]["category"], "短口播")
            update_external_video_records(config, directory, [{
                "id": entry["id"], "batch_date": day, "batch_slot": "02",
                "sheet": "统计", "category": "长口播", "included": True,
            }], state_path=state_path)
            corrected = reconcile_daily_quantity(config, directory, service=service,
                                                 records=[], state_path=state_path)
            self.assertEqual(len(corrected["updated"]), 2)
            self.assertEqual((grid[4][6], grid[5][6]), (0, 1))

    def test_finds_person_category_and_period_without_touching_quota_or_sum(self):
        rows = [
            ["", "", "", "", 46290, "", "", ""],
            ["组别", "名字", "尽本分时间", "定额", "一天总数", "中午12点", "中午18点", "晚上24点"],
            [],
            ["AI组", "他人", "", "=SUM(D5:D6)"],
            ["", "", "FL 短口播", 50, "=SUM(F5:H5)"],
            ["AI组", "本人", "", "=SUM(D7:D8)"],
            ["", "", "FL 短口播", 50, "=SUM(F7:H7)"],
        ]
        self.assertEqual(_find_cell(rows, "2026-09-25", "02", "本人", "FL 短口播"), (6, 6))

    def test_first_upload_wins_and_missing_category_is_reported(self):
        class Task:
            def __init__(self, category):
                self.task_id = "7"
                self.source_row = 2
                self.daily_stat_sheet = "统计"
                self.daily_stat_category = category

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "0925"
            project.mkdir()
            ods = project / "tasks.ods"
            ods.touch()
            base = {"source": "upload", "logical_key": "same.mp4",
                    "local_file": str(project / "result" / "same.mp4"),
                    "batch_slot": "01", "task": {"id": "7", "row": 2},
                    "file_name": "same.mp4"}
            records = [dict(base, batch_date="2026-09-25", event_id="a"),
                       dict(base, batch_date="2026-09-26", event_id="b")]
            report = {"matched_headers": {"daily_stat_sheet": ["每日统计分页"],
                                           "daily_stat_category": ["每日统计类别"]}}
            with patch("model.DailyQuantityStats.ReadTaskOds2", return_value=([Task("FL 短口播")], report)), patch(
                "model.DailyQuantityStats.load_task_table_schema", return_value={}
            ):
                groups, warnings = collect_assignments(
                    {"task_submission_creator": "本人", "task_table_file_name": "tasks.ods"},
                    root, records,
                )
                self.assertEqual(sum(map(len, groups.values())), 1)
                self.assertEqual(next(iter(groups))[1:3], ("2026-09-25", "01"))
                self.assertFalse(warnings)
            with patch("model.DailyQuantityStats.ReadTaskOds2", return_value=([Task("")], report)), patch(
                "model.DailyQuantityStats.load_task_table_schema", return_value={}
            ):
                groups, warnings = collect_assignments(
                    {"task_submission_creator": "本人", "task_table_file_name": "tasks.ods"},
                    root, records,
                )
                self.assertFalse(groups)
                self.assertTrue(any("未填" in text for text in warnings))

    def test_refresh_is_idempotent_and_reclassifies_without_changing_quota(self):
        day = "2026-09-25"
        grid = [
            ["", "", "", "", 46290, "", "", ""],
            ["组别", "名字", "尽本分时间", "定额", "一天总数", "中午12点", "中午18点", "晚上24点"],
            [],
            ["AI组", "本人", "", "=SUM(D5:D6)"],
            ["", "", "甲", 50, "=SUM(F5:H5)", "", "", ""],
            ["", "", "乙", 20, "=SUM(F6:H6)", "", "", ""],
        ]
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "统计", "gridProperties": {
                "rowCount": len(grid), "columnCount": len(grid[1]),
            }}}],
        }
        service.spreadsheets.return_value.values.return_value.batchGet.return_value.execute.side_effect = (
            lambda: {"valueRanges": [{"values": grid}]}
        )
        def write():
            body = service.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
            for item in body["data"]:
                address = item["range"].split("!")[1]
                row = int("".join(c for c in address if c.isdigit())) - 1
                col = ord(address[0]) - ord("A")
                grid[row][col] = item["values"][0][0]
            return {}
        service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = write
        config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            key_a = ("统计", day, "02", "甲", "本人")
            key_b = ("统计", day, "02", "乙", "本人")
            with patch("model.DailyQuantityStats.collect_assignments", return_value=({key_a: [{"identity": "v1"}]}, [])):
                first = reconcile_daily_quantity(config, directory, service=service, state_path=path)
                second = reconcile_daily_quantity(config, directory, service=service, state_path=path)
            self.assertEqual(len(first["updated"]), 1)
            self.assertFalse(second["updated"])
            self.assertEqual(grid[4][6], 1)
            with patch("model.DailyQuantityStats.collect_assignments", return_value=({key_b: [{"identity": "v1"}]}, [])):
                result = reconcile_daily_quantity(config, directory, service=service, state_path=path)
            self.assertEqual(len(result["updated"]), 2)
            self.assertEqual((grid[4][6], grid[5][6]), (0, 1))
            self.assertEqual(grid[4][3], 50)
            self.assertEqual(grid[4][4], "=SUM(F5:H5)")
            grid[5][6] = 9
            with patch("model.DailyQuantityStats.collect_assignments", return_value=({key_b: [{"identity": "v1"}, {"identity": "v2"}]}, [])):
                conflicted = reconcile_daily_quantity(config, directory, service=service, state_path=path)
            self.assertFalse(conflicted["updated"])
            self.assertTrue(any("外部修改" in item for item in conflicted["warnings"]))


if __name__ == "__main__":
    unittest.main()
