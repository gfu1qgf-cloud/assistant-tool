import os
import shutil
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets

from app_plugins.builtin.material_organizer.analyzer import (
    analyze_video,
    classify_motion_measurements,
    normalize_material_organizer_settings,
)
from app_plugins.builtin.material_organizer.settings import (
    MaterialOrganizerSettingsPage,
)
from app_plugins.builtin.material_organizer.store import MaterialCatalogStore
from app_plugins.builtin.material_organizer.ui import (
    MaterialAssetModel,
    MaterialOrganizerDialog,
)


def _measurements(**overrides):
    base = {
        "dx": 0.02,
        "dy": 0.01,
        "zoom": 0.01,
        "rotation": 0.01,
        "residual": 0.04,
        "flow": 0.05,
        "inlier_ratio": 0.92,
    }
    values = []
    for _index in range(8):
        item = dict(base)
        item.update(overrides)
        values.append(item)
    return values


class MaterialCatalogStoreTests(unittest.TestCase):
    def test_virtual_categories_never_modify_source_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.mp4"
            original = b"source-video-must-remain"
            source.write_bytes(original)
            store = MaterialCatalogStore(
                root / "catalog.sqlite3", root / "thumbnails"
            )
            asset_id = store.upsert_pending_asset(source)
            custom_id = store.create_category("客户精选")
            store.add_memberships([asset_id], custom_id)
            self.assertEqual(
                [item["id"] for item in store.list_assets(custom_id)],
                [asset_id],
            )
            store.remove_memberships([asset_id], custom_id)
            store.remove_assets([asset_id])
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), original)

    def test_manual_membership_survives_automatic_reclassification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"clip")
            store = MaterialCatalogStore(
                root / "catalog.sqlite3", root / "thumbnails"
            )
            asset_id = store.upsert_pending_asset(source)
            static_id = int(store.category_by_key("static")["id"])
            store.add_memberships([asset_id], static_id)
            store.update_analysis(asset_id, {
                "motion_key": "static",
                "status": "ready",
                "confidence": 0.9,
                "analysis": {"reason": "test"},
            })
            store.update_analysis(asset_id, {
                "motion_key": "pan_left",
                "status": "ready",
                "confidence": 0.8,
                "analysis": {"reason": "changed"},
            })
            self.assertEqual(
                [item["id"] for item in store.list_assets(static_id)],
                [asset_id],
            )
            pan_id = int(store.category_by_key("pan_left")["id"])
            self.assertEqual(
                [item["id"] for item in store.list_assets(pan_id)],
                [asset_id],
            )

    def test_check_library_marks_missing_without_removing_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "gone.mp4"
            source.write_bytes(b"clip")
            store = MaterialCatalogStore(
                root / "catalog.sqlite3", root / "thumbnails"
            )
            asset_id = store.upsert_pending_asset(source)
            source.unlink()
            self.assertEqual(store.refresh_file_states(), 1)
            self.assertFalse(store.asset(asset_id)["exists_now"])


