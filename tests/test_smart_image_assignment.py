import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_image_assignment import engine
from app_plugins.builtin.smart_image_assignment.settings import DEFAULT_MODEL, SmartAssignmentSettingsPage, normalize_settings
from app_plugins.builtin.smart_image_assignment.ui import GroupPicker, SmartAssignmentDialog
from app_plugins.builtin.smart_image_search.encoder import ChineseImageEncoder
from app_plugins.builtin.smart_image_search.index import ImageSearchIndex
from model.GeminiKeyManager import GeminiKeyManager
from model.InventoryManager import InventoryStore


def judgment(status="suitable", score=90):
    return {"status": status, "score": score, "reason": "色彩和主题相符"}


class FakeEncoder:
    model_key, model_id = "base", "test/smart-assignment"
    def model_is_cached(self):
        return True
    def images(self, paths):
        result = []
        for path in paths:
            with Image.open(path) as source:
                red, _green, blue = source.convert("RGB").getpixel((0, 0))
            vector = np.array([red, blue], dtype=np.float32)
            result.append(vector / np.linalg.norm(vector))
        return result
    def image(self, path):
        return self.images([path])[0]
    def text(self, text):
        return np.array([0, 1] if "蓝" in text else [1, 0], dtype=np.float32)


def synthetic_files(root, count=2):
    paths = []
    for index in range(count):
        path = root / f"image-{index}.png"
        Image.new("RGB", (80, 120), (255, 0, index * 80)).save(path)
        paths.append(path)
    group = {"source_kind": "material", "source_id": "group", "source_type_label": "素材管理",
             "name": "合成测试图", "path": str(root), "images": [{"path": str(path)} for path in paths]}
    return group, paths


def targets(root, count=2):
    return [{"task": SimpleNamespace(_full_task_name="红色主题" + str(i), task_name="截断标题", task_audio_text=""),
             "label": f"任务{i}", "target_dir": str(root / f"task{i}")} for i in range(count)]


