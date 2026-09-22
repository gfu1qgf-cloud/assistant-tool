import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from app_plugins.api import MAIN_MENU
from app_plugins.builtin.image_classifier.classifier import (
    DEFAULT_CATEGORY_TREE,
    ImageClassifierEngine,
    PENDING_CATEGORY,
    apply_classification_results,
    choose_category,
    discover_media,
    discover_images,
    flatten_categories,
    normalize_category_tree,
    normalize_image_classifier_settings,
)
from app_plugins.builtin.image_classifier.plugin import ImageClassifierPlugin
from app_plugins.builtin.image_classifier.settings import (
    ImageClassifierSettingsPage,
)


class _PluginContext:
    def __init__(self):
        self.commands = []
        self.pages = []

    def load_config(self):
        return {}

    def register_command(self, command):
        self.commands.append(command)

    def register_settings_page(self, page):
        self.pages.append(page)


class ImageClassifierCoreTests(unittest.TestCase):
    def test_default_categories_are_expanded_and_flattened_by_leaf(self):
        flattened = flatten_categories(DEFAULT_CATEGORY_TREE)

        self.assertGreaterEqual(len(flattened), 35)
        self.assertTrue(any("祈祷与敬拜" in item["path"] for item in flattened))
        self.assertTrue(any("龙卷风与飓风" in item["path"] for item in flattened))
        self.assertTrue(all(item["descriptions"] for item in flattened))

    def test_category_names_are_safe_and_descriptions_are_deduplicated(self):
        tree = normalize_category_tree({
            "../main": {
                "bad/name": ["same", "Same", "", "different"],
            }
        })

        self.assertEqual(list(tree), [".._main"])
        self.assertEqual(list(tree[".._main"]), ["bad_name"])
        self.assertEqual(tree[".._main"]["bad_name"], ["same", "different"])

    def test_decision_routes_weak_or_ambiguous_images_to_manual_review(self):
        categories = ["A/one", "B/two", "C/three"]
        weak = choose_category(categories, [0.10, 0.08, 0.04], 0.20, 0.01)
        ambiguous = choose_category(categories, [0.30, 0.295, 0.10], 0.20, 0.01)
        ready = choose_category(categories, [0.31, 0.24, 0.10], 0.20, 0.01)

        self.assertEqual(weak["assigned_category"], PENDING_CATEGORY)
        self.assertEqual(weak["status"], "low_confidence")
        self.assertEqual(ambiguous["assigned_category"], PENDING_CATEGORY)
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(ready["assigned_category"], "A/one")
        self.assertEqual(ready["status"], "ready")

    def test_discovery_supports_jfif_and_excludes_output_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = source / "classified"
            nested = source / "nested"
            output.mkdir(parents=True)
            nested.mkdir()
            (source / "one.jfif").write_bytes(b"image")
            (nested / "two.png").write_bytes(b"image")
            (output / "already.jpg").write_bytes(b"image")

            recursive = discover_images([source], output, recursive=True)
            flat = discover_images([source], output, recursive=False)

            self.assertEqual({path.name for path in recursive}, {"one.jfif", "two.png"})
            self.assertEqual([path.name for path in flat], ["one.jfif"])

    def test_media_discovery_includes_video_without_changing_image_scanner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "still.png"
            video = root / "clip.mp4"
            image.write_bytes(b"image")
            video.write_bytes(b"video")

            self.assertEqual(
                {path.name for path in discover_media([root])},
                {"still.png", "clip.mp4"},
            )
            self.assertEqual([path.name for path in discover_images([root])], ["still.png"])

    def test_user_category_can_use_reference_material_without_prompt(self):
        settings = normalize_image_classifier_settings({
            "categories": {"人物": {"天使": []}},
            "category_examples": {"人物/天使": [r"C:\references\angels"]},
            "video_sample_frames": 4,
        })
        flattened = flatten_categories(
            settings["categories"], settings["category_examples"]
        )

        self.assertEqual(flattened[0]["descriptions"], [])
        self.assertEqual(flattened[0]["examples"], [r"C:\references\angels"])
        self.assertEqual(settings["video_sample_frames"], 4)

    def test_video_frame_filter_rejects_black_and_accepts_content(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV/NumPy unavailable")
        engine = ImageClassifierEngine({})
        black = np.zeros((32, 32, 3), dtype=np.uint8)
        content = np.zeros((32, 32, 3), dtype=np.uint8)
        content[:, :16] = 40
        content[:, 16:] = 220

        self.assertFalse(engine._useful_video_frame(black))
        self.assertTrue(engine._useful_video_frame(content))

    def test_video_sampling_skips_black_intro(self):
        try:
            import cv2
            import numpy as np
            from PIL import Image
        except ImportError:
            self.skipTest("OpenCV/NumPy/Pillow unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "black_intro.avi"
            writer = cv2.VideoWriter(
                str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 64)
            )
            if not writer.isOpened():
                self.skipTest("No usable OpenCV test video encoder")
            black = np.zeros((64, 64, 3), dtype=np.uint8)
            content = np.zeros((64, 64, 3), dtype=np.uint8)
            content[:, :32] = 40
            content[:, 32:] = 220
            for _index in range(5):
                writer.write(black)
            for _index in range(15):
                writer.write(content)
            writer.release()

            frames = ImageClassifierEngine({
                "video_sample_frames": 3
            })._video_frames(video, Image)
            try:
                self.assertGreaterEqual(len(frames), 1)
                self.assertGreater(float(np.asarray(frames[0]).mean()), 20.0)
            finally:
                for frame in frames:
                    frame.close()

    def test_discovery_does_not_exclude_explicit_folder_below_output_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / "image_library"
            selected = library / "new" / "batch"
            selected.mkdir(parents=True)
            image = selected / "photo.jpeg"
            image.write_bytes(b"image")

            discovered = discover_images(
                [selected], output_dir=library, recursive=True
            )

            self.assertEqual(discovered, [image.resolve()])

    def test_apply_copies_safely_and_generates_unique_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photo.jpg"
            output = root / "out"
            source.write_bytes(b"content")
            record = {
                "source": str(source),
                "assigned_category": "Main/Sub",
                "suggested_category": "Main/Sub",
                "status": "ready",
            }

            first = apply_classification_results([record], output)
            second = apply_classification_results([record], output)

            self.assertEqual(len(first["completed"]), 1)
            self.assertEqual(len(second["completed"]), 1)
            self.assertTrue((output / "Main" / "Sub" / "photo.jpg").is_file())
            self.assertTrue((output / "Main" / "Sub" / "photo_1.jpg").is_file())
            self.assertTrue(source.is_file())

    def test_apply_skips_results_already_marked_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photo.jpg"
            source.write_bytes(b"content")
            report = apply_classification_results([{
                "source": str(source),
                "assigned_category": "Main/Sub",
                "status": "completed",
            }], root / "out")

            self.assertEqual(report["completed"], [])
            self.assertFalse((root / "out" / "Main" / "Sub" / "photo.jpg").exists())

    def test_plugin_registers_menu_and_settings_without_loading_model(self):
        context = _PluginContext()
        plugin = ImageClassifierPlugin()

        plugin.register(context)

        self.assertEqual(len(context.commands), 1)
        self.assertIn(MAIN_MENU, context.commands[0].locations)
        self.assertEqual(context.commands[0].title, "图片/视频素材分类…")
        self.assertEqual(len(context.pages), 1)


class ImageClassifierSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_settings_page_round_trips_categories_and_thresholds(self):
        page = ImageClassifierSettingsPage()
        config = {
            "image_classifier_settings": {
                "model": "base",
                "minimum_similarity": 0.25,
                "minimum_margin": 0.02,
                "video_sample_frames": 4,
                "categories": {"Main": {"Sub": []}},
                "category_examples": {"Main/Sub": [r"C:\references\sub"]},
            }
        }
        page.load_config(config)
        output = {}
        page.update_config(output)
        settings = normalize_image_classifier_settings(
            output["image_classifier_settings"]
        )

        self.assertEqual(settings["model"], "base")
        self.assertAlmostEqual(settings["minimum_similarity"], 0.25)
        self.assertAlmostEqual(settings["minimum_margin"], 0.02)
        self.assertEqual(settings["video_sample_frames"], 4)
        self.assertEqual(settings["categories"], config["image_classifier_settings"]["categories"])
        self.assertEqual(
            settings["category_examples"],
            config["image_classifier_settings"]["category_examples"],
        )
        page.widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
