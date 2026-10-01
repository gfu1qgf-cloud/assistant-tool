import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from model.TaskResultOrganizer import (
    build_routed_batches, compress_routed_batches, file_identity, run_task_result_organizer,
)
from PYUI.task_delivery_pyui import TaskReviewListDialog


class TaskReviewPlanTests(unittest.TestCase):
    def test_explicit_choice_survives_compressed_filename_change(self):
        root = Path("result")
        source = root / "video.mp4"
        compressed = root / "[SHANA]video.mp4"
        mapped = {}
        with mock.patch("model.TaskResultOrganizer.compress_video", return_value=compressed):
            compress_routed_batches([(root, [source], Path("Alice"))], {},
                                   review_decisions={file_identity(source): "normal_upload"},
                                   upload_review_decisions=mapped)
        self.assertEqual(mapped, {file_identity(compressed): "normal_upload"})

    def test_manual_list_can_route_and_skip_without_forced_detail_windows(self):
        root = Path("result")
        files = [root / f"AliceMARKER-0930-{index}-video.mp4" for index in range(3)]
        config = {"video_detection_mode": "manual", "video_filename_creator_marker": "MARKER"}
        received = []

        def choose(entries):
            received.extend(entries)
            return {str(path): decision for path, decision in zip(files, ("normal_upload", "skip_upload", "review"))}

        with mock.patch("model.VideoElementDetector.read_cached_detection", return_value=None), \
             mock.patch("model.TaskResultOrganizer.detect_video_element") as detector, \
             mock.patch("model.VideoElementDetector.save_detection_report") as save:
            result = build_routed_batches([(root, files)], config, review_plan_resolver=choose)
        detector.assert_not_called()
        self.assertEqual(len(received), 3)
        self.assertEqual(result, [(root, [files[0]], Path("Alice")), (root, [files[2]], Path("review"))])
        self.assertEqual(save.call_count, 3)
        self.assertTrue(save.call_args_list[1].args[1]["skip_upload"])

    def test_cancel_keeps_pending_files_and_does_not_connect_to_drive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "0930" / "result"
            output.mkdir(parents=True)
            video = output / "AliceMARKER-0930-1-video.mp4"
            video.write_bytes(b"video")
            pending = root / "pending.json"
            config = {
                "video_detection_mode": "manual", "video_filename_creator_marker": "MARKER",
                "task_result_pending_file": str(pending),
            }
            with mock.patch("model.TaskResultOrganizer.export_one_date", return_value=SimpleNamespace(output_dir=output, updated_files=[video])), \
                 mock.patch("model.VideoElementDetector.read_cached_detection", return_value=None), \
                 mock.patch("model.VideoElementDetector.save_detection_report") as save, \
                 mock.patch("model.TaskResultOrganizer.load_drive_service") as drive:
                result = run_task_result_organizer([date(2026, 9, 30)], root, config,
                                                   review_plan_resolver=lambda _entries: None)
            self.assertTrue(result["cancelled"])
            self.assertTrue(pending.is_file())
            self.assertEqual(result["uploaded_file_count"], 0)
            save.assert_not_called()
            drive.assert_not_called()

    def test_invalid_or_incomplete_list_never_writes_partial_choices(self):
        files = [Path("result") / f"video{index}.mp4" for index in range(2)]
        with mock.patch("model.VideoElementDetector.read_cached_detection", return_value=None), \
             mock.patch("model.VideoElementDetector.save_detection_report") as save:
            with self.assertRaises(ValueError):
                build_routed_batches([(Path("result"), files)], {
                    "video_detection_mode": "manual", "video_filename_creator_marker": "MARKER",
                }, review_plan_resolver=lambda _entries: {str(files[0]): "review"})
        save.assert_not_called()


class TaskReviewListUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_choices_and_optional_detail_result_are_returned_together(self):
        entries = [{"file_path": "C:/task/video.mp4", "decision": "review", "summary": "待标记"}]
        dialog = TaskReviewListDialog(entries)
        combo = dialog.combos[0]
        combo.setCurrentIndex(combo.findData("normal_upload"))
        self.assertEqual(dialog.decisions()[entries[0]["file_path"]]["decision"], "normal_upload")
        with mock.patch("model.VideoElementDetector.manual_review_video", return_value={
            "found": False, "skip_upload": True, "manual_review": True, "summary": "视频有问题，不上传",
        }):
            dialog._open_detail(0)
            self.assertTrue(dialog._detail_worker.wait(3000))
            self.app.processEvents()
        selected = dialog.decisions()[entries[0]["file_path"]]
        self.assertEqual(selected["decision"], "skip_upload")
        self.assertTrue(selected["result"]["manual_review"])
        # Inspecting and editing the table does not modify the caller's plan.
        self.assertEqual(entries[0]["decision"], "review")
        dialog.reject()


if __name__ == "__main__":
    unittest.main()
