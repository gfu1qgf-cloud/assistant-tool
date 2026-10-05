import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model.DailyQuantityStats import (
    _external_assignments, _scope_key, collect_assignments, effective_batch_slot,
    external_video_records, reconcile_daily_quantity,
)
from tests.test_daily_quantity_prayer_repair import fake_sheet


class DefaultSlotTests(unittest.TestCase):
    def test_only_missing_slots_default_and_explicit_periods_stay_unchanged(self):
        for value in (None, "", "  ", "00"):
            self.assertEqual(effective_batch_slot(value), "03")
        self.assertEqual(effective_batch_slot("1"), "01")
        self.assertEqual(effective_batch_slot("02"), "02")
        self.assertEqual(effective_batch_slot("99"), "99")

    def test_legacy_missing_slot_counts_but_user_exclusions_and_revisions_do_not(self):
        base = {"batch_date": "2026-10-01", "daily_scan_date": "2026-10-01",
                "batch_slot": "", "included": False, "sheet": "祷告词", "category": "动画"}
        items = [dict(base, id="default", drive_file_id="default"),
                 dict(base, id="user", drive_file_id="user", manual_included=True),
                 dict(base, id="revision", drive_file_id="revision", possible_revision=True),
                 dict(base, id="gone", drive_file_id="gone", missing_from_daily=True),
                 dict(base, id="review", drive_file_id="review", review_path=True)]
        for item in items:
            item["file_name"] = item["id"] + ".mp4"
        with patch("model.DailyQuantityStats.read_review_history", return_value={"items": {}}):
            groups, warnings = _external_assignments({"external_videos": items}, "本人", {})
        self.assertEqual(len(groups[("祷告词", "2026-10-01", "03", "动画", "本人")]), 1)
        self.assertFalse(warnings)

    def test_local_upload_without_slot_uses_last_period(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "1001"
            project.mkdir()
            (project / "tasks.ods").touch()
            config = {"task_submission_creator": "本人", "task_table_file_name": "tasks.ods"}
            task = SimpleNamespace(task_id="7", source_row=2,
                                   daily_stat_sheet="祷告词", daily_stat_category="动画")
            record = {"source": "upload", "logical_key": "video", "batch_date": "2026-10-01",
                      "local_file": str(project / "result" / "a.mp4"), "task": {"id": "7", "row": 2}}
            report = {"matched_headers": {"daily_stat_sheet": ["分页"], "daily_stat_category": ["类别"]}}
            with patch("model.DailyQuantityStats.read_review_history", return_value={"items": {}}), \
                    patch("model.DailyQuantityStats.load_task_table_schema", return_value={}), \
                    patch("model.DailyQuantityStats.ReadTaskOds2", return_value=([task], report)):
                groups, warnings = collect_assignments(config, root, [record])
            self.assertEqual(list(groups)[0][2], "03")
            self.assertFalse(warnings)

    def test_saved_blank_period_backfills_once_and_display_matches_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            config = {"daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人"}
            item = {"id": "legacy", "drive_file_id": "legacy", "batch_date": "2026-10-01",
                    "daily_scan_date": "2026-10-01", "batch_slot": "00", "included": False,
                    "sheet": "祷告词", "category": "动画"}
            path.write_text(json.dumps({_scope_key(config, directory): {"external_videos": [item]}}), "utf-8")
            grid = [["", "", "", "", "2026-10-01", "", "", ""],
                    ["", "", "", "定额", "合计", "12点", "18点", "24点"],
                    ["视频组", "本人", "动画", 50, "=SUM(F3:H3)", "", "", ""]]
            service = fake_sheet({"祷告词": grid})
            with patch("model.DailyQuantityStats.read_review_history", return_value={"items": {}}):
                display = external_video_records(config, directory, path)[0]
                self.assertEqual(display["batch_slot"], "03")
                self.assertTrue(display["included"])
                first = reconcile_daily_quantity(config, directory, service=service, records=[], state_path=path)
                second = reconcile_daily_quantity(config, directory, service=service, records=[], state_path=path)
            self.assertEqual(first["updated"], [{"range": "'祷告词'!H3", "count": 1}])
            self.assertEqual(second["updated"], [])
            self.assertEqual(grid[2][3:5], [50, "=SUM(F3:H3)"])


if __name__ == "__main__":
    unittest.main()
