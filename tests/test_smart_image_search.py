import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_image_search.encoder import ModelLoadError
from app_plugins.builtin.smart_image_search.index import (
    ImageSearchIndex, discover_external_groups,
)
from app_plugins.builtin.smart_image_search.settings import normalize_settings
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
                         str(image).casefold())

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
            pixels = _fitted_icon(path).pixmap(175, 175).toImage()
            self.assertEqual(pixels.pixelColor(0, 87).alpha(), 0)
            self.assertGreater(pixels.pixelColor(87, 87).alpha(), 0)


if __name__ == "__main__":
    unittest.main()
