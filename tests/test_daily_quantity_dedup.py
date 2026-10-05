import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from model.DailyQuantityDedup import (
    deduplicate_video_groups, video_content_fields, video_content_key,
)
from model.DailyQuantityStats import (
    _prepare_video_content, preview_external_day, reconcile_daily_quantity,
)

HASH = "a" * 32
DAY = "2026-09-28"
KEY = ("统计", DAY, "02", "甲", "本人")


def video(file_id, checksum=HASH, size="100", name="same.mp4"):
    return {"drive_file_id": file_id, "file_name": name,
            "content_md5": checksum, "content_size": size}


class ContentDedupTests(unittest.TestCase):
    def test_legacy_history_fingerprints_recovered_without_network(self):
        groups = {KEY:[{"drive_file_id":"old-id", "file_name":"old.mp4"},
                       video("copy-id")]}
        records = [{"drive_file_id":"old-id", "md5":HASH, "size":"100"}]
        prepared = _prepare_video_content(groups,records,{},verify=True)
        result, excluded = deduplicate_video_groups(prepared)
        self.assertEqual(sum(map(len,result.values())),1)
        self.assertEqual(len(excluded),1)

    def test_missing_legacy_hash_fetched_once_and_cached_per_file_version(self):
        service = MagicMock()
        metadata = {"md5Checksum":HASH,"size":"100","trashed":False}
        groups = {KEY:[{"drive_file_id":"old-id","file_name":"old.mp4",
                         "duration_version":"version-1"}]}
        cache = {}
        with patch("model.MaterialDriveSync._execute_with_retry",return_value=metadata) as request:
            for _ in range(2):
                prepared = _prepare_video_content(groups,[],cache,service,verify=True)
                self.assertEqual(video_content_key(prepared[KEY][0]),(HASH,"100"))
            self.assertEqual(request.call_count,1)
            groups[KEY][0]["duration_version"] = "version-2"
            _prepare_video_content(groups,[],cache,service,verify=True)
            self.assertEqual(request.call_count,2)

    def test_failed_verification_aborts_before_any_sheet_write(self):
        grid = [["", "", "", "", DAY, "", "", ""],
                ["组别","名字","类别","定额","合计","12点","18点","24点"],
                [],["AI组","本人"],["","","甲",50,"=SUM(F5:H5)",0,0,0]]
        service = MagicMock()
        service.spreadsheets().get().execute.return_value = {"sheets":[{"properties":{
            "title":"统计","gridProperties":{"rowCount":len(grid),"columnCount":8}}}]}
        service.spreadsheets().values().batchGet().execute.side_effect = lambda:{"valueRanges":[{"values":grid}]}
        groups = {KEY:[{"drive_file_id":"missing-id","file_name":"unverified.mp4"}]}
        with tempfile.TemporaryDirectory() as folder, \
                patch("model.DailyQuantityStats.collect_assignments",return_value=(groups,[])), \
                patch("model.DailyQuantityStats.read_review_history",return_value={"items":{}}), \
                patch("model.MaterialDriveSync._execute_with_retry",side_effect=OSError("network")):
            with self.assertRaisesRegex(RuntimeError,"为避免虚报"):
                reconcile_daily_quantity(
                    {"daily_quantity_sheet_url":"fake-id","task_submission_creator":"本人"},
                    folder,service=service,records=[],state_path=Path(folder)/"state.json",
                    drive_service=MagicMock(),verify_content=True)
        service.spreadsheets().values().batchUpdate.assert_not_called()

    def test_ui_worker_enables_content_verification(self):
        from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityThread
        with patch("app_plugins.builtin.task_delivery_daily_quantity.reconcile_daily_quantity",return_value={}) as call:
            DailyQuantityThread({},"root").run()
        call.assert_called_once_with({},"root",verify_content=True)

    def test_different_ids_names_dates_and_categories_only_count_first_delivery(self):
        first = ("统计", "2026-09-25", "03", "甲", "本人")
        groups = {KEY: [video("copy", name="copy (1).mp4")], first: [video("original")]}
        result, excluded = deduplicate_video_groups(groups)
        self.assertEqual(result, {first: [video("original")]})
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["retained_date"], "2026-09-25")
        self.assertEqual(excluded[0]["category"], "甲")
        reverse, _ = deduplicate_video_groups(dict(reversed(list(groups.items()))))
        self.assertEqual(result, reverse)

    def test_same_name_different_content_and_unknown_content_are_not_guessed(self):
        items = [video("a"), video("b", "b"*32), video("c", ""), video("d", "")]
        result, excluded = deduplicate_video_groups({KEY: items})
        self.assertEqual(len(result[KEY]), 4)
        self.assertFalse(excluded)

    def test_hash_and_size_both_must_match_and_be_valid(self):
        items = [video("a"), video("b", size="101"), video("c", "invalid"),
                 video("d", "invalid"), video("e", size="0"), video("f", size="0")]
        result, excluded = deduplicate_video_groups({KEY: items})
        self.assertEqual(len(result[KEY]), 6)
        self.assertFalse(excluded)

    def test_same_drive_id_is_never_counted_twice_even_if_metadata_differs(self):
        result, excluded = deduplicate_video_groups({KEY: [
            video("same-id"), video("same-id", "b"*32), video("copy", "b"*32)]})
        self.assertEqual(len(result[KEY]), 1)
        self.assertEqual(len(excluded), 2)

    def test_creators_are_separate_and_input_is_not_mutated(self):
        other = (*KEY[:4], "其他人")
        groups = {KEY: [video("a")], other: [video("b")]}
        before = copy.deepcopy(groups)
        result, excluded = deduplicate_video_groups(groups)
        self.assertEqual(sum(map(len, result.values())), 2)
        self.assertEqual(before, groups)
        self.assertFalse(excluded)

    def test_legacy_duration_version_recovers_hash_without_network(self):
        item = {"duration_version": json.dumps(["modified", HASH, "00100"])}
        self.assertEqual(video_content_key(item), (HASH, "100"))
        self.assertEqual(video_content_fields(item), {"content_md5":HASH,"content_size":"100"})

    def test_replacement_without_hash_clears_previous_fingerprint(self):
        old = video("old")
        old.update(video_content_fields({"modifiedTime":"new", "size":"100"}))
        self.assertIsNone(video_content_key(old))
        stale = dict(old, duration_version=json.dumps(["old", HASH,"100"]))
        self.assertIsNone(video_content_key(stale))

    def test_day_preview_deduplicates_against_earlier_available_records(self):
        common = dict(sheet="统计",category="甲",batch_slot="02",included=True,
                      manual_included=True)
        records = [dict(video("old"), **common, batch_date="2026-09-25"),
                   dict(video("new"), **common, batch_date=DAY)]
        with patch("model.DailyQuantityStats.read_review_history", return_value={"items":{}}):
            preview = preview_external_day(records, DAY)
        self.assertEqual(preview["counted"], 0)
        self.assertEqual(preview["not_counted"], 1)
        self.assertEqual(len(preview["duplicate_exclusions"]), 1)

    def test_reconcile_repairs_cached_overcount_and_refresh_is_idempotent(self):
        grid = [["", "", "", "", DAY, "", "", ""],
                ["组别","名字","类别","定额","合计","12点","18点","24点"],
                [], ["AI组","本人"], ["","","甲",50,"=SUM(F5:H5)",0,2,0]]
        service = MagicMock()
        service.spreadsheets().get().execute.return_value = {"sheets":[{"properties":{
            "title":"统计", "gridProperties":{"rowCount":len(grid),"columnCount":8}}}]}
        service.spreadsheets().values().batchGet().execute.side_effect = lambda: {
            "valueRanges":[{"values":grid}]}
        def write():
            data = service.spreadsheets().values().batchUpdate.call_args.kwargs["body"]["data"]
            self.assertEqual(data, [{"range":"'统计'!G5", "values":[[1]]}])
            grid[4][6] = 1
            return {"totalUpdatedCells":1}
        service.spreadsheets().values().batchUpdate().execute.side_effect = write
        config = {"daily_quantity_sheet_url":"fake-id", "task_submission_creator":"本人"}
        groups = {KEY:[video("a"), video("b", name="renamed.mp4")]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            scope = f"fake-id|{Path(folder).resolve()}"
            key = json.dumps(KEY, ensure_ascii=False)
            path.write_text(json.dumps({scope:{"cells":{key:{"base":0,"written":2,
                "videos":groups[KEY]}}}}), encoding="utf-8")
            with patch("model.DailyQuantityStats.collect_assignments", return_value=(groups,[])), \
                    patch("model.DailyQuantityStats.read_review_history", return_value={"items":{}}):
                for attempt in range(3):
                    result = reconcile_daily_quantity(config,folder,service=service,
                        records=[],state_path=path)
                    self.assertEqual(result["counted"],1)
                    self.assertEqual(len(result["updated"]),1 if attempt==0 else 0)
                    self.assertEqual(len(result["duplicate_exclusions"]),1)
            self.assertEqual(grid[4][3:5],[50,"=SUM(F5:H5)"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[scope]["cells"][key]["written"],1)

    def test_targeted_date_cannot_bypass_dedup_against_an_earlier_date(self):
        first = ("统计","2026-09-25","02","甲","本人")
        grid = [["", "", "", "", DAY, "", "", ""],
                ["组别","名字","类别","定额","合计","12点","18点","24点"],
                [],["AI组","本人"],["","","甲",50,"=SUM(F5:H5)",0,0,0]]
        service = MagicMock()
        service.spreadsheets().get().execute.return_value = {"sheets":[{"properties":{
            "title":"统计","gridProperties":{"rowCount":len(grid),"columnCount":8}}}]}
        service.spreadsheets().values().batchGet().execute.side_effect = lambda:{"valueRanges":[{"values":grid}]}
        with tempfile.TemporaryDirectory() as folder, \
                patch("model.DailyQuantityStats.collect_assignments", return_value=({
                    first:[video("old")],KEY:[video("new")]},[])), \
                patch("model.DailyQuantityStats.read_review_history", return_value={"items":{}}):
            result = reconcile_daily_quantity(
                {"daily_quantity_sheet_url":"fake-id","task_submission_creator":"本人"},
                folder,service=service,records=[],state_path=Path(folder)/"state.json",
                only_dates={DAY})
        self.assertEqual(result["counted"],0)
        self.assertEqual(len(result["duplicate_exclusions"]),1)
        self.assertFalse(result["updated"])


if __name__ == "__main__":
    unittest.main()
