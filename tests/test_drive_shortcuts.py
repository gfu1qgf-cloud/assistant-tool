import unittest
from pathlib import Path
from unittest.mock import patch

from model.GoogleDriveHelper import (
    GOOGLE_SHORTCUT_MIME,
    ensure_remote_file_shortcut,
    sync_file,
)


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeFiles:
    def __init__(self):
        self.previous = {
            "id": "stable-video", "name": "video.mp4", "mimeType": "video/mp4",
            "md5Checksum": "old", "trashed": False,
        }
        self.shortcut = None
        self.create_calls = []

    def list(self, **_kwargs):
        return FakeRequest({"files": [self.shortcut] if self.shortcut else []})

    def get(self, **_kwargs):
        return FakeRequest(dict(self.previous))

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self.shortcut = {
            "id": "today-shortcut", "name": kwargs["body"]["name"],
            "mimeType": GOOGLE_SHORTCUT_MIME,
            "shortcutDetails": dict(kwargs["body"]["shortcutDetails"]),
        }
        return FakeRequest(dict(self.shortcut))


class FakeService:
    def __init__(self):
        self.files_api = FakeFiles()

    def files(self):
        return self.files_api


class DriveShortcutTests(unittest.TestCase):
    def test_shortcut_in_today_folder_targets_old_file_and_is_idempotent(self):
        service = FakeService()
        shortcut_id = ensure_remote_file_shortcut(
            service, "stable-video", "today-person-folder", "video.mp4"
        )
        self.assertEqual(shortcut_id, "today-shortcut")
        body = service.files_api.create_calls[0]["body"]
        self.assertEqual(body["parents"], ["today-person-folder"])
        self.assertEqual(body["mimeType"], GOOGLE_SHORTCUT_MIME)
        self.assertEqual(body["shortcutDetails"]["targetId"], "stable-video")
        self.assertEqual(
            ensure_remote_file_shortcut(service, "stable-video", "today-person-folder", "video.mp4"),
            shortcut_id,
        )
        self.assertEqual(len(service.files_api.create_calls), 1)

    def test_second_sync_does_not_try_to_upload_over_shortcut(self):
        service = FakeService()
        local = Path("C:/result/video.mp4")
        with patch("model.GoogleDriveHelper.file_md5", return_value="new"), patch(
            "model.GoogleDriveHelper.update_existing_file",
            return_value=dict(service.files_api.previous, md5Checksum="new"),
        ) as update:
            first = sync_file(service, local, "today-person-folder", "stable-video")
            service.files_api.previous["md5Checksum"] = "new"
            second = sync_file(service, local, "today-person-folder", "stable-video")
        update.assert_called_once_with(service, local, "stable-video")
        self.assertEqual(first["action"], "updated_previous_batch")
        self.assertEqual(first["shortcut_id"], "today-shortcut")
        self.assertEqual(second["action"], "skipped_same_previous_batch")
        self.assertEqual(second["shortcut_id"], "today-shortcut")
        self.assertEqual(len(service.files_api.create_calls), 1)

    def test_shortcut_error_is_reported_without_losing_updated_link(self):
        service = FakeService()
        local = Path("C:/result/video.mp4")
        with patch("model.GoogleDriveHelper.file_md5", return_value="new"), patch(
            "model.GoogleDriveHelper.update_existing_file",
            return_value=dict(service.files_api.previous, md5Checksum="new"),
        ), patch("model.GoogleDriveHelper.ensure_remote_file_shortcut", side_effect=RuntimeError("denied")):
            result = sync_file(service, local, "today-person-folder", "stable-video")
        self.assertEqual(result["id"], "stable-video")
        self.assertEqual(result["action"], "updated_previous_batch")
        self.assertIn("denied", result["shortcut_error"])

    def test_retry_checks_existing_shortcut_after_network_timeout(self):
        service = FakeService()
        original_create = service.files_api.create
        attempts = [0]

        def create_then_timeout(**kwargs):
            request = original_create(**kwargs)
            attempts[0] += 1
            if attempts[0] == 1:
                raise TimeoutError("response lost after create")
            return request

        service.files_api.create = create_then_timeout
        with patch("model.GoogleDriveHelper.time.sleep"):
            shortcut_id = ensure_remote_file_shortcut(
                service, "stable-video", "today-person-folder", "video.mp4"
            )
        self.assertEqual(shortcut_id, "today-shortcut")
        self.assertEqual(attempts[0], 1)

    def test_same_name_shortcut_to_another_target_is_not_overwritten(self):
        service = FakeService()
        service.files_api.shortcut = {
            "id": "other-shortcut", "name": "video.mp4",
            "mimeType": GOOGLE_SHORTCUT_MIME,
            "shortcutDetails": {"targetId": "another-video"},
        }
        with self.assertRaises(ValueError):
            ensure_remote_file_shortcut(
                service, "stable-video", "today-person-folder", "video.mp4"
            )
        self.assertEqual(service.files_api.create_calls, [])


if __name__ == "__main__":
    unittest.main()
