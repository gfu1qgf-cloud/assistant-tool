"""
Adversarial Concurrency & Cancellation Stress Test Suite (Challenger 1).

Covers:
1. InventoryManager.py:
   - High-concurrency race condition testing across multiple threads for items, materials, and characters.
   - 2-phase transaction staging verification: no orphaned files or race on commit.
   - Collision-free atomic file replacement under high write contention.
   - Rollback verification upon write failure.
   - Multi-instance canonical path locking.
2. TaskSubmissionHelper.py:
   - High-volume concurrent audit log appending (30 threads, thousands of events).
   - JSON line integrity & no torn lines.
   - Batch os.fsync verification.
3. PYUI/main_pyui.py:
   - VideoAssignmentThread cooperative cancellation before start and during matching.
   - Rapid clicking & button state toggle ("分拣视频" -> "停止分拣" -> "正在停止…").
   - closeEvent cooperative timeout handling: clean close on quick interrupt vs. ignore/warn on slow/stuck thread.
"""

import concurrent.futures
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QThread

import model.InventoryManager as InventoryManagerMod
from model.InventoryManager import (
    InventoryStore,
    get_inventory_store_lock,
    material_directory_has_files,
)
import model.TaskSubmissionHelper as TaskSubmissionHelperMod
from model.TaskSubmissionHelper import (
    _AUDIT_LOG_LOCK,
    append_audit_events,
    get_audit_log_path,
)
from PYUI.main_pyui import MainDialog, VideoAssignmentThread


class FakeHotkeyManager(QtCore.QObject):
    activated = QtCore.pyqtSignal()

    def __init__(self, parent=None, hotkey_id=0):
        super().__init__(parent)
        self.sequence = None

    def register(self, shortcut):
        self.sequence = shortcut
        return True

    def close(self):
        self.sequence = None


def make_test_main_window(config_dict=None):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = {
        "video_match_ratio_threshold": 0.03,
        "task_path": "",
        "video_sorting_station_path": "",
    }
    if config_dict:
        cfg.update(config_dict)

    with patch("PYUI.main_pyui.GlobalHotkeyManager", FakeHotkeyManager), \
         patch("app_plugins.builtin.inventory.GlobalHotkeyManager", FakeHotkeyManager), \
         patch("app_plugins.builtin.chrome_launcher.GlobalHotkeyManager", FakeHotkeyManager), \
         patch("app_plugins.builtin.chrome_launcher.ChromeRunnerDialog.load_profiles", lambda self: None), \
         patch.object(MainDialog, "setupNotificationTray", lambda self: None), \
         patch.object(MainDialog, "saveCurrentConfig", lambda self: True), \
         patch.object(MainDialog, "load_config", lambda self: dict(cfg)):
        win = MainDialog()
        return win


