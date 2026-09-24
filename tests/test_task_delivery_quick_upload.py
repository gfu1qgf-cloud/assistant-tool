import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from app_plugins.builtin.task_delivery_quick_upload import (
    QuickUploadDialog,
    collect_upload_files,
    upload_quick_files,
    validate_folder_name,
)
from model.TaskResultOrganizer import get_upload_batch


class QuickUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_batch_boundaries_and_previous_day_slot(self):
        cases = (
            (datetime(2026, 9, 24, 6, 59), (date(2026, 9, 23), "03")),
            (datetime(2026, 9, 24, 7, 0), (date(2026, 9, 24), "01")),
            (datetime(2026, 9, 24, 11, 59), (date(2026, 9, 24), "01")),
            (datetime(2026, 9, 24, 12, 0), (date(2026, 9, 24), "02")),
            (datetime(2026, 9, 24, 17, 59), (date(2026, 9, 24), "02")),
            (datetime(2026, 9, 24, 18, 0), (date(2026, 9, 24), "03")),
        )
        for now, expected in cases:
            self.assertEqual(get_upload_batch({}, now), expected)

    def test_source_plan_preserves_folders_and_rejects_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "素材"
            folder.mkdir()
            (folder / "one.txt").write_text("one", encoding="utf-8")
            direct = root / "two.txt"
            direct.write_text("two", encoding="utf-8")
            plan = collect_upload_files([folder, direct, folder / "one.txt"])
            self.assertEqual(
                {str(relative) for _, relative in plan},
                {str(Path("素材") / "one.txt"), "two.txt"},
            )
            other = root / "other"
            other.mkdir()
            (other / "two.txt").write_text("other", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "同名"):
                collect_upload_files([direct, other / "two.txt"])
        with self.assertRaises(ValueError):
            validate_folder_name("客户/子目录")

    def test_upload_creates_batch_and_named_folder_without_sheets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            folder_calls = []

            def folder(_service, parent_id, name):
                folder_calls.append((parent_id, name))
                return f"id-{len(folder_calls)}"

            with patch(
                "app_plugins.builtin.task_delivery_quick_upload.get_upload_batch",
                return_value=(date(2026, 9, 24), "02"),
            ), patch(
                "app_plugins.builtin.task_delivery_quick_upload.load_drive_service",
                return_value=object(),
            ), patch(
                "app_plugins.builtin.task_delivery_quick_upload.get_or_create_remote_folder",
                side_effect=folder,
            ), patch(
                "app_plugins.builtin.task_delivery_quick_upload.sync_file",
                return_value={"action": "uploaded"},
            ) as sync:
                result = upload_quick_files([source], "临时补交", "parent-id")

            self.assertEqual(
                folder_calls,
                [("parent-id", "0924"), ("id-1", "02"), ("id-2", "临时补交")],
            )
            self.assertEqual(sync.call_args.args[2], "id-3")
            self.assertEqual(result["folder_link"], "https://drive.google.com/drive/folders/id-3")
            self.assertEqual(result["uploaded"], 1)

    def test_dialog_shows_batch_and_accepts_multiple_sources(self):
        dialog = QuickUploadDialog()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = root / "one.txt"
                second = root / "two.txt"
                first.write_text("1", encoding="utf-8")
                second.write_text("2", encoding="utf-8")
                dialog.sources.add_paths([first, second, first])
                self.assertEqual(len(dialog.sources.paths()), 2)
                self.assertIn("01 截至 12:00", dialog.batch_label.text())
        finally:
            dialog.close()

    def test_partial_failure_is_reported_and_other_files_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("1", encoding="utf-8")
            second.write_text("2", encoding="utf-8")

            def fake_sync(_service, local, _parent):
                if local.name == "one.txt":
                    raise OSError("temporary network error")
                return {"action": "skipped_same"}

            with patch(
                "app_plugins.builtin.task_delivery_quick_upload.get_upload_batch",
                return_value=(date(2026, 9, 24), "03"),
            ), patch(
                "app_plugins.builtin.task_delivery_quick_upload.load_drive_service",
                return_value=object(),
            ), patch(
                "app_plugins.builtin.task_delivery_quick_upload.get_or_create_remote_folder",
                side_effect=["date", "slot", "target"],
            ), patch(
                "app_plugins.builtin.task_delivery_quick_upload.sync_file",
                side_effect=fake_sync,
            ) as sync:
                result = upload_quick_files([first, second], "补交", "parent")
            self.assertEqual(sync.call_count, 2)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(len(result["failures"]), 1)
            self.assertIn("one.txt", result["failures"][0])


if __name__ == "__main__":
    unittest.main()
