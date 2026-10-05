"""Document current offline/retry behavior without changing production code or using Google."""

from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model.TaskResultOrganizer import load_pending_changed_file_batches, run_task_result_organizer


class OrganizerOfflineBehaviorTests(unittest.TestCase):
    def test_missing_drive_parent_keeps_local_result_and_retry_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "1005" / "result"
            output.mkdir(parents=True)
            video = output / "test.mp4"
            video.write_bytes(b"local-export")
            pending = root / "pending.json"
            config = {"run_export": True, "run_upload": True, "upload_only_changed_files": True,
                      "task_result_pending_file": str(pending), "video_detection_mode": "ask"}
            exports = [SimpleNamespace(output_dir=output, updated_files=[video]),
                       SimpleNamespace(output_dir=output, updated_files=[])]
            received = []
            with patch("model.TaskResultOrganizer.export_one_date", side_effect=exports), patch(
                "model.TaskResultOrganizer.build_routed_batches", return_value=[(output, [video], "")]
            ), patch("model.TaskResultOrganizer.load_drive_service") as drive:
                with self.assertRaisesRegex(ValueError, "父目录不能为空"):
                    run_task_result_organizer([date(2026, 10, 5)], root, config, detection_mode_resolver=lambda files: "skip")
                self.assertTrue(pending.is_file())
                self.assertEqual(video.read_bytes(), b"local-export")
                restored = run_task_result_organizer(
                    [date(2026, 10, 5)], root, {**config, "drive_parent_folder_id": "configured-id"},
                    detection_mode_resolver=lambda files: received.extend(files) or "cancel",
                )
            self.assertTrue(restored["cancelled"])
            self.assertEqual(received, [video])
            drive.assert_not_called()

    def test_upload_disabled_does_not_save_new_retry_queue_current_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "1005" / "result"
            output.mkdir(parents=True)
            video = output / "test.mp4"
            video.write_bytes(b"local-export")
            pending = root / "pending.json"
            config = {"run_export": True, "run_upload": False, "upload_only_changed_files": True,
                      "task_result_pending_file": str(pending)}
            exports = [SimpleNamespace(output_dir=output, updated_files=[video]),
                       SimpleNamespace(output_dir=output, updated_files=[])]
            with patch("model.TaskResultOrganizer.export_one_date", side_effect=exports), patch(
                "model.TaskResultOrganizer.pending_review_recovery_records", return_value=[]
            ), patch("model.TaskResultOrganizer.load_drive_service") as drive:
                local = run_task_result_organizer([date(2026, 10, 5)], root, config)
                self.assertIn("关闭上传", local["message"])
                self.assertEqual(load_pending_changed_file_batches(config, root, [date(2026, 10, 5)]), [])
                retry = run_task_result_organizer([date(2026, 10, 5)], root, {**config, "run_upload": True})
            self.assertIn("没有本次新增或更新", retry["message"])
            self.assertEqual(video.read_bytes(), b"local-export")
            drive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