# ============================================================================
# Suite 1: InventoryManager Concurrency & 2-Phase Staging
# ============================================================================
class TestInventoryConcurrencyStress(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="challenger_inv_")
        self.temp_path = Path(self.temp_dir)
        self.state_file = self.temp_path / "InventoryState.json"
        self.material_root = self.temp_path / "MaterialLibrary"
        self.store = InventoryStore(self.state_file, self.material_root)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_high_concurrency_add_stock_no_lost_updates(self):
        """30 concurrent threads incrementing stock on the same item. Verify zero lost updates."""
        item = self.store.add_item("TestStockItem", 100, 0)
        item_id = item["id"]

        num_threads = 30
        increments_per_thread = 20
        increment_amount = 5.0
        expected_final = 100 + (num_threads * increments_per_thread * increment_amount)

        barrier = threading.Barrier(num_threads)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                for _ in range(increments_per_thread):
                    self.store.add_stock(item_id, increment_amount)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        state = self.store.load()
        updated_item = next(i for i in state["items"] if i["id"] == item_id)
        self.assertAlmostEqual(
            updated_item["quantity"],
            expected_final,
            places=2,
            msg=f"Lost updates detected! Expected {expected_final}, got {updated_item['quantity']}",
        )

    def test_high_concurrency_add_item_distinct_names(self):
        """20 concurrent threads adding items with distinct names. All must persist."""
        num_threads = 20
        items_per_thread = 5
        total_items = num_threads * items_per_thread
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(thread_idx):
            try:
                barrier.wait(timeout=10)
                for i in range(items_per_thread):
                    name = f"Item_T{thread_idx}_N{i}"
                    self.store.add_item(name, 10 + i, 1.0)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t_idx,)) for t_idx in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        state = self.store.load()
        self.assertEqual(len(state["items"]), total_items)
        names = {i["name"] for i in state["items"]}
        self.assertEqual(len(names), total_items)

    def test_high_concurrency_add_item_name_collision(self):
        """25 threads trying to add the EXACT SAME item name concurrently.

        Exactly 1 must succeed; 24 must raise ValueError. No state corruption.
        """
        item_name = "CollidingItem"
        num_threads = 25
        barrier = threading.Barrier(num_threads)
        successes = []
        value_errors = []
        other_errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                res = self.store.add_item(item_name, 50, 2)
                successes.append(res)
            except ValueError as ve:
                value_errors.append(ve)
            except Exception as exc:
                other_errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(other_errors), 0, f"Unexpected errors: {other_errors}")
        self.assertEqual(len(successes), 1, f"Expected 1 success, got {len(successes)}")
        self.assertEqual(len(value_errors), num_threads - 1)

        state = self.store.load()
        matching = [i for i in state["items"] if i["name"] == item_name]
        self.assertEqual(len(matching), 1)

    def test_high_concurrency_mixed_reads_and_writes_atomic_replace(self):
        """10 readers and 10 writers running concurrently for 2 seconds.

        Ensures atomic file replacement prevents readers from observing torn/corrupt JSON.
        """
        item = self.store.add_item("BaseItem", 1000, 0)
        item_id = item["id"]
        stop_event = threading.Event()
        read_errors = []
        write_errors = []
        read_counts = [0]
        write_counts = [0]

        def reader():
            while not stop_event.is_set():
                try:
                    summary = self.store.summary()
                    self.assertIn("items", summary)
                    items = self.store.list_items()
                    self.assertTrue(len(items) >= 1)
                    read_counts[0] += 1
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.001)

        def writer(w_id):
            idx = 0
            while not stop_event.is_set():
                try:
                    self.store.add_stock(item_id, 1)
                    if idx % 5 == 0:
                        new_item = self.store.add_item(f"W_{w_id}_{idx}", 10, 0)
                        self.store.delete_item(new_item["id"])
                    idx += 1
                    write_counts[0] += 1
                except Exception as exc:
                    write_errors.append(exc)
                time.sleep(0.001)

        readers = [threading.Thread(target=reader) for _ in range(10)]
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(10)]

        for t in readers + writers:
            t.start()

        time.sleep(2.0)
        stop_event.set()

        for t in readers + writers:
            t.join(timeout=10)

        self.assertEqual(len(read_errors), 0, f"Read errors (possible torn JSON): {read_errors}")
        self.assertEqual(len(write_errors), 0, f"Write errors: {write_errors}")
        self.assertGreater(read_counts[0], 50)
        self.assertGreater(write_counts[0], 50)

    def test_materials_high_concurrency_add_material_no_orphaned_staging(self):
        """10 threads adding different materials concurrently.

        Verifies 2-phase staging cleans up all temporary folders.
        """
        # Prepare source files
        source_dir = self.temp_path / "sources"
        source_dir.mkdir()
        for i in range(10):
            f = source_dir / f"test_img_{i}.png"
            f.write_bytes(b"dummy_png_data")

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(idx):
            try:
                barrier.wait(timeout=10)
                src = source_dir / f"test_img_{idx}.png"
                self.store.add_material(f"Material_{idx}", [str(src)])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Errors adding materials: {errors}")

        # Verify all materials present
        state = self.store.load()
        self.assertEqual(len(state["materials"]), num_threads)

        # Verify NO orphaned .*.tmp staging directories exist in material_root
        all_entries = list(self.material_root.iterdir())
        tmp_entries = [p for p in all_entries if p.name.endswith(".tmp") or p.name.startswith(".")]
        self.assertEqual(
            len(tmp_entries),
            0,
            f"Orphaned staging directories found in material_root: {tmp_entries}",
        )

    def test_materials_add_material_name_collision_staging_cleanup(self):
        """10 threads concurrently trying to add material with the SAME name.

        Exactly 1 must succeed. All failing threads must cleanly delete their staging dirs.
        """
        source_dir = self.temp_path / "colliding_sources"
        source_dir.mkdir()
        f = source_dir / "asset.png"
        f.write_bytes(b"asset_data")

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        successes = []
        value_errors = []
        other_errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                res = self.store.add_material("SameMaterialName", [str(f)])
                successes.append(res)
            except ValueError as ve:
                value_errors.append(ve)
            except Exception as exc:
                other_errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(other_errors), 0, f"Unexpected errors: {other_errors}")
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(value_errors), num_threads - 1)

        # Check material_root for any orphaned temporary staging dirs
        all_entries = list(self.material_root.iterdir())
        tmp_entries = [p for p in all_entries if p.name.startswith(".") or p.name.endswith(".tmp")]
        self.assertEqual(
            len(tmp_entries),
            0,
            f"Orphaned staging directories left by aborted threads: {tmp_entries}",
        )

    def test_materials_append_concurrency(self):
        """Multiple threads concurrently appending files to the same material."""
        source_dir = self.temp_path / "append_sources"
        source_dir.mkdir()
        init_file = source_dir / "init.png"
        init_file.write_bytes(b"initial_data")

        mat = self.store.add_material("TargetMat", [str(init_file)])
        mat_id = mat["id"]

        num_threads = 8
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(idx):
            try:
                sub_file = source_dir / f"append_{idx}.png"
                sub_file.write_bytes(f"data_{idx}".encode("utf-8"))
                barrier.wait(timeout=10)
                self.store.append_material(mat_id, [str(sub_file)])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Append errors: {errors}")
        state = self.store.load()
        target_mat = next(m for m in state["materials"] if m["id"] == mat_id)
        self.assertEqual(target_mat["source_count"], 1 + num_threads)

        # Verify no .append-*.tmp folders remained
        tmp_entries = [
            p for p in self.material_root.iterdir()
            if p.name.startswith(".") or p.name.endswith(".tmp")
        ]
        self.assertEqual(len(tmp_entries), 0, f"Orphaned append temp dirs: {tmp_entries}")

    def test_material_rollback_on_write_failure(self):
        """Simulate disk/write failure during commit. Verify complete rollback."""
        source_dir = self.temp_path / "fail_sources"
        source_dir.mkdir()
        f = source_dir / "rollback_test.png"
        f.write_bytes(b"rollback_data")

        with patch.object(self.store, "_write", side_effect=OSError("Simulated Disk Full")):
            with self.assertRaises(OSError):
                self.store.add_material("FailedMaterial", [str(f)])

        # Verify no material added to state
        state = self.store.load()
        self.assertEqual(len(state["materials"]), 0)

        # Verify no orphaned folders in material_root (neither final_dir nor staging_dir)
        remaining = list(self.material_root.iterdir())
        self.assertEqual(len(remaining), 0, f"Remaining unrolled-back directories: {remaining}")

    def test_canonical_path_locking_across_instances(self):
        """Different InventoryStore instances referencing the same path must share lock."""
        path_str = str(self.state_file)
        store1 = InventoryStore(path_str)
        store2 = InventoryStore(path_str.lower() if os.name == 'nt' else path_str)
        self.assertIs(store1.lock, store2.lock)


