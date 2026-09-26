import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from model.DailyQuantityStats import (
    _daily_count_rows,
    _find_cell,
    _external_assignments,
    _category_options,
    collect_assignments,
    external_video_records,
    external_video_sources,
    preview_external_day,
    reconcile_daily_quantity,
    scan_daily_drive_date,
    scan_external_video_folder,
    update_external_video_records,
)


class DailyQuantityTests(unittest.TestCase):
    def test_saved_day_preview_counts_included_videos_without_sheet_refresh(self):
        records = [
            {"id": str(index), "drive_file_id": str(index),
             "batch_date": "2026-09-25", "batch_slot": "02",
             "included": index < 68, "sheet": "口播", "category": "短口播"}
            for index in range(81)
        ]
        preview = preview_external_day(records, "2026-09-25")
        self.assertEqual(preview["total_files"], 81)
        self.assertEqual(preview["counted"], 68)
        self.assertEqual(preview["not_counted"], 13)
        self.assertEqual(preview["daily_counts"][0]["02"], 68)

    def test_manual_slot_survives_rescanning_nonperiod_folder(self):
        config = {"daily_quantity_sheet_url": "fake-id",
                  "drive_parent_folder_id": "parentFolderId12345",
                  "task_submission_creator": "本人"}
        day = "2026-09-26"
        def children(_service, folder_id):
            if folder_id == "parentFolderId12345":
                return [{"id": "dateFolderId123456", "name": "0926",
                         "mimeType": "application/vnd.google-apps.folder"}]
            return [{"id": "personFolderId1234", "name": "人物素材",
                     "mimeType": "application/vnd.google-apps.folder"}]
        video = {"id": "video-id", "name": "video.mp4", "mimeType": "video/mp4",
                 "relative_parts": ("人物素材", "video.mp4")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch("model.MaterialDriveSync._folder_children", side_effect=children), patch(
                "model.MaterialDriveSync._collect_remote_files", return_value=[video]
            ):
                first = scan_daily_drive_date(config, directory, day,
                                              service=MagicMock(), state_path=path,
                                              records=[])
                entry = first["records"][0]
                self.assertFalse(entry["included"])
                self.assertEqual(entry["batch_slot"], "")
                update_external_video_records(config, directory, [{
                    "id": entry["id"], "batch_date": day, "batch_slot": "02",
                    "included": True, "sheet": "口播", "category": "短口播",
                }], state_path=path)
                second = scan_daily_drive_date(config, directory, day,
                                               service=MagicMock(), state_path=path,
                                               records=[])
            saved = second["records"][0]
            self.assertEqual(saved["batch_slot"], "02")
            self.assertEqual(saved["manual_batch_slot"], "02")
            self.assertTrue(saved["included"])

    def test_daily_count_summary_groups_categories_slots_and_dates(self):
        groups = {
            ("口播视频组", "2026-09-26", "01", "短口播", "本人"): [1, 2],
            ("口播视频组", "2026-09-26", "02", "短口播", "本人"): [3],
            ("效果视频组", "2026-09-26", "03", "特效", "本人"): [4],
            ("口播视频组", "2026-09-25", "03", "短口播", "本人"): [5],
        }
        rows = _daily_count_rows(groups)
        today = [row for row in rows if row["date"] == "2026-09-26"]
        self.assertEqual(sum(row["total"] for row in today), 4)
        self.assertEqual(sum(row["total"] for row in rows), 5)
        short = next(row for row in today if row["category"] == "短口播")
        self.assertEqual((short["01"], short["02"], short["03"], short["total"]),
                         (2, 1, 0, 3))

    def test_excluded_video_outside_period_can_be_classified_without_counting(self):
        config = {"daily_quantity_sheet_url": "fake-id"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            scope = f"fake-id|{Path(directory).resolve()}"
            path.write_text(json.dumps({scope: {"external_videos": [{
                "id": "outside", "file_name": "outside.mp4",
                "batch_date": "2026-09-26", "batch_slot": "00",
                "daily_scan_date": "2026-09-26", "included": False,
            }]}}), encoding="utf-8")
            edit = {"id": "outside", "batch_date": "2026-09-26",
                    "batch_slot": "", "included": False,
                    "sheet": "统计", "category": "短口播"}
            update_external_video_records(config, directory, [edit], state_path=path)
            saved = external_video_records(config, directory, path)[0]
            self.assertEqual(saved["batch_slot"], "")
            self.assertEqual(saved["category"], "短口播")
            with self.assertRaisesRegex(ValueError, "已勾选计数"):
                update_external_video_records(
                    config, directory, [dict(edit, included=True)], state_path=path
                )

    def test_old_task_revision_is_listed_but_not_counted_automatically(self):
        config = {"daily_quantity_sheet_url": "fake-id",
                  "drive_parent_folder_id": "parentFolderId12345",
                  "task_submission_creator": "本人"}
        def children(_service, folder_id):
            if folder_id == "parentFolderId12345":
                return [{"id": "dateFolderId123456", "name": "0925",
                         "mimeType": "application/vnd.google-apps.folder"}]
            return [{"id": "slotFolderId123456", "name": "02",
                     "mimeType": "application/vnd.google-apps.folder"}]
        prior = {("统计", "2026-09-24", "02", "短口播", "本人"): [
            {"drive_file_id": "old-id", "file_name": "same.mp4"}
        ]}
        video = {"id": "new-id", "name": "same.mp4", "mimeType": "video/mp4",
                 "relative_parts": ("02", "Alice", "same.mp4")}
        with tempfile.TemporaryDirectory() as directory:
            with patch("model.MaterialDriveSync._folder_children", side_effect=children), patch(
                "model.MaterialDriveSync._collect_remote_files", return_value=[video]
            ), patch("model.DailyQuantityStats.collect_assignments", return_value=(prior, [])):
                result = scan_daily_drive_date(
                    config, directory, "2026-09-25", service=MagicMock(),
                    state_path=Path(directory) / "state.json", records=[]
                )
        self.assertEqual(result["possible_revisions"], 1)
        self.assertFalse(result["records"][0]["included"])
        self.assertEqual(result["records"][0]["category"], "短口播")

    def test_date_scan_refuses_ambiguous_mmdd_folder(self):
        config = {"daily_quantity_sheet_url": "fake-id",
                  "drive_parent_folder_id": "parentFolderId12345"}
        folders = [{"id": "one", "name": "0925",
                    "mimeType": "application/vnd.google-apps.folder"},
                   {"id": "two", "name": "0925",
                    "mimeType": "application/vnd.google-apps.folder"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch("model.MaterialDriveSync._folder_children", return_value=folders):
                with self.assertRaisesRegex(ValueError, "找到 2 个"):
                    scan_daily_drive_date(config, directory, "2026-09-25",
                                          service=MagicMock(), state_path=path,
                                          records=[])
            self.assertFalse(path.exists())

    def test_category_choices_are_limited_to_my_block(self):
        rows = [[], [], ["AI组", "他人"], ["", "", "他人类别"],
                ["AI组", "本人"], ["", "", "短口播"], ["", "", "长口播"]]
        self.assertEqual(
            _category_options({"统计": rows}, "本人"),
            {"统计": ["短口播", "长口播"]},
        )

    def test_date_scan_lists_physical_videos_prefills_and_reconciles(self):
        day = "2026-09-25"
        parent_id = "parentFolderId12345"
        date_id = "dateFolderId123456"
        slot_id = "slotFolderId123456"
        config = {
            "daily_quantity_sheet_url": "fake-id", "task_submission_creator": "本人",
            "drive_parent_folder_id": parent_id, "review_folder_name": "review",
        }
        normal = {"id": "normal-video-id", "name": "a.mp4", "mimeType": "video/mp4",
                  "relative_parts": ("02", "Alice", "a.mp4")}
        review = {"id": "review-video-id", "name": "b.mp4", "mimeType": "video/mp4",
                  "relative_parts": ("02", "review", "b.mp4")}
        def children(_service, folder_id):
            if folder_id == parent_id:
                return [{"id": date_id, "name": "0925",
                         "mimeType": "application/vnd.google-apps.folder"}]
            if folder_id == date_id:
                return [{"id": slot_id, "name": "02",
                         "mimeType": "application/vnd.google-apps.folder"}]
            return []
        local_key = ("统计", day, "02", "短口播", "本人")
        local = {local_key: [{"file_name": "a.mp4", "drive_file_id": "normal-video-id"}]}
        grid = [["", "", "", "", 46290, "", "", ""],
                ["组别", "名字", "尽本分时间", "定额", "一天总数", "中午12点", "中午18点", "晚上24点"],
                [], ["AI组", "本人"],
                ["", "", "短口播", 50, "=SUM(F5:H5)", "", "", ""]]
        sheet = MagicMock()
        sheet.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "统计", "gridProperties": {
                "rowCount": len(grid), "columnCount": len(grid[1])}}}]
        }
        sheet.spreadsheets.return_value.values.return_value.batchGet.return_value.execute.side_effect = (
            lambda: {"valueRanges": [{"values": grid}]}
        )
        def write():
            body = sheet.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
            for item in body["data"]:
                grid[4][6] = item["values"][0][0]
            return {"totalUpdatedCells": len(body["data"])}
        sheet.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = write
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch("model.MaterialDriveSync._folder_children", side_effect=children), patch(
                "model.MaterialDriveSync._collect_remote_files", return_value=[normal, review]
            ), patch("model.DailyQuantityStats.collect_assignments", return_value=(local, [])):
                scanned = scan_daily_drive_date(config, directory, day, service=MagicMock(),
                                               state_path=path, records=[])
                first = reconcile_daily_quantity(config, directory, service=sheet,
                                                 records=[], state_path=path)
            self.assertEqual((scanned["found"], scanned["pending"], scanned["review"]), (2, 1, 1))
            by_name = {item["file_name"]: item for item in scanned["records"]}
            self.assertEqual(by_name["a.mp4"]["category"], "短口播")
            self.assertTrue(by_name["a.mp4"]["included"])
            self.assertFalse(by_name["b.mp4"]["included"])
            self.assertEqual(first["counted"], 1)
            self.assertEqual(first["daily_counts"][0]["02"], 1)
            self.assertEqual(grid[4][6], 1)
            update_external_video_records(config, directory, [{
                "id": by_name["a.mp4"]["id"], "batch_date": day,
                "batch_slot": "02", "sheet": "统计",
                "category": "人工改过的类别", "included": True,
            }], state_path=path)
            replacement = dict(normal, id="replacement-video-id")
            with patch("model.MaterialDriveSync._folder_children", side_effect=children), patch(
                "model.MaterialDriveSync._collect_remote_files", return_value=[replacement, review]
            ), patch("model.DailyQuantityStats.collect_assignments", return_value=(local, [])):
                replaced = scan_daily_drive_date(config, directory, day, service=MagicMock(),
                                                 state_path=path, records=[])
            self.assertEqual(replaced["replaced"], 1)
            updated = next(item for item in replaced["records"] if item["file_name"] == "a.mp4")
            self.assertEqual(updated["id"], by_name["a.mp4"]["id"])
            self.assertEqual(updated["category"], "人工改过的类别")
            with patch("model.MaterialDriveSync._folder_children", side_effect=children), patch(
                "model.MaterialDriveSync._collect_remote_files", return_value=[review]
            ), patch("model.DailyQuantityStats.collect_assignments", return_value=(local, [])):
                rescanned = scan_daily_drive_date(config, directory, day, service=MagicMock(),
                                                 state_path=path, records=[])
                second = reconcile_daily_quantity(config, directory, service=sheet,
                                                  records=[], state_path=path)
            removed = next(item for item in rescanned["records"]
                           if item["file_name"] == "a.mp4")
            self.assertTrue(removed["missing_from_daily"])
            self.assertEqual(removed["category"], "人工改过的类别")
            self.assertEqual(second["counted"], 0)
            self.assertEqual(grid[4][6], 0)

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
            self.assertEqual(second["daily_counts"][0]["total"], 1)
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
