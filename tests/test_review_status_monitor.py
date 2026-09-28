import tempfile
import unittest
import os
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from PYUI.review_status_pyui import ReviewStatusDialog, format_history_time

from model.ReviewStatusMonitor import (
    ReviewStatusMonitorThread,
    detect_review_columns,
    is_transient_review_error,
    review_status_from_row,
    statuses_from_review_values,
)
from model.ReviewSubmissionHistory import (
    acknowledge_review_items,
    apply_review_statuses,
    canonical_review_link,
    read_review_history,
    record_review_submissions,
    review_history_snapshot,
)
from model.TaskResultOrganizer import attach_local_task_metadata, file_identity


HEADERS = [
    "双击选-提交时间",
    "提交人员",
    "成品-视频链接",
    "",
    "审核人员1",
    "审核结果1",
    "成品视频-问题",
    "",
    "文字建议",
    "严重程度",
    "审核人员2",
    "审核结果2",
    "成品视频-问题",
    "",
    "文字建议",
    "严重程度",
    "返修链接",
    "返修审核人员1",
    "返修审核结果1",
    "文字建议",
    "返修审核人员2",
    "返修审核结果2",
    "成品视频-问题",
    "文字建议",
]


class ReviewStatusParsingTests(unittest.TestCase):
    def test_drive_link_key_ignores_display_variant(self):
        direct = "https://drive.google.com/file/d/abc_123/view?usp=sharing"
        formula = '=HYPERLINK("https://drive.google.com/file/d/abc_123/view","video")'
        self.assertEqual(canonical_review_link(direct), canonical_review_link(formula))

    def test_current_headers_are_detected_by_name_not_position(self):
        columns = detect_review_columns(HEADERS)
        self.assertEqual(columns["link"], 2)
        self.assertEqual(columns["result1"], 5)
        self.assertEqual(columns["result2"], 11)
        self.assertEqual(columns["rework_result1"], 18)
        self.assertEqual(columns["rework_result2"], 21)

    def test_one_approval_is_still_pending_and_any_rejection_wins(self):
        columns = detect_review_columns(HEADERS)
        row = [""] * len(HEADERS)
        row[5] = "可以使用"
        self.assertEqual(review_status_from_row(row, columns)["status"], "pending")
        row[11] = "需要修改"
        self.assertEqual(
            review_status_from_row(row, columns)["status"],
            "needs_changes",
        )

    def test_rework_results_replace_initial_results(self):
        columns = detect_review_columns(HEADERS)
        row = [""] * len(HEADERS)
        row[5] = "需要修改"
        row[11] = "需要修改"
        row[16] = "https://drive.google.com/file/d/rework/view"
        row[18] = "可以使用"
        row[21] = "可以使用"
        result = review_status_from_row(row, columns)
        self.assertEqual(result["phase"], "返修")
        self.assertEqual(result["status"], "passed")
        self.assertIn("rework/view", result["rework_link"])

    def test_formula_link_is_mapped_without_relying_on_row_number(self):
        row = [""] * len(HEADERS)
        row[2] = '=HYPERLINK("https://drive.google.com/file/d/video-1/view","v.mp4")'
        row[5] = "可以使用"
        row[11] = "可以使用"
        statuses = statuses_from_review_values([["标题"], HEADERS, row])
        key = canonical_review_link("https://drive.google.com/file/d/video-1/view")
        self.assertEqual(statuses[key]["status"], "passed")
        self.assertEqual(statuses[key]["sheet_row"], 3)


class ReviewStatusNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_ssl_eof_is_retryable_but_sheet_structure_error_is_not(self):
        self.assertTrue(is_transient_review_error(
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        ))
        self.assertFalse(is_transient_review_error(
            ValueError("审核表中没有找到审核结果列")
        ))

    def test_retry_rebuilds_service_and_returns_statuses(self):
        thread = ReviewStatusMonitorThread({
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/demo/edit",
        })
        statuses = []
        thread.status.connect(statuses.append)
        first_service = object()
        second_service = object()
        row = [""] * len(HEADERS)
        row[2] = "https://drive.google.com/file/d/retry/view"
        row[5] = "可以使用"
        row[11] = "可以使用"
        with mock.patch(
            "model.ReviewStatusMonitor.load_sheets_service",
            side_effect=[first_service, second_service],
        ) as loader, mock.patch.object(
            thread,
            "_read_values",
            side_effect=[
                ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
                [HEADERS, row],
            ],
        ), mock.patch.object(thread, "_retry_wait", return_value=True):
            service, result = thread._read_statuses_with_retry(None)

        self.assertIs(service, second_service)
        self.assertEqual(loader.call_count, 2)
        self.assertTrue(any("正在重试" in value for value in statuses))
        key = canonical_review_link(row[2])
        self.assertEqual(result[key]["status"], "passed")
        thread.deleteLater()


class ReviewHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_selected_review_rows_copy_plain_drive_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            link = "https://drive.google.com/file/d/copy-me/view"
            record_review_submissions(
                [{"webViewLink": link, "name": "copy.mp4"}],
                path,
            )
            key = canonical_review_link(link)
            apply_review_statuses(
                {
                    key: {
                        "status": "passed",
                        "phase": "初审",
                        "note": "",
                        "severity": "",
                        "sheet_row": 8,
                    }
                },
                path,
            )
            dialog = ReviewStatusDialog(path)
            item = dialog.passed_tree.topLevelItem(0)
            item.setSelected(True)
            dialog.passed_tree.setCurrentItem(item)

            self.assertEqual(dialog.copy_selected_links(), 1)
            self.assertEqual(QtWidgets.QApplication.clipboard().text(), link)
            dialog.close()

    def test_dialog_shows_submission_time_and_orders_newest_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            older_link = "https://drive.google.com/file/d/older/view"
            newer_link = "https://drive.google.com/file/d/newer/view"
            record_review_submissions(
                [{"webViewLink": older_link, "name": "older.mp4"}],
                path,
                now=1_700_000_000,
            )
            record_review_submissions(
                [{"webViewLink": newer_link, "name": "newer.mp4"}],
                path,
                now=1_700_003_600,
            )

            dialog = ReviewStatusDialog(path)
            self.assertEqual(dialog.history_tree.headerItem().text(0), "提交时间")
            newest = dialog.history_tree.topLevelItem(0)
            self.assertEqual(newest.text(2), "newer.mp4")
            self.assertEqual(
                newest.text(0),
                format_history_time(1_700_003_600),
            )
            self.assertIn("提交时间：", newest.toolTip(0))
            dialog.close()

    def test_local_administrator_is_attached_before_review_submission(self):
        video = Path("result") / "video.mp4"
        task = SimpleNamespace(
            admin="管理员B",
            task_name="任务名称",
            task_id="17",
            submission_task_type="",
            submission_task_type_from_table=False,
            review_required=None,
            source_row=9,
        )
        records = [{"local_file": str(video)}]
        attach_local_task_metadata(records, {file_identity(video): task})
        self.assertEqual(records[0]["local_admin"], "管理员B")
        self.assertEqual(records[0]["local_task_name"], "任务名称")
        self.assertEqual(records[0]["local_task_id"], "17")

    def test_transition_notifies_once_and_acknowledgement_clears_badge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            link = "https://drive.google.com/file/d/video-2/view"
            record_review_submissions(
                [
                    {
                        "webViewLink": link,
                        "name": "sample.mp4",
                        "local_admin": "管理员A",
                        "local_task_name": "任务A",
                    }
                ],
                path,
                now=10,
            )
            key = canonical_review_link(link)
            result = {
                key: {
                    "status": "passed",
                    "phase": "初审",
                    "note": "",
                    "severity": "",
                    "sheet_row": 50,
                }
            }
            first = apply_review_statuses(result, path, now=20)
            second = apply_review_statuses(result, path, now=30)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            snapshot = review_history_snapshot(path)
            self.assertEqual(snapshot["passed_count"], 1)
            self.assertEqual(snapshot["passed"][0]["admin"], "管理员A")

            acknowledge_review_items([key], path)
            self.assertEqual(review_history_snapshot(path)["passed_count"], 0)

    def test_pending_rework_link_change_triggers_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            link = "https://drive.google.com/file/d/old-id/view"
            record_review_submissions([{"webViewLink": link, "name": "a.mp4"}], path)
            key = canonical_review_link(link)
            pending = {key: {"status": "pending", "phase": "返修",
                             "rework_link": "https://drive.google.com/file/d/new-id/view"}}
            first = apply_review_statuses(pending, path)
            second = apply_review_statuses(pending, path)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(read_review_history(path)["items"][key]["rework_link"],
                             pending[key]["rework_link"])


if __name__ == "__main__":
    unittest.main()