# ============================================================================
# Suite 2: TaskSubmissionHelper Concurrency & os.fsync
# ============================================================================
class TestTaskSubmissionAuditConcurrencyStress(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="challenger_audit_")
        self.log_file = Path(self.temp_dir) / "sub" / "TaskSubmissionLog.jsonl"
        self.config = {"task_submission_log_file": str(self.log_file)}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_high_concurrency_audit_log_append_integrity(self):
        """30 concurrent threads appending batches of audit events.

        Total 3,000 events. Verify every single line is valid JSON, exactly 3,000 events,
        no interleaved lines or torn lines.
        """
        num_threads = 30
        batches_per_thread = 10
        events_per_batch = 10
        total_events = num_threads * batches_per_thread * events_per_batch

        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(t_idx):
            try:
                barrier.wait(timeout=10)
                for b_idx in range(batches_per_thread):
                    batch = [
                        {
                            "id": str(uuid.uuid4()),
                            "thread_id": t_idx,
                            "batch_id": b_idx,
                            "event_idx": e_idx,
                            "timestamp": datetime.now().isoformat(),
                            "content": f"Audit entry payload with symbols: 漢字 / ⚡ / \\n / \"quotes\" {e_idx}",
                        }
                        for e_idx in range(events_per_batch)
                    ]
                    append_audit_events(self.config, batch)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Append errors: {errors}")
        self.assertTrue(self.log_file.exists())

        # Inspect all lines in log
        lines = self.log_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), total_events, f"Expected {total_events} lines, found {len(lines)}")

        seen_ids = set()
        for idx, line in enumerate(lines):
            try:
                obj = json.loads(line)
                self.assertIn("id", obj)
                seen_ids.add(obj["id"])
            except Exception as err:
                self.fail(f"Torn line at index {idx}: {line[:100]}... Error: {err}")

        self.assertEqual(len(seen_ids), total_events)

    def test_audit_log_batch_fsync_retention(self):
        """Verify batch os.fsync is strictly executed for each append call."""
        fsync_calls = []
        original_fsync = os.fsync

        def spy_fsync(fd):
            fsync_calls.append(fd)
            return original_fsync(fd)

        with patch("os.fsync", side_effect=spy_fsync):
            append_audit_events(self.config, [{"event": 1}, {"event": 2}])
            append_audit_events(self.config, [{"event": 3}])

        self.assertEqual(len(fsync_calls), 2, "os.fsync must be called once per append call")

    def test_audit_log_parent_dir_creation_race(self):
        """Verify concurrent appends cleanly handle newly created nested directory."""
        nested_log = Path(self.temp_dir) / "new_dir_1" / "new_dir_2" / "log.jsonl"
        cfg = {"task_submission_log_file": str(nested_log)}

        num_threads = 15
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(t_idx):
            try:
                barrier.wait(timeout=10)
                append_audit_events(cfg, [{"thread": t_idx}])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        lines = nested_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), num_threads)


