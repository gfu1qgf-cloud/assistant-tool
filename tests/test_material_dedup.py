import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model.InventoryManager import InventoryStore
from model.MaterialDriveSync import MaterialSyncStateStore, sync_material_drive_folder
from model.MaterialDuplicateIndex import MaterialDuplicateIndex
from model.MaterialSourceDownloader import download_google_drive_source


class _Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class _FolderFiles:
    def __init__(self, item):
        self.item = item

    def get(self, **_kwargs):
        return _Request({"id": "folder-1", "name": "素材包", "mimeType": "application/vnd.google-apps.folder"})

    def list(self, **_kwargs):
        return _Request({"files": [dict(self.item)]})

    def get_media(self, **_kwargs):
        raise AssertionError("duplicate should be skipped before downloading")


class _FolderService:
    def __init__(self, item):
        self.files_api = _FolderFiles(item)

    def files(self):
        return self.files_api


class MaterialDedupTests(unittest.TestCase):
    def test_interrupted_batch_keeps_completed_files_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = InventoryStore(root / "inventory.json", root / "library")
            url = "https://drive.google.com/drive/folders/folder-123"
            attempts = []

            def fake_download(_url, output_dir, **_kwargs):
                folder = Path(output_dir) / "素材包"
                folder.mkdir(exist_ok=True)
                first = folder / "first.mp4"
                attempts.append(first.exists())
                if len(attempts) == 1:
                    first.write_bytes(b"first-complete")
                    raise RuntimeError("网络中断")
                (folder / "second.mp4").write_bytes(b"second-complete")
                return SimpleNamespace(root_paths=(folder,), downloaded_files=1)

            with patch("model.InventoryManager.download_google_drive_source",
                       side_effect=fake_download):
                with self.assertRaisesRegex(Exception, "网络中断"):
                    store.add_material("中断测试", [url])
                self.assertEqual(store.list_materials(), [])
                checkpoints = list((store.material_root / ".download-checkpoints.tmp").rglob("first.mp4"))
                self.assertEqual(len(checkpoints), 1)
                material = store.add_material("中断测试", [url])

            self.assertEqual(attempts, [False, True])
            self.assertEqual((Path(material["path"]) / "素材包" / "first.mp4").read_bytes(),
                             b"first-complete")
            self.assertEqual((Path(material["path"]) / "素材包" / "second.mp4").read_bytes(),
                             b"second-complete")
            self.assertFalse((store.material_root / ".download-checkpoints.tmp").exists())

    def test_index_checks_hash_after_size_and_detects_same_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "existing.mp4"
            existing.write_bytes(b"ABC123")
            different = root / "different.mp4"
            different.write_bytes(b"XYZ789")
            duplicate = root / "incoming.part"
            duplicate.write_bytes(b"ABC123")
            index = MaterialDuplicateIndex(root)

            self.assertEqual(index.find_same_content(duplicate), existing)
            self.assertIsNone(index.find_same_content(different))
            self.assertEqual(
                index.find_remote_md5(6, hashlib.md5(b"ABC123").hexdigest()),
                existing,
            )
            self.assertIsNone(index.find_remote_md5(7, hashlib.md5(b"ABC123").hexdigest()))

            existing.write_bytes(b"changed-size")
            index.register(existing)
            incoming = root / "later.part"
            incoming.write_bytes(b"changed-size")
            self.assertEqual(index.find_same_content(incoming), existing)

    def test_manual_drive_import_skips_duplicate_but_keeps_same_size_new_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = InventoryStore(root / "inventory.json", root / "library")
            source = root / "original.mp4"
            source.write_bytes(b"ABC123")
            original = store.add_material("已有素材", [source])
            payload = [b"ABC123"]

            def fake_download(_link, output_dir, *_args, **_kwargs):
                target = Path(output_dir) / "new-name.mp4"
                target.write_bytes(payload[0])
                return "downloaded", target

            url = "https://drive.google.com/file/d/video-123/view"
            with patch("model.MaterialSourceDownloader.download_one", side_effect=fake_download):
                with self.assertRaisesRegex(ValueError, "重复的网盘素材"):
                    store.add_material("重复素材", [url])
                self.assertEqual(len(store.list_materials()), 1)
                self.assertEqual(len(list(Path(original["path"]).rglob("*.mp4"))), 1)

                payload[0] = b"XYZ789"  # Same byte count, different content.
                created = store.add_material("新素材", [url])
                self.assertEqual(
                    (Path(created["path"]) / "new-name.mp4").read_bytes(),
                    b"XYZ789",
                )

    def test_authenticated_folder_skips_known_checksum_before_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "library" / "old-name.mp4"
            existing.parent.mkdir()
            existing.write_bytes(b"video-data")
            item = {
                "id": "video-1",
                "name": "new-name.mp4",
                "mimeType": "video/mp4",
                "size": str(existing.stat().st_size),
                "md5Checksum": hashlib.md5(existing.read_bytes()).hexdigest(),
            }
            result = download_google_drive_source(
                "https://drive.google.com/drive/folders/folder-1",
                root / "staging",
                service_factory=lambda: _FolderService(item),
                duplicate_index=MaterialDuplicateIndex(existing.parent),
            )
            self.assertEqual(result.downloaded_files, 0)
            self.assertEqual(result.skipped_files, 1)

    def test_append_of_only_duplicate_download_leaves_existing_material_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = InventoryStore(root / "inventory.json", root / "library")
            source = root / "original.mp4"
            source.write_bytes(b"ABC123")
            material = store.add_material("素材", [source])
            before = store.list_materials()[0]

            def fake_download(_link, output_dir, *_args, **_kwargs):
                target = Path(output_dir) / "renamed.mp4"
                target.write_bytes(b"ABC123")
                return "downloaded", target

            with patch("model.MaterialSourceDownloader.download_one", side_effect=fake_download):
                with self.assertRaisesRegex(ValueError, "重复的网盘素材"):
                    store.append_material(
                        material["id"],
                        ["https://drive.google.com/file/d/video-123/view"],
                    )
            self.assertEqual(store.list_materials()[0], before)
            self.assertEqual(
                sorted(path.name for path in Path(material["path"]).iterdir()),
                ["original.mp4"],
            )

    def test_monitor_skips_same_content_and_records_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "library" / "old-name.mp4"
            existing.parent.mkdir()
            existing.write_bytes(b"video-data")
            item = {
                "id": "video-1",
                "name": "new-name.mp4",
                "mimeType": "video/mp4",
                "size": str(existing.stat().st_size),
                "md5Checksum": hashlib.md5(existing.read_bytes()).hexdigest(),
                "modifiedTime": "2026-09-25T10:00:00Z",
            }
            settings = {
                "folder_url": "https://drive.google.com/drive/folders/folder-1",
                "local_dir": str(root / "sync"),
                "enabled": True,
                "interval_minutes": 5,
            }
            state_store = MaterialSyncStateStore(root / "history.json")
            with patch("model.MaterialDriveSync._download_item") as downloader:
                first = sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    service_factory=lambda: _FolderService(item),
                    material_root=existing.parent,
                )
                second = sync_material_drive_folder(
                    settings,
                    state_store=state_store,
                    service_factory=lambda: _FolderService(item),
                    material_root=existing.parent,
                )
            downloader.assert_not_called()
            self.assertEqual(first["downloaded_count"], 0)
            self.assertEqual(first["skipped_count"], 1)
            self.assertEqual(first["duplicate_count"], 1)
            self.assertEqual(second["duplicate_count"], 0)
            self.assertEqual(second["state"]["files"]["video-1"]["local_status"], "duplicate")

    def test_monitor_confirms_by_sha256_when_remote_checksum_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "library" / "old-name.mp4"
            existing.parent.mkdir()
            existing.write_bytes(b"video-data")
            item = {
                "id": "video-1",
                "name": "new-name.mp4",
                "mimeType": "video/mp4",
                "size": str(existing.stat().st_size),
                "modifiedTime": "2026-09-25T10:00:00Z",
            }
            settings = {
                "folder_url": "https://drive.google.com/drive/folders/folder-1",
                "local_dir": str(root / "sync"),
                "enabled": True,
                "interval_minutes": 5,
            }

            def fake_download(_service, _item, local_root, progress_callback=None):
                target = Path(local_root) / "new-name.mp4"
                target.write_bytes(b"video-data")
                return target

            with patch("model.MaterialDriveSync._download_item", side_effect=fake_download):
                result = sync_material_drive_folder(
                    settings,
                    state_store=MaterialSyncStateStore(root / "history.json"),
                    service_factory=lambda: _FolderService(item),
                    material_root=existing.parent,
                )
            self.assertEqual(result["downloaded_count"], 0)
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(result["duplicate_count"], 1)
            self.assertFalse((root / "sync" / "new-name.mp4").exists())
            self.assertEqual(result["state"]["files"]["video-1"]["local_status"], "duplicate")


if __name__ == "__main__":
    unittest.main()
