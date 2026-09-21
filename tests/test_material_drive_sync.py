import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model.MaterialDriveSync import (
    MaterialSyncStateStore,
    sync_material_drive_folder,
)


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Files:
    def __init__(self, service):
        self.service = service

    def list(self, **_kwargs):
        return _Request({"files": [dict(self.service.item)]})


class _Service:
    def __init__(self):
        self.item = {
            "id": "file-1",
            "name": "one.jpg",
            "mimeType": "image/jpeg",
            "size": "12",
            "modifiedTime": "2026-09-18T10:00:00Z",
            "md5Checksum": "first",
            "capabilities": {"canDownload": True},
        }
        self._files = _Files(self)

    def files(self):
        return self._files


class MaterialDriveSyncTests(unittest.TestCase):
    def test_corrupt_history_stops_instead_of_redownloading_everything(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "避免重复下载"):
                MaterialSyncStateStore(path).load()

    def test_consumed_file_stays_consumed_until_remote_revision_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_store = MaterialSyncStateStore(root / "state.json")
            service = _Service()
            settings = {
                "folder_url": "https://drive.google.com/drive/folders/root-1",
                "local_dir": str(root / "downloads"),
                "enabled": True,
                "interval_minutes": 5,
            }
            calls = []

            def download(_service, item, local_root, progress_callback=None):
                del progress_callback
                calls.append(item["md5Checksum"])
                target = Path(local_root) / item["relative_parts"][-1]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item["md5Checksum"].encode("ascii"))
                return target

            with patch("model.MaterialDriveSync._download_item", side_effect=download):
                first = sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    service_factory=lambda: service,
                )
                self.assertEqual(first["downloaded_count"], 1)
                Path(first["downloaded"][0]).unlink()

                second = sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    service_factory=lambda: service,
                )
                self.assertEqual(second["downloaded_count"], 0)
                self.assertEqual(
                    second["state"]["files"]["file-1"]["local_status"],
                    "consumed",
                )

                service.item["md5Checksum"] = "second"
                service.item["modifiedTime"] = "2026-09-18T11:00:00Z"
                third = sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    service_factory=lambda: service,
                )
                self.assertEqual(third["downloaded_count"], 1)
                self.assertEqual(calls, ["first", "second"])

    def test_force_download_ignores_unchanged_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_store = MaterialSyncStateStore(root / "state.json")
            service = _Service()
            settings = {
                "folder_url": "https://drive.google.com/drive/folders/root-1",
                "local_dir": str(root / "downloads"),
                "enabled": True,
                "interval_minutes": 5,
            }

            def download(_service, item, local_root, progress_callback=None):
                del progress_callback
                target = Path(local_root) / item["relative_parts"][-1]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"value")
                return target

            with patch("model.MaterialDriveSync._download_item", side_effect=download) as mocked:
                sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    service_factory=lambda: service,
                )
                sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    force_ids={"file-1"},
                    service_factory=lambda: service,
                )
                self.assertEqual(mocked.call_count, 2)

    def test_new_monitor_adopts_files_just_saved_to_material_library(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads = root / "material"
            downloads.mkdir()
            existing = downloads / "one.jpg"
            existing.write_bytes(b"already imported")
            state_store = MaterialSyncStateStore(root / "state.json")
            service = _Service()
            settings = {
                "folder_url": "https://drive.google.com/drive/folders/root-1",
                "local_dir": str(downloads),
                "enabled": True,
                "interval_minutes": 5,
            }

            with patch("model.MaterialDriveSync._download_item") as download:
                result = sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    adopt_existing=True,
                    service_factory=lambda: service,
                )

            download.assert_not_called()
            self.assertEqual(result["downloaded_count"], 0)
            record = result["state"]["files"]["file-1"]
            self.assertEqual(record["local_status"], "present")
            self.assertEqual(Path(record["local_path"]), existing.resolve())


if __name__ == "__main__":
    unittest.main()
