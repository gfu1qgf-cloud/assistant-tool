import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import numpy as np
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_image_search.encoder import (
    ChineseImageEncoder, MODEL_IDS, MODEL_REVISION, MODEL_SPECS,
    ModelLoadError, combine_search_vectors,
)
from app_plugins.builtin.smart_image_search.index import (
    ImageSearchIndex, discover_external_groups,
)
from app_plugins.builtin.smart_image_search.settings import (
    SmartImageSearchSettingsPage, normalize_settings,
)
from app_plugins.builtin.smart_image_search.plugin import SmartImageSearchPlugin
from app_plugins.builtin.smart_image_search.ui import (
    SmartImageSearchDialog, _fitted_icon, _move_external,
)


class FakeEncoder:
    model_id = "test/chinese-image-text"
    model_key = "base"

    def __init__(self):
        self.calls = []

    def model_is_cached(self):
        return True

    def images(self, paths):
        self.calls.extend(paths)
        vectors = []
        for path in paths:
            with Image.open(path) as image:
                red, _green, blue = image.convert("RGB").getpixel((0, 0))
            value = np.array([red, blue], dtype=np.float32)
            vectors.append(value / np.linalg.norm(value))
        return vectors

    def image(self, path):
        return self.images([path])[0]

    def text(self, _query):
        return np.array([1, 0], dtype=np.float32)


class ImageEncoderTests(unittest.TestCase):
    def test_pinned_models_keep_exact_legacy_base_id(self):
        base = ChineseImageEncoder()
        self.assertEqual(base.model_id, f"{MODEL_IDS['base']}@{MODEL_REVISION}")
        ids = [ChineseImageEncoder(key).model_id for key in MODEL_IDS]
        self.assertEqual(len(set(ids)), 3)
        for spec in MODEL_SPECS.values():
            self.assertEqual(len(spec["revision"]), 40)
        self.assertFalse(base.is_loaded)

    def test_combined_search_weight_and_endpoints(self):
        np.testing.assert_allclose(combine_search_vectors([2, 0]), [1, 0])
        np.testing.assert_allclose(combine_search_vectors([1, 0], [0, 1], 0), [1, 0])
        np.testing.assert_allclose(combine_search_vectors([1, 0], [0, 1], 1), [0, 1])
        vector = combine_search_vectors([1, 0], [0, 1], .8)
        self.assertGreater(vector[1], vector[0])
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1, places=6)

    def test_combination_rejects_invalid_or_mixed_features(self):
        for image, text, weight in [([0, 0], None, .5), ([np.nan, 0], None, .5),
                                    ([1, 0], [0, 1, 2], .5), ([1, 0], [0, 1], 2),
                                    ([1, 0], [0, 1], np.nan)]:
            with self.assertRaises(ValueError):
                combine_search_vectors(image, text, weight)

    def test_reference_cache_invalidates_changed_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reference.png"
            Image.new("RGB", (8, 8), "red").save(path)
            encoder = ChineseImageEncoder()
            encoder.images = Mock(return_value=[np.array([1, 0], np.float32)])
            returned = encoder.image(path)
            returned[0] = 0
            np.testing.assert_allclose(encoder.image(path), [1, 0])
            self.assertEqual(encoder.images.call_count, 1)
            Image.new("RGB", (12, 12), "blue").save(path)
            encoder.image(path)
            self.assertEqual(encoder.images.call_count, 2)

    def test_query_cache_is_bounded(self):
        encoder = ChineseImageEncoder()
        for i in range(80):
            encoder._remember(("text", str(i)), np.array([1, 0], np.float32))
        self.assertEqual(len(encoder._query_cache), 64)
        self.assertIsNone(encoder._cached(("text", "0")))


def group(*paths, kind="material", name="素材组"):
    return [{"source_kind": kind, "name": name,
             "images": [{"path": str(path)} for path in paths]}]


class ImageSearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = ImageSearchIndex(self.root / "index")
        self.red = self.root / "red.png"
        self.blue = self.root / "blue.png"
        Image.new("RGB", (8, 8), (255, 0, 1)).save(self.red)
        Image.new("RGB", (8, 8), (1, 0, 255)).save(self.blue)
        self.encoder = FakeEncoder()

    def test_incremental_build_and_move_without_reencoding(self):
        first = self.index.sync(group(self.red, self.blue), self.encoder)
        self.assertEqual(first["new_or_changed"], 2)
        self.assertEqual(self.index.count(self.encoder.model_id), 2)
        self.assertEqual(len(self.encoder.calls), 2)
        again = self.index.sync(group(self.red, self.blue), self.encoder)
        self.assertEqual(again["new_or_changed"], 0)
        self.assertEqual(len(self.encoder.calls), 2)
        result = self.index.search(self.encoder.model_id, [1, 0])
        self.assertEqual(Path(result[0]["path"]).name, "red.png")
        self.index.remove_paths([self.red])
        self.assertEqual(self.index.count(self.encoder.model_id), 1)
        self.assertEqual(len(self.encoder.calls), 2)
        result = self.index.search(self.encoder.model_id, [1, 0])
        self.assertEqual([Path(row["path"]).name for row in result], ["blue.png"])

    def test_new_image_and_deleted_image_only_change_their_records(self):
        self.index.sync(group(self.red), self.encoder)
        result = self.index.sync(group(self.blue, kind="person"), self.encoder)
        self.assertEqual(result["new_or_changed"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(len(self.encoder.calls), 2)
        self.assertEqual(self.index.search(self.encoder.model_id, [0, 1],
                                           source_kind="material"), [])
        self.assertEqual(len(self.index.search(self.encoder.model_id, [0, 1],
                                               source_kind="person")), 1)

    def test_search_can_return_all_matches(self):
        self.index.sync(group(self.red, self.blue), self.encoder)
        self.assertEqual(len(self.index.search(self.encoder.model_id, [1, 0],
                                               limit=None)), 2)

    def test_model_load_failure_does_not_poison_index(self):
        class BrokenEncoder(FakeEncoder):
            def images(self, _paths):
                raise ModelLoadError("模型下载失败")

        with self.assertRaises(ModelLoadError):
            self.index.sync(group(self.red), BrokenEncoder())
        self.assertEqual(self.index.count(self.encoder.model_id), 0)
        self.assertEqual(self.index.sync(group(self.red), self.encoder)["new_or_changed"], 1)

    def test_external_folder_index_is_recursive_and_incremental(self):
        library = self.root / "大图库"
        nested = library / "子目录"
        nested.mkdir(parents=True)
        image = nested / "灾难.png"
        Image.new("RGB", (8, 8), (255, 0, 1)).save(image)
        (nested / "忽略.txt").write_text("no", encoding="utf-8")
        groups = discover_external_groups([library])
        self.assertEqual(len(groups[0]["images"]), 1)
        self.assertIn("子目录", groups[0]["images"][0]["source_name"])
        first = self.index.sync(groups, self.encoder)
        second = self.index.sync(groups, self.encoder)
        self.assertEqual(first["new_or_changed"], 1)
        self.assertEqual(second["new_or_changed"], 0)
        self.assertEqual(self.index.search(self.encoder.model_id, [1, 0],
                                           source_kind="folder")[0]["path"].casefold(),
                         str(image.resolve()).casefold())

    def test_external_move_removes_only_moved_index_record(self):
        library = self.root / "图库"
        library.mkdir()
        image = library / "red.png"
        image.write_bytes(self.red.read_bytes())
        self.index.sync(discover_external_groups([library]), self.encoder)
        target = self.root / "目标"
        moved = _move_external([image], target)
        self.assertFalse(image.exists())
        self.assertTrue((target / "red.png").is_file())
        self.index.remove_paths(item["source"] for item in moved)
        self.assertEqual(self.index.count(self.encoder.model_id), 0)
        self.assertEqual(self.index.sync(discover_external_groups([library]),
                                         self.encoder)["new_or_changed"], 0)

    def test_settings_keep_distinct_external_roots(self):
        settings = normalize_settings({"library_roots": [str(self.root),
                                                         str(self.root), ""]})
        self.assertEqual(settings["library_roots"], [str(self.root)])
        self.assertEqual(settings["result_limit"], 100)

    def test_legacy_thumbnail_upgrades_without_reencoding_image(self):
        source = self.root / "portrait.png"
        Image.new("RGB", (400, 800), (255, 0, 1)).save(source)
        self.index.sync(group(source), self.encoder)
        legacy = self.index.thumbnails / "legacy.jpg"
        Image.new("RGB", (95, 170), (255, 0, 1)).save(legacy)
        with sqlite3.connect(self.index.path) as connection:
            connection.execute(
                "UPDATE images SET thumbnail=? WHERE path=?",
                (str(legacy), os.path.normcase(os.path.abspath(source))),
            )
        previous_calls = len(self.encoder.calls)
        result = self.index.sync(group(source), self.encoder)
        self.assertEqual(result["new_or_changed"], 0)
        self.assertEqual(result["thumbnail_updated"], 1)
        self.assertEqual(len(self.encoder.calls), previous_calls)
        row = self.index.search(self.encoder.model_id, [1, 0])[0]
        self.assertTrue(row["thumbnail"].endswith("_v2.jpg"))
        with Image.open(row["thumbnail"]) as thumb:
            self.assertEqual(thumb.size, (300, 600))

    def test_models_use_independent_databases_without_overwriting_base(self):
        self.index.sync(group(self.red), self.encoder)
        large = FakeEncoder()
        large.model_key = "large336"
        large.model_id = "test/large336"
        upgraded = ImageSearchIndex.for_encoder(large, self.index.root)
        self.assertEqual(self.index.path.name, "index.sqlite3")
        self.assertEqual(upgraded.path.name, "index-large336.sqlite3")
        upgraded.sync(group(self.blue), large)
        self.assertEqual(self.index.count(self.encoder.model_id), 1)
        self.assertEqual(upgraded.count(large.model_id), 1)
        with self.assertRaises(ValueError):
            self.index.sync(group(self.blue), large)
        self.assertEqual(self.index.count(self.encoder.model_id), 1)

    def test_failed_images_can_be_retried_without_source_change(self):
        class FailsOnce(FakeEncoder):
            def images(self, paths):
                raise ValueError("临时解码失败")
        self.index.sync(group(self.red), FailsOnce())
        self.assertEqual(self.index.count(), 0)
        self.assertEqual(self.index.sync(group(self.red), self.encoder)["new_or_changed"], 1)


class ImageSearchDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_dialog_builds_without_loading_model(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = SmartImageSearchDialog(
                {"model": "base"}, index=ImageSearchIndex(Path(folder) / "index"),
                encoder=FakeEncoder(), store=object(),
            )
            try:
                self.assertEqual(dialog.windowTitle(), "智能搜图")
                self.assertEqual(dialog.search_tabs.tabText(0), "文字搜图")
                self.assertEqual(dialog.search_tabs.tabText(1), "以图找图")
                self.assertEqual(dialog.scope.itemData(3), "folder")
                self.assertIn("0 张", dialog.index_status.text())
            finally:
                dialog.close()

    def test_chinese_query_uses_existing_index_in_background(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "red.png"
            Image.new("RGB", (8, 8), (255, 0, 1)).save(image)
            index = ImageSearchIndex(root / "index")
            encoder = FakeEncoder()
            index.sync(group(image), encoder)
            dialog = SmartImageSearchDialog(
                {"model": "base"}, index=index, encoder=encoder, store=object(),
            )
            try:
                dialog.query_text.setText("红色图片")
                dialog.search()
                loop = QtCore.QEventLoop()
                dialog.worker.finished.connect(loop.quit)
                QtCore.QTimer.singleShot(3000, loop.quit)
                loop.exec()
                self.assertFalse(dialog.is_busy())
                self.assertEqual(dialog.results.count(), 1)
                self.assertIn("red.png", dialog.results.item(0).text())
            finally:
                dialog.close()

    def test_combined_image_and_text_runs_in_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            red, blue = root / "red.png", root / "blue.png"
            Image.new("RGB", (8, 8), "red").save(red)
            Image.new("RGB", (8, 8), "blue").save(blue)
            encoder = FakeEncoder()
            encoder.text = Mock(return_value=np.array([0, 1], np.float32))
            index = ImageSearchIndex(root / "index")
            index.sync(group(red, blue), encoder)
            dialog = SmartImageSearchDialog({}, index=index, encoder=encoder, store=object())
            try:
                dialog.search_tabs.setCurrentIndex(1)
                dialog.query_image.setText(str(red))
                dialog.image_description.setText("蓝色")
                dialog.text_weight.setValue(80)
                dialog.search()
                self.assertFalse(dialog.model_combo.isEnabled())
                loop = QtCore.QEventLoop()
                dialog.worker.finished.connect(loop.quit)
                QtCore.QTimer.singleShot(3000, loop.quit)
                loop.exec()
                self.assertFalse(dialog.is_busy())
                self.assertIn("blue.png", dialog.results.item(0).text())
                self.assertIn("图片＋文字", dialog.status.text())
                encoder.text.assert_called_once_with("蓝色")
            finally:
                dialog.close()

    def test_model_switch_keeps_base_index_without_loading_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "red.png"
            Image.new("RGB", (8, 8), "red").save(image)
            index = ImageSearchIndex(root / "index")
            encoder = FakeEncoder()
            index.sync(group(image), encoder)
            dialog = SmartImageSearchDialog({}, index=index, encoder=encoder, store=object())
            try:
                saved = Mock()
                dialog.settingsChanged.connect(saved)
                dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("large336"))
                self.assertEqual(dialog.encoder.model_key, "large336")
                self.assertFalse(dialog.encoder.is_loaded)
                self.assertEqual(dialog.index.count(), 0)
                self.assertEqual(index.count(), 1)
                self.assertEqual(saved.call_args[0][0]["model"], "large336")
                dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("base"))
                self.assertEqual(dialog.index.path, index.path)
                self.assertEqual(dialog.index.count(), 1)
            finally:
                dialog.close()

    def test_settings_wait_for_queued_worker_finished_handler(self):
        with tempfile.TemporaryDirectory() as folder:
            encoder = FakeEncoder()
            dialog = SmartImageSearchDialog({}, index=ImageSearchIndex(folder),
                                           encoder=encoder, store=object())
            try:
                # Ownership remains even if the underlying thread already ended.
                dialog.worker = QtCore.QObject(dialog)
                dialog.update_settings({"model": "large", "text_weight": 70})
                self.assertTrue(dialog.is_busy())
                self.assertIs(dialog.encoder, encoder)
                self.assertEqual(dialog.settings["model"], "base")
                dialog._finished()
                self.assertEqual(dialog.encoder.model_key, "large")
                self.assertEqual(dialog.text_weight.value(), 70)
                self.assertFalse(dialog.encoder.is_loaded)
            finally:
                dialog.close()

    def test_settings_page_round_trip_preserves_roots_and_weight(self):
        page = SmartImageSearchSettingsPage()
        try:
            page.load_config({"smart_image_search": {"model": "large336",
                "library_roots": ["D:/图库"], "text_weight": 60, "result_limit": 80}})
            value = page.update_config({"other": 123})
            self.assertEqual(value["other"], 123)
            self.assertEqual(value["smart_image_search"]["model"], "large336")
            self.assertEqual(value["smart_image_search"]["library_roots"], ["D:/图库"])
            self.assertEqual(value["smart_image_search"]["text_weight"], 60)
        finally:
            page.widget.close()

    def test_plugin_saves_search_options_without_touching_other_config(self):
        plugin = SmartImageSearchPlugin()
        plugin.context = Mock()
        plugin.context.save_config.return_value = True
        plugin._save_search_settings({"model": "large336", "library_roots": ["D:/图片"]})
        plugin.context.save_config.assert_called_once_with()
        config = plugin.update_config({"other": {"keep": True}})
        self.assertEqual(config["other"], {"keep": True})
        self.assertEqual(config["smart_image_search"]["model"], "large336")

    def test_results_are_paged_without_losing_remaining_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = SmartImageSearchDialog(
                {"result_limit": 20}, index=ImageSearchIndex(Path(folder) / "index"),
                encoder=FakeEncoder(), store=object(),
            )
            try:
                rows = [
                    {"path": str(Path(folder) / f"image-{number}.png"),
                     "source_name": "图库", "source_kind": "folder",
                     "score": 0.5, "thumbnail": ""}
                    for number in range(23)
                ]
                dialog._show_results(rows)
                self.assertEqual(dialog.results.count(), 20)
                self.assertFalse(dialog.load_more_button.isHidden())
                dialog._append_page()
                self.assertEqual(dialog.results.count(), 23)
                self.assertEqual(dialog._shown, 23)
            finally:
                dialog.close()

    def test_portrait_thumbnail_keeps_its_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "portrait.png"
            Image.new("RGB", (25, 100), (255, 0, 0)).save(path)
            pixels = _fitted_icon(path).pixmap(185, 280).toImage()
            self.assertEqual(pixels.pixelColor(0, 140).alpha(), 0)
            self.assertGreater(pixels.pixelColor(92, 140).alpha(), 0)

    def test_drag_exports_file_urls_with_move_as_default(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "image.png"
            Image.new("RGB", (8, 8), "red").save(image)
            dialog = SmartImageSearchDialog(
                {}, index=ImageSearchIndex(Path(folder) / "index"),
                encoder=FakeEncoder(), store=object(),
            )
            try:
                dialog._show_results([{
                    "path": str(image), "source_name": "图库",
                    "source_kind": "folder", "score": 0.8, "thumbnail": "",
                }])
                item = dialog.results.item(0)
                mime = dialog.results.mimeData([item])
                self.assertEqual([Path(url.toLocalFile()) for url in mime.urls()],
                                 [image])
                self.assertEqual(dialog.results.defaultDropAction(),
                                 QtCore.Qt.DropAction.MoveAction)
            finally:
                dialog.close()

    def test_drag_cleanup_only_removes_files_actually_moved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "image.png"
            Image.new("RGB", (8, 8), "red").save(image)
            index = ImageSearchIndex(root / "index")
            encoder = FakeEncoder()
            index.sync(group(image), encoder)
            dialog = SmartImageSearchDialog(
                {}, index=index, encoder=encoder, store=object(),
            )
            try:
                row = index.search(encoder.model_id, [1, 0])[0]
                dialog._show_results([row])
                dialog._finalize_dragged_move(
                    [str(image)], QtCore.Qt.DropAction.TargetMoveAction
                )
                self.assertEqual(index.count(encoder.model_id), 1)
                self.assertEqual(dialog.results.count(), 1)
                destination = root / "destination.png"
                shutil.move(image, destination)
                dialog._finalize_dragged_move(
                    [str(image)], QtCore.Qt.DropAction.TargetMoveAction
                )
                self.assertEqual(index.count(encoder.model_id), 0)
                self.assertEqual(dialog.results.count(), 0)
            finally:
                dialog.close()

    def test_successful_move_drop_removes_source_after_target_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "image.png"
            destination = root / "copied.png"
            Image.new("RGB", (8, 8), "red").save(image)
            index = ImageSearchIndex(root / "index")
            encoder = FakeEncoder()
            index.sync(group(image), encoder)
            dialog = SmartImageSearchDialog(
                {}, index=index, encoder=encoder, store=object(),
            )
            try:
                shutil.copy2(image, destination)
                dialog._finalize_dragged_move(
                    [str(image)], QtCore.Qt.DropAction.MoveAction
                )
                self.assertFalse(image.exists())
                self.assertTrue(destination.is_file())
                self.assertEqual(index.count(encoder.model_id), 0)
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
