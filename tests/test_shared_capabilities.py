"""Dependency boundaries and compatibility of extracted shared capabilities."""
import ast
from contextlib import closing
import importlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from app_paths import APP_ROOT
from model.image_retrieval.encoder import ChineseImageEncoder, MODEL_SPECS
from model.image_retrieval.index import ImageSearchIndex, INDEX_ROOT, THUMBNAIL_VERSION
from model.image_retrieval.settings import normalize_model_settings


PLUGINS = {"batch_text_video", "smart_video_editor", "smart_image_search", "smart_image_assignment"}
LEGACY = {"app_plugins.builtin.smart_video_editor.player",
          "app_plugins.builtin.smart_image_search.encoder",
          "app_plugins.builtin.smart_image_search.index"}


def imported_modules(source, package):
    """Resolve absolute, relative and literal dynamic imports for boundary checks."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            yield module
            yield from (module + "." + item.name for item in node.names)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
            target = ast.unparse(node.func)
            if isinstance(name, str) and (target == "__import__" or target.endswith(".import_module")):
                yield importlib.util.resolve_name(name, package)


class SharedBoundaryTests(unittest.TestCase):
    def test_plugin_pairs_do_not_import_each_other_or_legacy_implementations(self):
        root = Path(__file__).resolve().parents[1]
        for plugin in sorted(PLUGINS):
            for path in (root / "app_plugins" / "builtin" / plugin).rglob("*.py"):
                parts = path.relative_to(root).with_suffix("").parts
                package = ".".join(parts[:-1])
                for target in imported_modules(path.read_text("utf-8-sig"), package):
                    for other in PLUGINS - {plugin}:
                        prefix = "app_plugins.builtin." + other
                        self.assertFalse(target == prefix or target.startswith(prefix + "."),
                                         f"{path.relative_to(root)} depends on another plugin: {target}")
                    self.assertFalse(any(target == old or target.startswith(old + ".") for old in LEGACY),
                                     f"{path.relative_to(root)} must use the shared library: {target}")

    def test_shared_library_never_imports_plugin_implementations(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / "model" / "MediaPlayback.py"]
        paths += list((root / "model" / "image_retrieval").glob("*.py"))
        for path in paths:
            package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
            for target in imported_modules(path.read_text("utf-8-sig"), package):
                self.assertFalse(target == "app_plugins" or target.startswith("app_plugins."), target)

    def test_check_understands_relative_and_dynamic_imports(self):
        source = "from ..smart_video_editor.player import Surface\nimportlib.import_module('app_plugins.builtin.smart_image_search.index')\nimportlib.import_module('..smart_image_search.index', __package__)"
        imports = set(imported_modules(source, "app_plugins.builtin.batch_text_video"))
        self.assertIn("app_plugins.builtin.smart_video_editor.player", imports)
        self.assertIn("app_plugins.builtin.smart_image_search.index", imports)

    def test_legacy_imports_are_aliases_not_duplicate_implementations(self):
        from model.MediaPlayback import FfplayReviewSurface
        self.assertIs(importlib.import_module("app_plugins.builtin.smart_video_editor.player").FfplayReviewSurface,
                      FfplayReviewSurface)
        self.assertIs(importlib.import_module("app_plugins.builtin.smart_image_search.encoder").ChineseImageEncoder,
                      ChineseImageEncoder)
        self.assertIs(importlib.import_module("app_plugins.builtin.smart_image_search.index").ImageSearchIndex,
                      ImageSearchIndex)


class SharedCompatibilityTests(unittest.TestCase):
    def test_only_model_selection_is_shared_not_plugin_presentation_settings(self):
        from app_plugins.builtin.smart_image_search.settings import normalize_settings
        config = {"model": "large336", "result_limit": 140, "text_weight": 85,
                  "library_roots": ["example/library"]}
        self.assertEqual(normalize_model_settings(config), {"model": "large336"})
        self.assertEqual(normalize_settings(config), config)
        for value in (None, [], {}, {"model": "invalid"}):
            self.assertEqual(normalize_model_settings(value), {"model": "base"})

    def test_model_repository_revisions_and_cache_lookup_stay_identical(self):
        expected = {
            "base": ("OFA-Sys/chinese-clip-vit-base-patch16", "f4a64596bbcf9a2a94591b74b9dc39b2e4e77e3e"),
            "large": ("OFA-Sys/chinese-clip-vit-large-patch14", "e84ea94303a56200300792613c510894a4c6f9f5"),
            "large336": ("OFA-Sys/chinese-clip-vit-large-patch14-336px", "7543eecd94b8736b8ccc315153553905f31e6749"),
        }
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "model.safetensors"
            cached.write_bytes(b"test-cache-not-a-model")
            for key, (repository, revision) in expected.items():
                encoder = ChineseImageEncoder(key)
                self.assertEqual((MODEL_SPECS[key]["repository"], encoder.revision), (repository, revision))
                self.assertEqual(encoder.model_id, repository + "@" + revision)
                with patch("huggingface_hub.try_to_load_from_cache", return_value=str(cached)) as lookup:
                    self.assertTrue(encoder.model_is_cached())
                    lookup.assert_called_once_with(repository, "model.safetensors", revision=revision)
                self.assertFalse(encoder.is_loaded)

    def test_existing_vectors_database_and_thumbnails_reused_without_encoding(self):
        self.assertEqual(INDEX_ROOT, APP_ROOT / "SmartImageSearch")
        self.assertEqual(THUMBNAIL_VERSION, "v2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "picture.png"
            Image.new("RGB", (16, 32), "red").save(source)
            thumbnail = root / "existing_v2.jpg"
            Image.new("RGB", (16, 32), "red").save(thumbnail)
            vector = np.asarray([1, 0], dtype=np.float16).tobytes()
            path = os.path.normcase(str(source.resolve()))
            info = source.stat()
            model_id = ChineseImageEncoder().model_id
            database = root / "index.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE images (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, source_kind TEXT NOT NULL, source_name TEXT NOT NULL, model_id TEXT NOT NULL, vector BLOB, thumbnail TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', indexed_at REAL NOT NULL)")
                connection.execute("INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (path, info.st_size, info.st_mtime_ns, "material", "test", model_id,
                                    vector, str(thumbnail), "", 1))
                connection.commit()
            class NeverEncode:
                model_key = "base"
                def images(self, _paths):
                    raise AssertionError("Compatible existing vectors must not be rebuilt")
            encoder = NeverEncode()
            encoder.model_id = model_id
            index = ImageSearchIndex(root)
            result = index.sync([{"source_kind": "material", "name": "test", "images": [{"path": str(source)}]}], encoder)
            self.assertEqual(result["new_or_changed"], 0)
            self.assertEqual(index.count(model_id), 1)
            self.assertEqual(index.search(model_id, [1, 0])[0]["thumbnail"], str(thumbnail))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT vector FROM images").fetchone()[0], vector)
            for key in ("large", "large336"):
                self.assertEqual(ImageSearchIndex(root, key).path.name, f"index-{key}.sqlite3")

    def test_media_preview_does_not_depend_on_app_root_or_external_executables(self):
        from model import MediaPlayback
        self.assertIs(MediaPlayback.FfplayReviewSurface, MediaPlayback.QtMediaReviewSurface)
        source = Path(MediaPlayback.__file__).read_text(encoding='utf-8-sig')
        self.assertNotIn('QProcess', source)
        self.assertNotIn('SetParent', source)
        self.assertNotIn('resolve_mpv', source)