class MaterialMotionClassifierTests(unittest.TestCase):
    def test_common_motion_categories(self):
        settings = normalize_material_organizer_settings({})
        self.assertEqual(
            classify_motion_measurements(_measurements(), settings)[0], "static"
        )
        self.assertEqual(
            classify_motion_measurements(
                _measurements(dx=0.9, dy=0.08, flow=0.9), settings
            )[0],
            "pan_left",
        )
        self.assertEqual(
            classify_motion_measurements(
                _measurements(dx=-0.9, dy=0.08, flow=0.9), settings
            )[0],
            "pan_right",
        )
        self.assertEqual(
            classify_motion_measurements(
                _measurements(zoom=0.9, flow=0.9), settings
            )[0],
            "push_in",
        )
        self.assertEqual(
            classify_motion_measurements(
                _measurements(dx=0.9, zoom=0.9, flow=1.2), settings
            )[0],
            "mixed",
        )
        self.assertEqual(
            classify_motion_measurements(
                _measurements(residual=1.0, flow=1.0), settings
            )[0],
            "subject_motion",
        )

    def test_valid_video_always_gets_thumbnail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "static.avi"
            writer = cv2.VideoWriter(
                str(source),
                cv2.VideoWriter_fourcc(*"MJPG"),
                10.0,
                (320, 180),
            )
            if not writer.isOpened():
                self.skipTest("OpenCV MJPG encoder is unavailable")
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            for y in range(0, 180, 20):
                for x in range(0, 320, 20):
                    if (x // 20 + y // 20) % 2:
                        frame[y:y + 20, x:x + 20] = (220, 220, 220)
            for _index in range(24):
                writer.write(frame)
            writer.release()
            result = analyze_video(source, root / "thumbs", {})
            self.assertEqual(result["status"], "ready")
            self.assertTrue(Path(result["thumbnail_path"]).is_file())
            self.assertEqual((result["width"], result["height"]), (320, 180))

    def test_broken_video_gets_error_thumbnail_instead_of_blank_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "broken.mp4"
            source.write_bytes(b"not-a-video")
            result = analyze_video(source, root / "thumbs", {})
            self.assertEqual(result["motion_key"], "damaged")
            self.assertEqual(result["status"], "error")
            thumbnail = Path(result["thumbnail_path"])
            self.assertTrue(thumbnail.is_file())
            self.assertGreater(thumbnail.stat().st_size, 0)

    def test_thumbnail_is_written_to_unicode_windows_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ascii_source = root / "source.avi"
            writer = cv2.VideoWriter(
                str(ascii_source),
                cv2.VideoWriter_fourcc(*"MJPG"),
                10.0,
                (160, 90),
            )
            if not writer.isOpened():
                self.skipTest("OpenCV MJPG encoder is unavailable")
            frame = np.full((90, 160, 3), 120, dtype=np.uint8)
            for _index in range(12):
                writer.write(frame)
            writer.release()
            unicode_root = root / "辅助小工具" / "素材整理"
            unicode_root.mkdir(parents=True)
            source = unicode_root / "第一条视频.avi"
            shutil.move(str(ascii_source), str(source))
            result = analyze_video(source, unicode_root / "缩略图", {})
            thumbnail = Path(result["thumbnail_path"])
            self.assertTrue(thumbnail.is_file())
            self.assertGreater(thumbnail.stat().st_size, 0)


class MaterialOrganizerQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_asset_drag_only_offers_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "clip.mp4"
            source.write_bytes(b"clip")
            model = MaterialAssetModel()
            model.set_assets([{
                "id": 7,
                "path": str(source),
                "file_name": source.name,
                "thumbnail_path": "",
                "duration": 0,
                "confidence": 0,
                "exists_now": 1,
                "analysis": {},
            }])
            index = model.index(0, 0)
            mime = model.mimeData([index])
            self.assertEqual(
                model.supportedDragActions(), QtCore.Qt.DropAction.CopyAction
            )
            self.assertEqual(
                Path(mime.urls()[0].toLocalFile()).resolve(), source.resolve()
            )

    def test_settings_round_trip(self):
        page = MaterialOrganizerSettingsPage()
        try:
            config = {"material_organizer_settings": {"sample_fps": 4.5}}
            page.load_config(config)
            updated = {}
            page.update_config(updated)
            self.assertEqual(
                updated["material_organizer_settings"]["sample_fps"], 4.5
            )
        finally:
            page.widget.deleteLater()

    def test_dialog_browses_virtual_catalog_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"immutable")
            thumbnail = root / "thumb.jpg"
            cv2.imwrite(str(thumbnail), np.full((90, 160, 3), 120, np.uint8))
            store = MaterialCatalogStore(
                root / "catalog.sqlite3", root / "thumbnails"
            )
            asset_id = store.upsert_pending_asset(source)
            store.update_analysis(asset_id, {
                "motion_key": "static",
                "status": "ready",
                "confidence": 0.95,
                "duration": 5.0,
                "width": 1080,
                "height": 1920,
                "fps": 30.0,
                "thumbnail_path": str(thumbnail),
                "analysis": {"reason": "test"},
            })
            dialog = MaterialOrganizerDialog(store, {})
            try:
                self.assertGreater(dialog.category_tree.topLevelItemCount(), 2)
                self.assertEqual(dialog.asset_model.rowCount(), 1)
                self.assertEqual(source.read_bytes(), b"immutable")
            finally:
                dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