# ============================================================================
# Suite 3: VideoAssignmentThread Interactive Cancellation & UI Safety
# ============================================================================
class TestVideoAssignmentInteractiveCancellation(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.temp_dir = tempfile.mkdtemp(prefix="challenger_ui_")
        self.temp_path = Path(self.temp_dir)
        self.today_dir = self.temp_path / "today"
        self.today_dir.mkdir()
        self.video_root = self.temp_path / "videos"
        self.video_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_thread_cancellation_before_start(self):
        """Calling requestInterruption() before start() terminates cooperatively at start."""
        thread = VideoAssignmentThread(
            today_dir=str(self.today_dir),
            video_root_dir=str(self.video_root),
        )
        logs = []
        completed = []
        thread.log.connect(logs.append)
        thread.completed.connect(lambda m, t: completed.append((m, t)))

        thread.requestInterruption()
        self.assertTrue(thread.isInterruptionRequested())

        thread.start()
        thread.wait(3000)
        QtWidgets.QApplication.processEvents()

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0], (0, 0))
        self.assertTrue(any("取消" in l for l in logs))

    def test_thread_cooperative_cancellation_during_matching(self):
        """Simulate multiple video files; interrupt mid-run and verify cooperative stop."""
        # Create dummy image in today_dir
        (self.today_dir / "target.png").write_bytes(b"dummy")

        # Create 10 dummy video files
        for i in range(10):
            (self.video_root / f"vid_{i}.mp4").write_bytes(b"dummy_vid")

        thread = VideoAssignmentThread(
            today_dir=str(self.today_dir),
            video_root_dir=str(self.video_root),
        )

        completed = []
        logs = []
        thread.completed.connect(lambda m, t: completed.append((m, t)))
        thread.log.connect(logs.append)

        processed_count = [0]

        def fake_scan(d, m):
            return [{"name": "target.png", "folder": str(self.today_dir), "desc": object()}]

        def fake_frame(fp):
            processed_count[0] += 1
            if processed_count[0] == 2:
                # Interrupt cooperatively on the 2nd video
                thread.requestInterruption()
            time.sleep(0.01)
            return object()

        with patch("model.VideoHelper.FeatureMatcher.scan_images_recursively", side_effect=fake_scan), \
             patch("model.VideoHelper.FeatureMatcher.get_video_frame_clean", side_effect=fake_frame), \
             patch("model.VideoHelper.FeatureMatcher.get_features", return_value=(None, object())), \
             patch("model.VideoHelper.FeatureMatcher.match", return_value=0.0):

            thread.start()
            thread.wait(5000)
            QtWidgets.QApplication.processEvents()

        self.assertEqual(len(completed), 1)
        # Should have stopped early, well before 10 videos
        self.assertLess(processed_count[0], 10)
        self.assertTrue(any("中止" in l for l in logs))

    def test_main_window_rapid_clicks_toggle_interruption(self):
        """Simulate rapid UI button clicks on assignVideo().

        Click 1: starts thread, button becomes '停止分拣', enabled=True.
        Click 2: calls requestInterruption(), button becomes '正在停止…', enabled=False.
        Click 3+: idempotent, does not spawn duplicate threads.
        On finish: button reset to '分拣视频', enabled=True.
        """
        win = make_test_main_window()
        try:
            # Configure valid paths
            win.getTodayDir = lambda: str(self.today_dir)
            with patch("globalValue.globalValue.videoSortingStationPath", return_value=str(self.video_root)):
                # Mock worker to run briefly
                with patch.object(VideoAssignmentThread, "run") as mock_run:
                    # Let thread simulate a short sleep
                    def delayed_run(t_self):
                        time.sleep(0.2)
                        t_self.completed.emit(1, 1)

                    mock_run.side_effect = lambda: delayed_run(win.assign_video_thread)

                    # 1st click -> Starts thread
                    t1 = win.assignVideo()
                    self.assertIsNotNone(t1)
                    self.assertTrue(t1.isRunning())
                    self.assertEqual(win.assign_video_btn.text(), "停止分拣")
                    self.assertTrue(win.assign_video_btn.isEnabled())

                    # 2nd click -> Requests interruption
                    t2 = win.assignVideo()
                    self.assertIs(t1, t2)
                    self.assertTrue(t1.isInterruptionRequested())
                    self.assertEqual(win.assign_video_btn.text(), "正在停止…")
                    self.assertFalse(win.assign_video_btn.isEnabled())

                    # 3rd click -> idempotent
                    t3 = win.assignVideo()
                    self.assertIs(t1, t3)

                    # Wait for thread completion
                    t1.wait(3000)
                    QtWidgets.QApplication.processEvents()

                    # Button restored
                    self.assertTrue(win.assign_video_btn.isEnabled())
                    self.assertEqual(win.assign_video_btn.text(), "分拣视频")
                    self.assertIsNone(win.assign_video_thread)
        finally:
            win.close()

    def test_close_event_cooperative_wait_success(self):
        """closeEvent interrupts running VideoAssignmentThread; clean shutdown if thread exits within 2s."""
        win = make_test_main_window()
        try:
            thread = VideoAssignmentThread(str(self.today_dir), str(self.video_root), parent=win)
            win.assign_video_thread = thread

            def cooperative_run():
                while not thread.isInterruptionRequested():
                    time.sleep(0.01)

            thread.run = cooperative_run
            thread.start()

            event = QtGui.QCloseEvent()
            win.closeEvent(event)

            self.assertTrue(event.isAccepted(), "closeEvent should accept when thread exits cooperatively")
            self.assertFalse(thread.isRunning())
        finally:
            win.close()

    def test_close_event_timeout_wait_failure(self):
        """closeEvent ignores close and warns user if thread does not stop within 2s."""
        win = make_test_main_window()
        try:
            # This test only verifies the closeEvent decision.  A real QThread is
            # unnecessary here and can make Qt tear down a native thread object
            # while its methods are monkey-patched on headless CI runners.
            thread = MagicMock()
            thread.isRunning.return_value = True
            thread.wait.return_value = False
            win.assign_video_thread = thread

            event = QtGui.QCloseEvent()
            with patch("PyQt5.QtWidgets.QMessageBox.warning") as mock_warn:
                win.closeEvent(event)
                self.assertFalse(event.isAccepted(), "closeEvent must be ignored when wait() times out")
                thread.requestInterruption.assert_called_once_with()
                thread.wait.assert_called_once_with(2000)
                mock_warn.assert_called_once()
                self.assertIn("视频分拣仍在运行", mock_warn.call_args[0][1])
        finally:
            win.assign_video_thread = None
            win.close()


if __name__ == "__main__":
    unittest.main()