class EngineTests(unittest.TestCase):
    def test_full_task_text_and_missing_text_not_derived_from_filename(self):
        value = engine.task_records([{"task": SimpleNamespace(_full_task_name="全文" * 200, task_name="短标题",
                                                               task_audio_text="speech"), "label": "admin-private"}])[0]
        self.assertEqual(value["text"], "全文" * 200)
        self.assertEqual(value["speech_text"], "speech")
        self.assertEqual(engine.task_records([{"label": "只含任务名"}])[0]["text"], "")

    def test_cache_changes_only_for_model_document_or_image_not_task_identity(self):
        task = {"text": "文案", "speech_text": "", "id": "T0", "label": "客户"}
        original = engine.cache_key("model", task, "image")
        self.assertEqual(original, engine.cache_key("model", {**task, "id": "T9", "label": "其他"}, "image"))
        self.assertNotEqual(original, engine.cache_key("other-model", task, "image"))
        self.assertNotEqual(original, engine.cache_key("model", {**task, "text": "新文案"}, "image"))
        self.assertNotEqual(original, engine.cache_key("model", task, "new-image"))

    def test_response_rejects_unknown_duplicate_and_non_numeric_scores(self):
        allowed = {("T0", "I0")}
        row = {"task_id": "T0", "image_id": "I0", **judgment()}
        for rows in ([{**row, "image_id": "C:/secret"}], [row, row], [{**row, "score": True}],
                     [{**row, "score": float("nan")}], [{**row, "status": "sure"}]):
            with self.assertRaises(ValueError):
                engine.parse_matches(json.dumps({"matches": rows}), allowed)
        self.assertEqual(engine.parse_matches('{"matches":[]}', allowed), {})

    def test_assignment_is_unique_and_can_reassign_for_scarce_choices(self):
        tasks = [{"id": "T0"}, {"id": "T1"}, {"id": "T2"}]
        matches = {("T0", "I0"): judgment(), ("T0", "I1"): judgment(score=80),
                   ("T1", "I0"): judgment(), ("T2", "I1"): judgment("uncertain", 99),
                   ("T2", "I2"): judgment(score=20)}
        choices = engine.recommended_choices(tasks, matches, 60)
        self.assertEqual(choices, {"T0": "I1", "T1": "I0"})

    def test_shortlist_distributes_alternatives_not_same_top_six_everywhere(self):
        tasks = [{"id": f"T{i}"} for i in range(8)]
        images = {f"I{i}": {} for i in range(24)}
        ranked = {task["id"]: list(images) for task in tasks}
        result = engine.shortlist(tasks, images, ranked, 6)
        self.assertTrue(all(len(value) == 6 for value in result.values()))
        self.assertEqual(len(set(image for values in result.values() for image in values)), 24)

    def test_incremental_subset_preserves_other_inventory_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group, paths = synthetic_files(root)
            index = ImageSearchIndex(root / "index")
            encoder = FakeEncoder()
            index.sync([group], encoder)
            subset = {**group, "images": [group["images"][0]]}
            result = index.sync([subset], encoder, prune_missing=False)
            self.assertEqual(result["removed"], 0)
            self.assertEqual(index.count(), 2)
            index.sync([subset], encoder)
            self.assertEqual(index.count(), 1)

    def test_analysis_is_read_only_and_cached_without_api_on_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group, paths = synthetic_files(root)
            tasks = engine.task_records(targets(root))
            cache, index = engine.MatchCache(root / "cache"), ImageSearchIndex(root / "index")
            def response(tasks, images, candidates, *_args):
                return {(task["id"], image_id): judgment() for task in tasks for image_id in candidates[task["id"]]}
            with patch.object(engine, "request_matches", side_effect=response) as request:
                first = engine.analyze([group], tasks, {}, {}, Mock(), root / "previews",
                                       encoder=FakeEncoder(), index=index, cache=cache)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(len(first["choices"]), 2)
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertFalse((root / "task0").exists())
            with patch.object(engine, "request_matches", side_effect=AssertionError("cache should be offline")):
                second = engine.analyze([group], tasks, {}, {}, Mock(), root / "previews",
                                        encoder=FakeEncoder(), index=index, cache=cache)
            self.assertEqual(second["choices"], first["choices"])
            self.assertGreater(second["cached_count"], 0)

    def test_api_failure_stops_new_calls_but_keeps_prior_and_later_cached_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group, _paths = synthetic_files(root)
            tasks = engine.task_records(targets(root, 6))
            cache, index = engine.MatchCache(root / "cache"), ImageSearchIndex(root / "index")
            def reply(batch, _images, candidates, *_args):
                return {(task["id"], image_id): judgment() for task in batch for image_id in candidates[task["id"]]}
            with patch.object(engine, "request_matches", side_effect=reply):
                engine.analyze([group], tasks, {}, {}, Mock(), root / "previews", encoder=FakeEncoder(), index=index, cache=cache)
            with cache.connect() as connection:
                for task in tasks[2:4]:
                    for path in (root / "image-0.png", root / "image-1.png"):
                        import hashlib
                        key = engine.cache_key(DEFAULT_MODEL, task, hashlib.sha256(engine.thumbnail_bytes(path)).hexdigest())
                        connection.execute("DELETE FROM matches WHERE key=?", (key,))
            with patch.object(engine, "request_matches", side_effect=RuntimeError("HTTP 503")) as request:
                result = engine.analyze([group], tasks, {}, {}, Mock(), root / "previews", encoder=FakeEncoder(), index=index, cache=cache)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(result["errors"])
            self.assertNotIn("T2", result["choices"])
            self.assertNotIn("T3", result["choices"])
            self.assertTrue(any(task_id == "T5" for task_id, _image_id in result["matches"]))

    def test_cancel_never_moves_and_missing_model_does_not_download(self):
        encoder = FakeEncoder()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(engine.Canceled):
                engine.analyze([], [], {}, {}, Mock(), directory, encoder=encoder,
                               index=Mock(), cache=Mock(), canceled=lambda: True)
            encoder.model_is_cached = lambda: False
            with self.assertRaisesRegex(RuntimeError, "先打开"):
                engine.analyze([], [], {}, {}, Mock(), directory, encoder=encoder, index=Mock(), cache=Mock())

    def test_retrieval_device_override_is_explicit_and_does_not_change_search_default(self):
        self.assertIsNone(ChineseImageEncoder().device)
        self.assertEqual(ChineseImageEncoder("base", device="cpu").device, "cpu")
        with self.assertRaises(ValueError):
            ChineseImageEncoder(device="other")

    def test_preview_preserves_portrait_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portrait.png"
            Image.new("RGB", (400, 1000), "red").save(path)
            with Image.open(io.BytesIO(engine.thumbnail_bytes(path))) as thumbnail:
                self.assertEqual(thumbnail.height, 512)
                self.assertAlmostEqual(thumbnail.width / thumbnail.height, .4, places=2)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.manager = GeminiKeyManager({"gemini_api_keys": ["secret-one", "secret-two"]})
        self.tasks = [{"id": "T0", "text": "红色图形", "speech_text": "", "label": "private-admin", "target_dir": "C:/private"}]
        self.images = [{"id": "I0", "jpeg": b"test jpeg", "path": "C:/private/image.png"}]
        self.candidates = {"T0": ["I0"]}

    def response(self):
        content = json.dumps({"matches": [{"task_id": "T0", "image_id": "I0", **judgment()}]})
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": content}]}}]}).encode()
        return response

    def request(self):
        return engine.request_matches(self.tasks, self.images, self.candidates, DEFAULT_MODEL, self.manager, Mock(), lambda: False)

    def test_only_selected_thumbnails_and_documents_sent_no_paths_or_keys_in_url(self):
        with patch("urllib.request.urlopen", return_value=self.response()) as request:
            result = self.request()
        self.assertTrue(result)
        outgoing = request.call_args.args[0]
        self.assertNotIn("secret", outgoing.full_url)
        self.assertIn(DEFAULT_MODEL, outgoing.full_url)
        text = outgoing.data.decode()
        self.assertNotIn("private-admin", text)
        self.assertNotIn("C:/private", text)
        self.assertIn("inline_data", text)
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_403_skips_key_and_success_uses_shared_broker(self):
        error = urllib.error.HTTPError("https://example.test", 403, "forbidden", {}, io.BytesIO(b'{}'))
        with patch("urllib.request.urlopen", side_effect=[error, self.response()]) as request:
            self.request()
        self.assertEqual(request.call_count, 2)
        self.assertFalse(self.manager.is_available("secret-one", DEFAULT_MODEL))

    def test_503_retries_once_not_once_for_every_key(self):
        errors = [urllib.error.HTTPError("https://example.test", 503, "busy", {}, io.BytesIO(b'{}')) for _ in range(2)]
        with patch("urllib.request.urlopen", side_effect=errors) as request, patch.object(engine.time, "sleep"), \
             patch.object(engine.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                self.request()
        self.assertEqual(request.call_count, 2)
        self.assertTrue(self.manager.is_available("secret-one", DEFAULT_MODEL))

    def test_canceled_request_makes_no_api_call(self):
        with patch("urllib.request.urlopen") as request:
            with self.assertRaises(engine.Canceled):
                engine.request_matches(self.tasks, self.images, self.candidates, DEFAULT_MODEL, self.manager, Mock(), lambda: True)
        request.assert_not_called()


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.group, self.paths = synthetic_files(self.root)
        self.context = SimpleNamespace(gemini_keys=GeminiKeyManager({"gemini_api_keys": ["test-key"]}),
                                       load_config=lambda: {}, log=Mock())
        self.store = Mock()
        self.dialog = SmartAssignmentDialog([self.group], targets(self.root), self.store, self.context)
        self.result = {"tasks": engine.task_records(targets(self.root)), "images": {},
                       "candidates": {"T0": ["I0", "I1"], "T1": ["I0", "I1"]},
                       "matches": {("T0", "I0"): judgment(), ("T1", "I1"): judgment("uncertain", 95)},
                       "choices": {"T0": "I0"}, "errors": [], "cached_count": 0}
        for i, path in enumerate(self.paths):
            stat = path.stat()
            self.result["images"][f"I{i}"] = {"path": str(path), "group_name": "素材", "preview": str(path),
                                                 "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def tearDown(self):
        self.assertFalse(self.dialog.is_busy())
        self.dialog.cleanup()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_preview_moves_nothing_uncertain_not_checked_and_user_can_override(self):
        self.dialog.on_analysis(self.result)
        self.assertEqual(len(self.dialog.assignments()), 1)
        self.store.move_material_images.assert_not_called()
        combo = self.dialog.table.cellWidget(1, 2)
        combo.setCurrentIndex(combo.findData("I1"))
        self.assertEqual(self.dialog.table.item(1, 0).checkState(), QtCore.Qt.Unchecked)
        self.dialog.table.item(1, 0).setCheckState(QtCore.Qt.Checked)
        self.assertEqual(len(self.dialog.assignments()), 2)
        self.assertIn("expected_mtime_ns", self.dialog.assignments()[0])

    def test_manual_duplicate_missing_and_changed_files_block_before_move(self):
        self.dialog.on_analysis(self.result)
        combo = self.dialog.table.cellWidget(1, 2)
        combo.setCurrentIndex(combo.findData("I0"))
        self.dialog.table.item(1, 0).setCheckState(QtCore.Qt.Checked)
        with self.assertRaisesRegex(ValueError, "多个任务"):
            self.dialog.assignments()
        self.dialog.table.item(1, 0).setCheckState(QtCore.Qt.Unchecked)
        self.paths[0].write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "更新"):
            self.dialog.assignments()
        self.paths[0].unlink()
        with self.assertRaisesRegex(ValueError, "移动或删除"):
            self.dialog.assignments()
        self.store.move_material_images.assert_not_called()

    def test_manual_picture_must_belong_to_selected_inventory(self):
        self.dialog.on_analysis(self.result)
        self.dialog.table.selectRow(0)
        with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(self.root / "outside.png"), "")), \
             patch.object(QtWidgets.QMessageBox, "warning") as warning:
            self.dialog.pick_other()
        warning.assert_called_once()
        self.assertEqual(len(self.dialog.assignments()), 1)

    def wait_worker(self):
        until = time.monotonic() + 5
        while self.dialog.is_busy() and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(self.dialog.is_busy())

    def test_move_runs_off_gui_thread_completion_and_logging_on_gui(self):
        self.dialog.on_analysis(self.result)
        called = []
        def move(assignments):
            called.append((threading.get_ident(), assignments))
            return {"moved": [{"task_label": "任务0"}]}
        self.store.move_material_images.side_effect = move
        gui = threading.get_ident()
        self.context.log.side_effect = lambda _text: self.assertEqual(threading.get_ident(), gui)
        self.dialog.start_move()
        self.wait_worker()
        self.assertNotEqual(called[0][0], gui)
        self.assertIsNotNone(self.dialog.move_result)
        self.assertEqual(self.dialog.result(), QtWidgets.QDialog.Accepted)

    def test_close_during_analysis_waits_without_destroying_running_thread(self):
        started = threading.Event()
        def analyze(_progress, canceled):
            started.set()
            while not canceled():
                time.sleep(.01)
            raise engine.Canceled()
        self.dialog._launch(analyze, self.dialog.on_analysis)
        self.assertTrue(started.wait(2))
        self.dialog.reject()
        self.assertTrue(self.dialog.is_busy())
        self.wait_worker()
        self.store.move_material_images.assert_not_called()

    def test_picker_selects_whole_entries_without_sequential_matching_claim(self):
        picker = GroupPicker([self.group], targets(self.root))
        try:
            picker.group_table.item(0, 0).setCheckState(QtCore.Qt.Checked)
            self.assertEqual(len(picker.selected_images()), 2)
            self.assertEqual(picker.selected_groups(), [self.group])
            self.assertEqual(picker.assign_button.text(), "分析并预览…")
            self.assertNotIn("可分配全部", picker.distribution_label.text())
        finally:
            picker.deleteLater()

    def test_settings_keep_custom_choice_and_use_lightweight_default(self):
        page = SmartAssignmentSettingsPage()
        self.assertEqual(page.model.currentText(), DEFAULT_MODEL)
        page.load_config({"smart_image_assignment": {"model": "custom", "future": "keep"}})
        result = page.update_config({"gemini_api_keys": ["unchanged"]})
        self.assertEqual(result["smart_image_assignment"]["future"], "keep")
        self.assertEqual(result["smart_image_assignment"]["model"], "custom")
        self.assertEqual(result["gemini_api_keys"], ["unchanged"])
        self.assertEqual(normalize_settings({"candidate_count": "invalid"})["candidate_count"], 6)
        page.widget.deleteLater()


class MoveSafetyTests(unittest.TestCase):
    def test_changed_file_version_refused_inside_inventory_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(root / "state.json", material_root=root / "library")
            group, paths = synthetic_files(root)
            material = store.add_material("测试素材", [str(paths[0])])
            source = Path(store.list_image_groups()[0]["images"][0]["path"])
            stat = source.stat()
            source.write_bytes(b"different content")
            assignment = {"path": str(source), "target_dir": str(root / "task"),
                          "expected_size": stat.st_size, "expected_mtime_ns": stat.st_mtime_ns}
            with self.assertRaisesRegex(ValueError, "核对后更新"):
                store.move_material_images([assignment])
            self.assertTrue(source.exists())
            self.assertFalse((root / "task").exists())


if __name__ == "__main__":
    unittest.main()
