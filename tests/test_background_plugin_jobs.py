import ast
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import inspect
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import (BackgroundTaskManager, BackgroundTaskHandle, TaskRunContext,
    BackgroundTaskResultError, directory_resource, directory_tree_resource, resources_conflict)
from model.InventoryManager import InventoryStore
from model.InventoryBackground import run_material_job, inventory_resources
from model.MaterialDriveSync import MaterialSyncStateStore, sync_material_drive_folder
from app_plugins.builtin.task_audio_subtitle import TaskAudioSubtitlePlugin, _AudioJobService, run_task_audio_job
from app_plugins.builtin.audio_splitter import AudioSplitterPlugin, run_audio_split_job
from PYUI.utility_managers_pyui import InventoryManagerDialog, MaterialCopyThread
from app_plugins.host import PluginContext


class Context(PluginContext):
    def __init__(self, manager, parent, targets=(), config=None):
        super().__init__(SimpleNamespace(main_window=parent, background_tasks=manager,
                                        is_enabled=lambda _plugin_id: True), "test-plugin")
        self.manager = manager
        self.targets, self.config = list(targets), copy.deepcopy(config or {})
        self.logs = []

    def load_config(self):
        return copy.deepcopy(self.config)

    def prepare_elevenlabs(self):
        from model.ElevenLabsService import ElevenLabsService
        if not hasattr(self, '_elevenlabs'): self._elevenlabs=ElevenLabsService()
        return self._elevenlabs.bind(self.config,'')

    def task_targets(self, _rows):
        return list(self.targets)

    def loaded_whisper_model_name(self):
        return "small"

    def log(self, message, *args, **kwargs):
        self.logs.append(message)

    def notify(self, *args, **kwargs):
        pass


class BackgroundPluginJobsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = QtWidgets.QWidget()
        self.manager = BackgroundTaskManager(max_parallel=3)
        self.context = Context(self.manager, self.window)

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                # A finished callback may schedule a coalesced metadata refresh.
                self.app.processEvents()
                if predicate():
                    return
            time.sleep(.003)
        self.assertTrue(predicate(), "background job timed out")

    def tearDown(self):
        self.manager.cancel_all()
        self.wait(lambda: not self.manager.is_busy())
        self.manager.shutdown()
        self.window.deleteLater()
        self.manager.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def submit_hold(self, resources=(), kind="network"):
        event = threading.Event()
        def hold(_payload, context):
            while not event.wait(.01):
                context.check_canceled()
        return self.context.submit_background_task(title="hold", runner=hold, payload={}, kind=kind, resources=resources), event

    def test_background_submissions_obey_real_plugin_context_signature(self):
        root = Path(os.environ.get('LZX_TEST_BASE_ROOT') or Path(inspect.getfile(PluginContext)).resolve().parents[1])
        overlay = Path(__file__).resolve().parents[1]
        signature = inspect.signature(PluginContext.submit_background_task)
        violations = []
        checked = 0
        for folder in ("PYUI", "app_plugins", "model"):
            for path in (root / folder).rglob("*.py"):
                relative = path.relative_to(root)
                source_path = overlay / relative if (overlay / relative).is_file() else path
                tree = ast.parse(source_path.read_text("utf-8-sig"), filename=str(relative))
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "submit_background_task"):
                        continue
                    checked += 1
                    try:
                        signature.bind(None, *([None] * len(node.args)),
                                       **{kw.arg: None for kw in node.keywords if kw.arg is not None})
                    except TypeError as error:
                        violations.append(f"{relative}:{node.lineno}: {error}")
        self.assertGreater(checked, 0, "no submission call sites were checked")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_test_context_does_not_replace_the_host_submission_contract(self):
        self.assertIs(self.context.submit_background_task.__func__, PluginContext.submit_background_task)
        with self.assertRaises(TypeError):
            self.context.submit_background_task("bad", lambda _p, _c: None, {})
        self.assertFalse(self.manager.is_busy())

    def test_directory_parent_locks_child_but_not_similar_sibling(self):
        parent = directory_tree_resource(self.root / "tasks")
        child = directory_resource(self.root / "tasks" / "1")
        sibling = directory_resource(self.root / "tasks-other")
        self.assertTrue(resources_conflict([parent], [child]))
        self.assertTrue(resources_conflict([child], [parent]))
        self.assertFalse(resources_conflict([parent], [sibling]))
        # A normal exporter writes only its own folder, not every child folder.
        self.assertFalse(resources_conflict([directory_resource(self.root / "tasks")], [child]))
        first, release = self.submit_hold([parent])
        second, _ = self.submit_hold([child])
        third, release3 = self.submit_hold([sibling])
        self.wait(lambda: self.manager.get(first.task_id)["status"] == "running")
        self.assertEqual(self.manager.get(second.task_id)["status"], "queued")
        self.assertEqual(self.manager.get(third.task_id)["status"], "running")
        second.requestInterruption()
        release.set()
        release3.set()
        self.wait(lambda: not self.manager.is_busy())

    def audio_plugin(self):
        task = SimpleNamespace(task_name="demo", task_audio_type="demo", task_audio_text="Ahoj svet.")
        jobs = [{"task": task, "label": "demo", "target_dir": str(self.root / "task"), "language": "sk"}]
        plugin = TaskAudioSubtitlePlugin()
        plugin.context = self.context
        plugin.audio_settings = {"demo": {"model": "edge", "sex": "female"}}
        plugin._target_jobs = lambda _rows: jobs
        plugin._confirm_audio_language = lambda *_args: True
        return plugin, jobs

    def test_audio_snapshot_queue_and_dedup_without_live_plugin_in_worker(self):
        plugin, jobs = self.audio_plugin()
        blocker, release = self.submit_hold([directory_resource(jobs[0]["target_dir"])])
        seen = []
        def create(service, task, path, progress):
            self.assertNotIsInstance(service.context, QtCore.QObject)
            seen.append((threading.get_ident(), task.task_audio_text))
            path.write_bytes(b"audio")
            return True
        with patch.object(_AudioJobService, "_create_audio", create):
            plugin.start_audio([0])
            plugin.start_audio([0])
            self.assertEqual(len(plugin._background_handles), 1)
            jobs[0]["task"].task_audio_text = "edited after submit"
            self.assertEqual(plugin.can_close(), (True, ""))
            release.set()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(seen[0][1], "Ahoj svet.")
        self.assertNotEqual(seen[0][0], threading.get_ident())
        self.assertTrue((self.root / "task/task_audio.mp3").is_file())
        self.assertFalse(plugin.is_running())

    def test_audio_batches_can_queue_and_waiting_cancel_does_not_write_files(self):
        plugin, jobs = self.audio_plugin()
        hold, release = self.submit_hold([directory_resource(self.root)])
        plugin.start_audio([0])
        handles = list(plugin._background_handles.values())
        jobs[0]["target_dir"] = str(self.root / "other")
        plugin.start_audio([1])
        self.assertEqual(len(plugin._background_handles), 2)
        handles[0].requestInterruption()
        self.assertEqual(len(plugin._background_handles), 1)
        self.assertFalse((self.root / "task").exists())
        for handle in list(plugin._background_handles.values()):
            handle.requestInterruption()
        release.set()

    def test_subtitle_receives_cancel_callback_and_loaded_model(self):
        payload = {"config": {}, "config_path": str(self.root / "config.json"), "audio_settings": {}, "model_name": "small"}
        canceled = threading.Event()
        context = TaskRunContext(canceled, lambda *_: None, lambda *_: None)
        service = _AudioJobService(payload, context)
        service._interrupted = context.is_canceled
        audio = self.root / "task_audio.mp3"
        audio.write_bytes(b"audio")
        service.context.whisper_model = lambda: "loaded-small"
        with patch("app_plugins.builtin.task_audio_subtitle.generate_srt_whisper_only") as generate:
            service._create_subtitle(audio, SimpleNamespace(task_audio_text="demo"), "sk", {})
        self.assertEqual(generate.call_args.kwargs["model"], "loaded-small")
        self.assertIs(generate.call_args.kwargs["cancelled"].__self__, context)

    def test_partial_audio_failure_retains_success_and_does_not_count_blank_voice(self):
        plugin, jobs = self.audio_plugin()
        payload = {"operation": "audio", "options": {}, "config": {}, "config_path": str(self.root / "config.json"),
                   "model_name": "small", "audio_settings": {}, "jobs": []}
        for i, voice in enumerate(("", "good", "bad")):
            payload["jobs"].append({"label": str(i), "target_dir": str(self.root / str(i)),
                "task": {"task_name": "demo", "task_audio_type": voice, "task_audio_text": "demo"}})
        def create(service, task, path, progress):
            if task.task_audio_type == "bad":
                raise ValueError("test API failure")
            path.write_bytes(b"ok")
            return True
        context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        with patch.object(_AudioJobService, "_create_audio", create), self.assertRaises(BackgroundTaskResultError) as error:
            run_task_audio_job(payload, context)
        self.assertEqual(error.exception.result["failed"], 1)
        self.assertEqual(error.exception.result["skipped_no_voice"], 1)
        self.assertEqual(len(error.exception.result["paths"]), 1)

    def test_material_window_can_close_and_host_retains_copy_result(self):
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        source = self.root / "picture.png"
        source.write_bytes(b"test-content")
        dialog = InventoryManagerDialog(store, context=self.context)
        dialog.material_copy_operation = {"kind": "material_add", "name": "demo"}
        hold, release = self.submit_hold(inventory_resources(store), "cpu")
        handle = dialog._launch_material_worker(MaterialCopyThread(store, "demo", [source], dialog))
        self.assertIsInstance(handle, BackgroundTaskHandle)
        dialog.close()
        self.assertTrue(handle.isRunning())
        release.set()
        self.wait(lambda: not self.manager.is_busy())
        self.assertIsNone(dialog.material_copy_thread)
        self.assertEqual(dialog.material_background_jobs, {})
        items = store.list_materials()
        self.assertEqual(len(items), 1)
        self.assertEqual((Path(items[0]["path"]) / source.name).read_bytes(), b"test-content")
        self.assertFalse(QtCore.QThread.currentThread() != self.app.thread())
        dialog.deleteLater()

    def test_inventory_reconstructs_store_and_does_not_change_input_payload(self):
        payload = {"state_path": str(self.root / "state.json"), "material_root": str(self.root / "library"),
                   "name": "demo", "source_paths": []}
        original = copy.deepcopy(payload)
        seen = []
        with patch("model.InventoryBackground.material_operation", side_effect=lambda store, p, progress:
                   seen.append(store) or {"path": str(self.root / "result")}) as operation:
            result = run_material_job(payload, TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None))
        self.assertIsInstance(seen[0], InventoryStore)
        self.assertEqual(payload, original)
        self.assertEqual(result["paths"], [str(self.root / "result")])

    def test_material_check_runs_off_gui_without_automatic_modal_result(self):
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        dialog = InventoryManagerDialog(store, context=self.context)
        seen = []
        with patch.object(InventoryStore, "check_materials", side_effect=lambda:
                seen.append(threading.get_ident()) or {"kept": [], "removed": []}), \
             patch.object(QtWidgets.QMessageBox, "information", side_effect=AssertionError("unexpected modal")):
            dialog.check_materials()
            handle = dialog.material_copy_thread
            self.assertIsInstance(handle, BackgroundTaskHandle)
            dialog.close()
            self.wait(lambda: not self.manager.is_busy())
        self.assertNotEqual(seen[0], threading.get_ident())
        self.assertTrue(handle.has_result_handler())
        dialog.deleteLater()

    def test_library_counts_are_loaded_in_background_and_sort_selection_survives(self):
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        source = self.root / "a.jpg"
        source.write_bytes(b"abc")
        item = store.add_material("demo", [source])
        seen = []
        def stats(path, cancelled=None):
            seen.append(threading.get_ident())
            return {"status": "正常", "file_count": 3, "size_bytes": 100}
        with patch("model.InventoryBackground.material_directory_stats", side_effect=stats), \
             patch("PYUI.utility_managers_pyui.material_directory_stats", side_effect=AssertionError("GUI must not walk files")):
            dialog = InventoryManagerDialog(store, context=self.context)
            dialog.material_table.selectRow(0)
            dialog.material_table.sortItems(6, QtCore.Qt.SortOrder.DescendingOrder)
            self.wait(lambda: dialog._material_stats.get(item["path"], {}).get("file_count") == 3)
            self.wait(lambda: not self.manager.is_busy())
            self.assertEqual(dialog.material_table.item(0, 3).text(), "3")
            self.assertEqual(dialog.material_table.currentRow(), 0)
            self.assertEqual(dialog.material_table.horizontalHeader().sortIndicatorSection(), 6)
        self.assertTrue(all(t != threading.get_ident() for t in seen))
        dialog.deleteLater()

    def test_canceled_directory_stats_does_not_publish_partial_counts(self):
        from model.InventoryBackground import run_material_stats_job
        event = threading.Event()
        def stats(*args, **kwargs):
            event.set()
            return {"status": "无法检查", "file_count": 0, "size_bytes": 0}
        from model.BackgroundTasks import BackgroundTaskCanceled
        with patch("model.InventoryBackground.material_directory_stats", side_effect=stats), self.assertRaises(BackgroundTaskCanceled):
            run_material_stats_job({"directories": [str(self.root)]},
                TaskRunContext(event, lambda *_: None, lambda *_: None))

    def test_audio_split_directory_scan_is_not_on_gui_thread(self):
        plugin = AudioSplitterPlugin()
        plugin.context = self.context
        seen = []
        with patch("app_plugins.builtin.audio_splitter.collect_audio_files", side_effect=lambda *_a, **_k:
                   seen.append(threading.get_ident()) or []):
            plugin.start_paths([str(self.root)])
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.get_ident())

    def test_audio_split_failure_preserves_other_outputs(self):
        outputs = self.root / "chunk.mp3"
        outputs.write_bytes(b"good")
        with patch("app_plugins.builtin.audio_splitter.collect_audio_files", return_value=[self.root / "a.mp3", self.root / "b.mp3"]), \
             patch("app_plugins.builtin.audio_splitter.split_audio_file", side_effect=[
                {"path": "a.mp3", "status": "split", "outputs": [str(outputs)]}, ValueError("bad audio")]), \
             self.assertRaises(BackgroundTaskResultError) as error:
            run_audio_split_job({"paths": [str(self.root)], "settings": {}},
                TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None))
        self.assertEqual(error.exception.result["paths"], [str(outputs)])
        self.assertTrue(outputs.is_file())

    def test_stitch_render_survives_close_and_can_cancel_queued(self):
        from app_plugins.builtin.video_stitch.ui import VideoStitchDialog
        dialog = VideoStitchDialog(self.context)
        hold, release = self.submit_hold([directory_resource(self.root)], "media")
        payload = {"left": "a.mp4", "right": "b.mp4", "output": str(self.root / "out.mp4"),
                   "basis": 0, "audio": "basis", "compatible": True}
        dialog._start(payload, True)
        handle = dialog._worker
        event = QtGui.QCloseEvent()
        dialog.closeEvent(event)
        self.assertTrue(handle.isRunning())
        self.assertEqual(self.manager.get(handle.task_id)["status"], "queued")
        dialog.cancel_work()
        self.assertIsNone(dialog._worker)
        self.assertFalse((self.root / "out.mp4").exists())
        release.set()
        dialog.deleteLater()

    def test_batch_host_queue_keeps_render_after_close(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store
        store = Store(self.root / "batch")
        state=store.load()
        source=self.root/'source.mp4';source.write_bytes(b'synthetic')
        state['jobs']=[{'id':'video','path':str(source),'title':'Title','body':'Body',
            'name':'video','status':'待生成','output':'','error':''}]
        store.save(state,state['revision'])
        dialog = BatchTextVideoDialog(self.context, store)
        from app_plugins.builtin.batch_text_video.background import batch_resources
        resources=batch_resources(store,dialog.state,['video'])
        hold, release = self.submit_hold(resources, "media")
        self.assertTrue(dialog._launch_batch(['video']))
        handle = dialog._host_plans[dialog.state['active_profile']]
        dialog.close()
        self.assertTrue(handle.isRunning())
        handle.requestInterruption()
        self.wait(lambda:not handle.isRunning())
        self.assertFalse(dialog._host_plans)
        release.set()
        dialog.deleteLater()

    def test_cancelled_sync_keeps_previous_presence_and_completed_download_record(self):
        from tests.test_material_drive_sync import _Service
        service = _Service()
        state_store = MaterialSyncStateStore(self.root / "sync.json")
        second = dict(service.item, id="file-2", name="two.mp4")
        event = threading.Event()
        settings = {"folder_url": "https://drive.google.com/drive/folders/root-1", "local_dir": str(self.root / "downloads")}
        state_store.save({"version": 1, "folder_id": "root-1", "local_root": str((self.root / "downloads").resolve()),
            "files": {"file-2": {"id": "file-2", "local_status": "present", "remote_present": True}}})
        def download(_service, item, local_root, **kwargs):
            target = Path(local_root) / item["name"]
            target.write_bytes(b"ok")
            event.set()
            return target
        with patch("model.MaterialDriveSync._collect_remote_files", return_value=[service.item, second]), \
             patch("model.MaterialDriveSync._download_item", side_effect=download):
            result = sync_material_drive_folder(settings, state_store=state_store,
                service_factory=lambda: service, cancelled=event.is_set)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["downloaded_count"], 1)
        self.assertEqual(state_store.load()["files"]["file-1"]["local_status"], "present")
        self.assertEqual(state_store.load()["files"]["file-2"]["local_status"], "present")

    def test_drive_sync_submits_through_real_context_and_can_cancel_before_network(self):
        from app_plugins.builtin.inventory import InventoryPlugin
        from model.MaterialDriveSync import update_material_sync_config
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        update_material_sync_config(self.context.config, {
            "enabled": True, "folder_url": "https://drive.google.com/drive/folders/root-1",
            "local_dir": str(self.root / "downloads"), "interval_minutes": 5,
        })
        plugin = InventoryPlugin.__new__(InventoryPlugin)
        plugin.context = self.context
        plugin.store = store
        plugin.material_sync_state_store = MaterialSyncStateStore(self.root / "sync.json")
        plugin.material_sync_thread = None
        plugin.material_sync_status_label = None
        _, release = self.submit_hold(inventory_resources(store), "network")
        with patch("app_plugins.builtin.inventory.is_offline", return_value=False), \
             patch("model.InventoryBackground.sync_material_drive_folder",
                   side_effect=AssertionError("canceled queued job must not use the network")):
            self.assertTrue(plugin.trigger_material_sync(force_ids=["file-1"]))
            handle = plugin.material_sync_thread
            self.assertIsInstance(handle, BackgroundTaskHandle)
            self.assertEqual(self.manager.get(handle.task_id)["status"], "queued")
            handle.requestInterruption()
            self.assertIsNone(plugin.material_sync_thread)
            release.set()
            self.wait(lambda: not self.manager.is_busy())

    def test_api_status_writes_from_many_threads_do_not_lose_keys_or_other_config(self):
        from model.ApiKeyHelper import record_api_key_status, api_key_id
        path = self.root / "config.json"
        original = {"gemini_api_keys": ["fake-configured-key"], "audio_settings": {"keep": {"model": "edge"}},
                    "business": {"tasks": [1, 2]}}
        path.write_text(json.dumps(original), encoding="utf-8")
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda i: record_api_key_status(path, f"unit-test-key-{i}", state="ok", used=i), range(40)))
        saved = json.loads(path.read_text("utf-8"))
        self.assertEqual(len(saved["elevenlabs_api_key_statuses"]), 40)
        for key, value in original.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved["elevenlabs_api_key_statuses"][api_key_id("unit-test-key-39")]["used"], 39)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_settings_save_keeps_latest_worker_status_and_user_settings(self):
        from model.ApiKeyHelper import record_api_key_status, api_key_id
        from PYUI.main_setting_pyui import _write_config_with_rolling_backups
        path = self.root / "config.json"
        stale = {"audio_settings": {"keep": {"model": "edge"}}, "gemini_api_keys": ["fake-key"]}
        path.write_text(json.dumps(stale), encoding="utf-8")
        record_api_key_status(path, "unit-test-key", state="exhausted", used=100)
        _write_config_with_rolling_backups(path, {**stale, "new_user_option": True})
        saved = json.loads(path.read_text("utf-8"))
        self.assertEqual(saved["elevenlabs_api_key_statuses"][api_key_id("unit-test-key")]["state"], "exhausted")
        self.assertEqual(saved["gemini_api_keys"], ["fake-key"])
        self.assertTrue(saved["new_user_option"])
        self.assertNotIn("elevenlabs_api_key_statuses", stale)

    def test_corrupt_config_is_not_overwritten_by_api_status(self):
        from model.ApiKeyHelper import record_api_key_status
        path = self.root / "config.json"
        path.write_text("corrupt-not-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            record_api_key_status(path, "unit-test-key", state="ok")
        self.assertEqual(path.read_text("utf-8"), "corrupt-not-json")

    def test_main_config_save_uses_same_transaction_lock_as_audio(self):
        from PYUI.main_pyui import MainDialog
        from model.ConfigTransactions import config_file_lock
        path = self.root / "config.json"
        observed = []
        window = SimpleNamespace(config_name=str(path))
        def save(*args):
            # Another thread must not acquire the lock during the save body.
            def attempt():
                lock = config_file_lock(path)
                acquired = lock.acquire(blocking=False)
                observed.append(acquired)
                if acquired:
                    lock.release()
            t = threading.Thread(target=attempt)
            t.start()
            t.join()
            return True
        window._save_current_config_locked = save
        self.assertTrue(MainDialog.saveCurrentConfig(window))
        self.assertEqual(observed, [False])


if __name__ == "__main__":
    unittest.main()
