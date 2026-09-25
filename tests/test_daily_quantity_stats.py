import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from model.DailyQuantityStats import _find_cell, collect_assignments, reconcile_daily_quantity


class DailyQuantityTests(unittest.TestCase):
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
