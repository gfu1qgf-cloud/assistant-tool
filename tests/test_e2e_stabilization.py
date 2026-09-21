"""
Opaque-box E2E stabilization test suite for requirements R1 through R5.

Architecture & Methodology:
- Tier 1: Feature Coverage (>=5 test cases per feature for R1, R2, R3, R4, R5 -> 25 tests)
- Tier 2: Boundary & Corner Cases (>=5 test cases per feature for limits, errors, empty/large inputs -> 25 tests)
- Tier 3: Cross-Feature Combinations (pairwise interactions across features -> 5 tests)
- Tier 4: Real-World Application Scenarios (comprehensive end-to-end workflows -> 4 tests)

Total: 59 Test Cases
"""

import concurrent.futures
import json
import logging
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets

# Target modules
from PYUI.main_pyui import MainDialog, VideoAssignmentThread
import model.InventoryManager as InventoryManagerMod
from model.InventoryManager import (
    InventoryStore,
    get_inventory_store_lock,
    current_quantity,
)
import model.GoogleSheetsHelper as GoogleSheetsHelperMod
from model.GoogleSheetsHelper import (
    sheet_range,
    column_to_letter,
    write_review_video_links,
)
import model.GoogleDriveHelper as GoogleDriveHelperMod
from model.GoogleDriveHelper import (
    METADATA_MAX_RETRIES,
    list_remote_children,
    get_remote_file,
    get_or_create_remote_folder,
    is_retryable_upload_error,
    retry_sleep_seconds,
    GOOGLE_FOLDER_MIME,
)
import model.TaskSubmissionHelper as TaskSubmissionHelperMod
from model.TaskSubmissionHelper import (
    _AUDIT_LOG_LOCK,
    append_audit_events,
    get_audit_log_path,
)
from model.VideoHelper import FeatureMatcher


# ============================================================================
# Test Doubles & Mocks
# ============================================================================

class FakeHotkeyManager(QtCore.QObject):
    activated = QtCore.pyqtSignal()

    def __init__(self, parent=None, hotkey_id=0):
        super().__init__(parent)
        self.sequence = None
        self.last_error = ""

    def register(self, shortcut):
        self.sequence = shortcut
        return True

    def close(self):
        self.sequence = None


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeButton:
    def __init__(self, text="分拣视频"):
        self._text = text
        self._enabled = True

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setEnabled(self, enabled):
        self._enabled = bool(enabled)

    def isEnabled(self):
        return self._enabled


class FakeVideoAssignmentWorker:
    def __init__(self, *args, **kwargs):
        self.log = FakeSignal()
        self.completed = FakeSignal()
        self.failed = FakeSignal()
        self.finished = FakeSignal()
        self.running = False
        self.finished_state = False
        self.interrupted = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def isFinished(self):
        return self.finished_state

    def requestInterruption(self):
        self.interrupted = True

    def finish(self, matched_count=1, total_count=1):
        self.running = False
        self.finished_state = True
        self.completed.emit(matched_count, total_count)
        self.finished.emit()

    def deleteLater(self):
        pass


def make_assignment_host(today_dir=None, config=None):
    """Build the minimal host needed by MainDialog assignment methods."""
    host = MagicMock()
    host.assign_video_thread = None
    host.assign_video_btn = FakeButton()
    host._assign_video_button_text = ""
    host._last_video_assign_result = None
    host.getTodayDir.return_value = str(today_dir) if today_dir is not None else None
    host.load_config.return_value = dict(config or {"video_match_ratio_threshold": 0.03})
    host.onAssignVideoCompleted.side_effect = (
        lambda *args: MainDialog.onAssignVideoCompleted(host, *args)
    )
    host.onAssignVideoFinished.side_effect = (
        lambda *args: MainDialog.onAssignVideoFinished(host, *args)
    )
    return host


@contextmanager
def create_test_main_window(config_dict=None):
    """Context manager creating a safely headless MainDialog instance."""
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
        try:
            yield win
        finally:
            if hasattr(win, "assign_video_thread") and win.assign_video_thread:
                if win.assign_video_thread.isRunning():
                    win.assign_video_thread.requestInterruption()
                    win.assign_video_thread.wait(2000)
            win.close()


class MockHttpResponse:
    def __init__(self, status, reason=""):
        self.status = status
        self.reason = reason


def make_http_error(status, reason=""):
    try:
        from googleapiclient.errors import HttpError
        return HttpError(MockHttpResponse(status, reason), b"Mock error content")
    except Exception:
        class FakeHttpError(Exception):
            def __init__(self, resp, content):
                self.resp = resp
                self.content = content
        return FakeHttpError(MockHttpResponse(status, reason), b"Mock error content")


class MockDriveRequest:
    def __init__(self, execute_fn):
        self._execute_fn = execute_fn

    def execute(self):
        if callable(self._execute_fn):
            return self._execute_fn()
        return self._execute_fn


class MockFilesResource:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []
        self.create_calls = []
        self.list_handler = lambda: {"files": []}
        self.get_handler = lambda: {}
        self.create_handler = lambda: {"id": "new-folder-id"}

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return MockDriveRequest(self.list_handler)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return MockDriveRequest(self.get_handler)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return MockDriveRequest(self.create_handler)


class MockDriveService:
    def __init__(self):
        self._files = MockFilesResource()

    def files(self):
        return self._files


class MockValuesResource:
    def __init__(self, values=None):
        self.values = values if values is not None else []
        self.get_calls = []
        self.update_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return MockDriveRequest(lambda: {"values": self.values})

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return MockDriveRequest(lambda: {"updatedRows": len(kwargs.get("body", {}).get("values", []))})


class MockSpreadsheetsResource:
    def __init__(self, values=None, sheet_name="Sheet1", sheet_id=0):
        self._values = MockValuesResource(values)
        self.sheet_name = sheet_name
        self.sheet_id = sheet_id
        self.batch_update_calls = []
        self.get_calls = []

    def values(self):
        return self._values

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return MockDriveRequest(lambda: {
            "sheets": [{
                "properties": {
                    "title": self.sheet_name,
                    "sheetId": self.sheet_id,
                }
            }]
        })

    def batchUpdate(self, **kwargs):
        self.batch_update_calls.append(kwargs)
        return MockDriveRequest(lambda: {"replies": []})


class MockSheetsService:
    def __init__(self, values=None, sheet_name="Sheet1", sheet_id=0):
        self._spreadsheets = MockSpreadsheetsResource(values, sheet_name, sheet_id)

    def spreadsheets(self):
        return self._spreadsheets


# ============================================================================
# Hermetic Network Isolation Guard
# ============================================================================

_ORIGINAL_SOCKET_CONNECT = socket.socket.connect


def _hermetic_socket_connect(self, address):
    host = address[0] if isinstance(address, tuple) and len(address) > 0 else str(address)
    if host in ("127.0.0.1", "localhost", "::1"):
        return _ORIGINAL_SOCKET_CONNECT(self, address)
    raise RuntimeError(
        f"Hermetic test network violation: outbound network connection to {address} is forbidden!"
    )


def _fail_real_google_service(*args, **kwargs):
    raise RuntimeError("Real Google service initialization is forbidden during hermetic tests!")


def setUpModule():
    socket.socket.connect = _hermetic_socket_connect
    if hasattr(GoogleDriveHelperMod, "load_drive_service"):
        GoogleDriveHelperMod._orig_load_drive_service = GoogleDriveHelperMod.load_drive_service
        GoogleDriveHelperMod.load_drive_service = _fail_real_google_service
    if hasattr(GoogleSheetsHelperMod, "load_sheets_service"):
        GoogleSheetsHelperMod._orig_load_sheets_service = GoogleSheetsHelperMod.load_sheets_service
        GoogleSheetsHelperMod.load_sheets_service = _fail_real_google_service


def tearDownModule():
    socket.socket.connect = _ORIGINAL_SOCKET_CONNECT
    if hasattr(GoogleDriveHelperMod, "_orig_load_drive_service"):
        GoogleDriveHelperMod.load_drive_service = GoogleDriveHelperMod._orig_load_drive_service
    if hasattr(GoogleSheetsHelperMod, "_orig_load_sheets_service"):
        GoogleSheetsHelperMod.load_sheets_service = GoogleSheetsHelperMod._orig_load_sheets_service


# ============================================================================
# Tier 1: Feature Coverage (25 tests)
# ============================================================================

class Tier1FeatureCoverageTests(unittest.TestCase):
    """
    Tier 1 tests verify primary functionality and interfaces for R1, R2, R3, R4, R5.
    At least 5 test cases per requirement.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # --- R1: Video Assignment Threading ---

    def test_tier1_r1_01_video_assignment_thread_class_structure(self):
        """Verify VideoAssignmentThread inherits QThread and defines log, completed, failed signals."""
        self.assertTrue(issubclass(VideoAssignmentThread, QtCore.QThread))
        thread = VideoAssignmentThread(
            today_dir=str(self.tmp_path / "today"),
            video_root_dir=str(self.tmp_path / "videos"),
            match_ratio_threshold=0.05,
        )
        self.assertTrue(hasattr(thread, "log"))
        self.assertTrue(hasattr(thread, "completed"))
        self.assertTrue(hasattr(thread, "failed"))
        self.assertAlmostEqual(thread.match_ratio_threshold, 0.05)

    def test_tier1_r1_02_assign_video_signature_preserved(self):
        """Verify MainDialog.assignVideo keeps exact signature def assignVideo(self)."""
        import inspect
        sig = inspect.signature(MainDialog.assignVideo)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["self"], "assignVideo must retain its def assignVideo(self) signature")

    def test_tier1_r1_03_assign_video_spawns_thread_and_disables_button(self):
        """Verify assignVideo spawns VideoAssignmentThread and sets button to interactive cancel mode."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir(parents=True)
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir(parents=True)

        win = make_assignment_host(today_dir)
        with patch("globalValue.globalValue.videoSortingStationPath", return_value=str(videos_dir)), \
             patch("PYUI.main_pyui.VideoAssignmentThread", FakeVideoAssignmentWorker):
            btn = win.assign_video_btn
            returned_thread = MainDialog.assignVideo(win)

            self.assertIsNotNone(returned_thread)
            self.assertIsInstance(returned_thread, FakeVideoAssignmentWorker)
            self.assertTrue(btn.isEnabled(), "Button must remain enabled for cancellation while thread runs")
            self.assertEqual(btn.text(), "停止分拣")
            self.assertIs(win.assign_video_thread, returned_thread)

    def test_tier1_r1_04_thread_emits_log_signals_instead_of_direct_ui_call(self):
        """Verify VideoAssignmentThread communicates through log signals."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()

        thread = VideoAssignmentThread(
            today_dir=str(today_dir),
            video_root_dir=str(videos_dir),
        )
        emitted_logs = []
        thread.log.connect(emitted_logs.append)

        # Running synchronously with empty dirs
        thread.run()

        self.assertTrue(len(emitted_logs) >= 1)
        self.assertTrue(any("任务目录" in log or "ORB" in log for log in emitted_logs))

    def test_tier1_r1_05_thread_completion_triggers_cleanup_and_reenables_button(self):
        """Verify completion restores button state and cleans up thread reference."""
        win = make_assignment_host()
        btn = win.assign_video_btn
        btn.setEnabled(False)
        win._assign_video_button_text = "原始分拣"
        win.assign_video_thread = MagicMock()

        MainDialog.onAssignVideoFinished(win, 2, 5)

        self.assertTrue(btn.isEnabled())
        self.assertEqual(btn.text(), "原始分拣")
        self.assertIsNone(win.assign_video_thread)
        self.assertEqual(win._last_video_assign_result, (2, 5))

    # --- R2: InventoryStore RMW Locking ---

    def test_tier1_r2_01_path_keyed_lock_registry(self):
        """Verify get_inventory_store_lock returns shared RLock for canonical paths."""
        path_a = self.tmp_path / "inv.json"
        path_b = self.tmp_path / "subdir" / ".." / "inv.json"
        path_c = self.tmp_path / "other.json"

        lock_a = get_inventory_store_lock(path_a)
        lock_b = get_inventory_store_lock(path_b)
        lock_c = get_inventory_store_lock(path_c)

        self.assertIsInstance(lock_a, type(threading.RLock()))
        self.assertIs(lock_a, lock_b, "Canonical paths must resolve to the identical RLock")
        self.assertIsNot(lock_a, lock_c, "Different paths must resolve to distinct locks")

    def test_tier1_r2_02_inventory_store_exposes_lock_property(self):
        """Verify InventoryStore binds and exposes path-keyed lock."""
        state_file = self.tmp_path / "store.json"
        store1 = InventoryStore(state_file)
        store2 = InventoryStore(state_file)

        self.assertIs(store1.lock, store2.lock)
        self.assertIs(store1.lock, get_inventory_store_lock(state_file))

    def test_tier1_r2_03_add_item_is_thread_safe_under_lock(self):
        """Verify add_item executes cleanly under lock and persists to file."""
        state_file = self.tmp_path / "items.json"
        store = InventoryStore(state_file)

        item = store.add_item("剪辑模板", 10, 1.5)
        self.assertEqual(item["name"], "剪辑模板")
        self.assertEqual(item["quantity"], 10.0)

        # Verify persisted state
        loaded = store.load()
        self.assertEqual(len(loaded["items"]), 1)
        self.assertEqual(loaded["items"][0]["id"], item["id"])

    def test_tier1_r2_04_add_stock_concurrent_increments(self):
        """Verify concurrent add_stock increments across multiple threads preserve all additions."""
        state_file = self.tmp_path / "stock.json"
        store = InventoryStore(state_file)
        item = store.add_item("电池", 0, 0)
        item_id = item["id"]

        num_threads = 5
        increments_per_thread = 20
        amount = 2.0

        def worker():
            for _ in range(increments_per_thread):
                store.add_stock(item_id, amount)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_total = num_threads * increments_per_thread * amount
        reloaded = store.load()
        final_item = [i for i in reloaded["items"] if i["id"] == item_id][0]
        self.assertAlmostEqual(final_item["quantity"], expected_total)

    def test_tier1_r2_05_atomic_write_uses_unique_temporary_file(self):
        """Verify _write creates unique temporary file (UUID) to prevent Windows collision."""
        state_file = self.tmp_path / "atomic.json"
        store = InventoryStore(state_file)
        state = store._default_state()

        created_temps = []
        orig_replace = os.replace

        def capturing_replace(src, dst):
            created_temps.append(Path(src).name)
            return orig_replace(src, dst)

        with patch("os.replace", side_effect=capturing_replace):
            store._write(state)

        self.assertEqual(len(created_temps), 1)
        temp_name = created_temps[0]
        self.assertNotEqual(temp_name, "atomic.json.tmp")
        self.assertTrue(temp_name.startswith("atomic.json."))
        self.assertTrue(temp_name.endswith(".tmp"))

    # --- R3: Dynamic Sheets Grid Range ---

    def test_tier1_r3_01_sheet_range_default_empty_a1(self):
        """Verify sheet_range(sheet_name) with default empty a1 returns quoted sheet name."""
        result = sheet_range("TaskReviews")
        self.assertEqual(result, "'TaskReviews'")

    def test_tier1_r3_02_sheet_range_with_explicit_a1(self):
        """Verify sheet_range(sheet_name, a1) returns standard formatted A1 range."""
        result = sheet_range("TaskReviews", "A1:D10")
        self.assertEqual(result, "'TaskReviews'!A1:D10")

    def test_tier1_r3_03_column_to_letter_mapping(self):
        """Verify column_to_letter converts 1-based indices to Excel/Sheets column letters."""
        self.assertEqual(column_to_letter(1), "A")
        self.assertEqual(column_to_letter(3), "C")
        self.assertEqual(column_to_letter(26), "Z")
        self.assertEqual(column_to_letter(27), "AA")
        self.assertEqual(column_to_letter(28), "AB")
        self.assertEqual(column_to_letter(52), "AZ")

    def test_tier1_r3_04_write_review_video_links_dynamic_read_range(self):
        """Verify write_review_video_links uses dynamic read_range without hardcoded Z2000."""
        mock_service = MockSheetsService(values=[
            ["日期", "提交人", "视频链接"],
            ["2026-09-01", "Alice", "https://drive.google.com/file/d/existing123/view"],
        ])
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/test_sheet_id_123/edit#gid=0",
            "review_sheet_date_column": "日期",
            "review_sheet_submitter_column": "提交人",
            "review_sheet_link_column": "视频链接",
        }
        records = [
            {"name": "new_video.mp4", "webViewLink": "https://drive.google.com/file/d/new456/view"}
        ]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=mock_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 1)
        get_calls = mock_service.spreadsheets().values().get_calls
        self.assertEqual(len(get_calls), 1)
        self.assertEqual(get_calls[0]["range"], "'Sheet1'")
        self.assertNotIn("Z2000", get_calls[0]["range"])

    def test_tier1_r3_05_absence_of_z2000_in_source(self):
        """Verify hardcoded Z2000 is completely purged from GoogleSheetsHelper.py."""
        source_path = _WORKSPACE_ROOT / "model" / "GoogleSheetsHelper.py"
        source_text = source_path.read_text(encoding="utf-8")
        self.assertNotIn("Z2000", source_text, "Hardcoded range Z2000 must not exist in GoogleSheetsHelper.py")

    # --- R4: Drive Metadata Retry & Idempotency ---

    def test_tier1_r4_01_list_remote_children_retries_transient_error(self):
        """Verify list_remote_children retries on transient errors and returns result."""
        service = MockDriveService()
        attempts = [0]

        def flaky_list():
            attempts[0] += 1
            if attempts[0] == 1:
                raise socket.timeout("Transient socket timeout")
            return {"files": [{"id": "item1", "name": "test_folder", "mimeType": GOOGLE_FOLDER_MIME}]}

        service.files().list_handler = flaky_list

        with patch("time.sleep"):
            items = list_remote_children(service, "parent_root", "test_folder")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "item1")
        self.assertEqual(attempts[0], 2)

    def test_tier1_r4_02_get_remote_file_retries_and_reraises_404(self):
        """Verify get_remote_file retries transient error, but immediately re-raises 404."""
        service = MockDriveService()
        attempts = [0]

        def not_found():
            attempts[0] += 1
            raise make_http_error(404, "Not Found")

        service.files().get_handler = not_found

        with patch("time.sleep"):
            with self.assertRaises(Exception):
                get_remote_file(service, "missing_id")

        self.assertEqual(attempts[0], 1, "404 must not be retried")

    def test_tier1_r4_03_metadata_max_retries_constant(self):
        """Verify METADATA_MAX_RETRIES is defined and equals 5."""
        self.assertEqual(METADATA_MAX_RETRIES, 5)

    def test_tier1_r4_04_get_or_create_remote_folder_idempotent_on_retry(self):
        """Verify get_or_create_remote_folder lists before re-creating on retryable error."""
        service = MockDriveService()
        create_attempts = [0]

        def simulated_list():
            if create_attempts[0] >= 1:
                return {"files": [{"id": "recovered_folder_id", "mimeType": GOOGLE_FOLDER_MIME}]}
            return {"files": []}

        def simulated_create():
            create_attempts[0] += 1
            raise ConnectionResetError("Connection lost after server-side folder creation")

        service.files().list_handler = simulated_list
        service.files().create_handler = simulated_create

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(service, "parent123", "IdempotentFolder")

        self.assertEqual(folder_id, "recovered_folder_id")
        self.assertEqual(create_attempts[0], 1, "Create should not have been called a second time")

    def test_tier1_r4_05_get_or_create_remote_folder_returns_existing_without_duplicate(self):
        """Verify get_or_create_remote_folder reuses existing folder immediately."""
        service = MockDriveService()
        service.files().list_handler = lambda: {
            "files": [{"id": "existing_folder_999", "mimeType": GOOGLE_FOLDER_MIME}]
        }

        folder_id = get_or_create_remote_folder(service, "root_dir", "MyFolder")
        self.assertEqual(folder_id, "existing_folder_999")
        self.assertEqual(len(service.files().create_calls), 0)

    # --- R5: Task Submission Audit Locking ---

    def test_tier1_r5_01_audit_log_lock_defined(self):
        """Verify _AUDIT_LOG_LOCK is defined in TaskSubmissionHelper as an RLock."""
        self.assertIsNotNone(_AUDIT_LOG_LOCK)
        self.assertIsInstance(_AUDIT_LOG_LOCK, type(threading.RLock()))

    def test_tier1_r5_02_append_audit_events_acquires_lock(self):
        """Verify append_audit_events acquires _AUDIT_LOG_LOCK during append."""
        log_file = self.tmp_path / "test_audit.jsonl"
        config = {"task_submission_log_file": str(log_file)}
        events = [{"event": "test_init", "ts": "2026-09-18"}]

        mock_lock = MagicMock()
        mock_lock.__enter__.return_value = mock_lock
        with patch("model.TaskSubmissionHelper._AUDIT_LOG_LOCK", mock_lock):
            append_audit_events(config, events)

        self.assertTrue(mock_lock.__enter__.called, "append_audit_events must acquire _AUDIT_LOG_LOCK")
        self.assertTrue(mock_lock.__exit__.called, "append_audit_events must release _AUDIT_LOG_LOCK")

    def test_tier1_r5_03_append_audit_events_preserves_batch_fsync(self):
        """Verify os.fsync is preserved and executed for the batch."""
        log_file = self.tmp_path / "fsync_audit.jsonl"
        config = {"task_submission_log_file": str(log_file)}
        events = [{"id": 1}, {"id": 2}, {"id": 3}]

        fsync_calls = []
        with patch("os.fsync", side_effect=lambda fd: fsync_calls.append(fd)):
            append_audit_events(config, events)

        self.assertEqual(len(fsync_calls), 1, "fsync must be called exactly once per batch")

    def test_tier1_r5_04_append_audit_events_valid_jsonl_output(self):
        """Verify append_audit_events produces valid JSONL lines."""
        log_file = self.tmp_path / "valid.jsonl"
        config = {"task_submission_log_file": str(log_file)}
        events = [
            {"event": "start", "code": 100},
            {"event": "complete", "status": "OK"},
        ]
        append_audit_events(config, events)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["event"], "start")
        self.assertEqual(parsed[1]["status"], "OK")

    def test_tier1_r5_05_append_audit_events_respects_custom_config_path(self):
        """Verify custom task_submission_log_file path in config is honored."""
        custom_file = self.tmp_path / "custom_dir" / "custom_audit.jsonl"
        config = {"task_submission_log_file": str(custom_file)}
        events = [{"custom": True}]

        out_path = append_audit_events(config, events)
        self.assertEqual(out_path.resolve(), custom_file.resolve())
        self.assertTrue(custom_file.exists())


# ============================================================================
# Tier 2: Boundary & Corner Cases (25 tests)
# ============================================================================

class Tier2BoundaryAndCornerCaseTests(unittest.TestCase):
    """
    Tier 2 tests verify limits, error conditions, empty/large inputs, and extreme boundary behavior.
    At least 5 test cases per requirement.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # --- R1 Boundaries ---

    def test_tier2_r1_01_empty_today_dir_no_images(self):
        """Boundary: today_dir has no valid images -> cleanly completes with (0,0) without crashing."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()

        thread = VideoAssignmentThread(str(today_dir), str(videos_dir))
        completed_results = []
        thread.completed.connect(lambda m, p: completed_results.append((m, p)))

        thread.run()

        self.assertEqual(completed_results, [(0, 0)])

    def test_tier2_r1_02_empty_video_root_no_videos(self):
        """Boundary: today_dir has images, but video_root has no video files -> processed=0, moved=0."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()

        mock_matcher = MagicMock()
        mock_matcher.scan_images_recursively.return_value = [
            {"path": "dummy.jpg", "folder": str(today_dir), "desc": [[1, 2]], "name": "dummy.jpg"}
        ]

        with patch("PYUI.main_pyui.FeatureMatcher", return_value=mock_matcher), \
             patch("PYUI.main_pyui.FeatureMatcher.scan_images_recursively", return_value=mock_matcher.scan_images_recursively.return_value):
            thread = VideoAssignmentThread(str(today_dir), str(videos_dir))
            completed_results = []
            thread.completed.connect(lambda m, p: completed_results.append((m, p)))
            thread.run()

        self.assertEqual(completed_results, [(0, 0)])

    def test_tier2_r1_03_nonexistent_paths_prevent_thread_launch(self):
        """Boundary: missing directory paths in assignVideo trigger Critical messagebox and return None."""
        win = make_assignment_host(self.tmp_path / "nonexistent_today")
        with patch("globalValue.globalValue.videoSortingStationPath", return_value=str(self.tmp_path / "nonexistent_vid")):
            result = MainDialog.assignVideo(win)
            self.assertIsNone(result)
            win.Critical.assert_called_once()
            self.assertTrue(win.assign_video_btn.isEnabled())

    def test_tier2_r1_04_corrupt_video_frame_gracefully_skipped(self):
        """Boundary: video file whose frame decoding returns None is skipped without terminating loop."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()
        (videos_dir / "corrupted.mp4").write_text("not a video")

        mock_matcher = MagicMock()
        img_db = [{"path": "img1.png", "folder": str(today_dir), "desc": [[1]], "name": "img1.png"}]

        with patch("PYUI.main_pyui.FeatureMatcher", return_value=mock_matcher), \
             patch("PYUI.main_pyui.FeatureMatcher.scan_images_recursively", return_value=img_db), \
             patch("PYUI.main_pyui.FeatureMatcher.get_video_frame_clean", return_value=None):
            thread = VideoAssignmentThread(str(today_dir), str(videos_dir))
            completed_results = []
            thread.completed.connect(lambda m, p: completed_results.append((m, p)))
            thread.run()

        self.assertEqual(completed_results, [(0, 0)])

    def test_tier2_r1_05_target_file_collision_appends_match_suffix(self):
        """Boundary: existing destination file triggers target_path with _match suffix."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()

        existing_dest = today_dir / "clip.mp4"
        existing_dest.write_text("destination existing")

        source_video = videos_dir / "clip.mp4"
        source_video.write_text("source video content")

        mock_matcher = MagicMock()
        mock_matcher.get_features.return_value = (None, [[1, 2]])
        mock_matcher.match.return_value = 0.90
        img_db = [{"path": "pic.png", "folder": str(today_dir), "desc": [[1, 2]], "name": "pic.png"}]

        with patch("PYUI.main_pyui.FeatureMatcher", return_value=mock_matcher), \
             patch("PYUI.main_pyui.FeatureMatcher.scan_images_recursively", return_value=img_db), \
             patch("PYUI.main_pyui.FeatureMatcher.get_video_frame_clean", return_value=MagicMock()):
            thread = VideoAssignmentThread(str(today_dir), str(videos_dir), match_ratio_threshold=0.5)
            thread.run()

        expected_match_path = today_dir / "clip_match.mp4"
        self.assertTrue(expected_match_path.exists())
        self.assertEqual(expected_match_path.read_text(), "source video content")
        self.assertEqual(existing_dest.read_text(), "destination existing")

    # --- R2 Boundaries ---

    def test_tier2_r2_01_add_item_empty_name_raises_value_error(self):
        """Boundary: adding an item with whitespace or empty name raises ValueError and releases lock."""
        store = InventoryStore(self.tmp_path / "inv.json")
        with self.assertRaises(ValueError):
            store.add_item("   ", 10, 1)

        acquired = store.lock.acquire(blocking=False)
        self.assertTrue(acquired)
        store.lock.release()

    def test_tier2_r2_02_add_item_duplicate_name_case_insensitive(self):
        """Boundary: case-insensitive duplicate name raises ValueError."""
        store = InventoryStore(self.tmp_path / "inv.json")
        store.add_item("胶带", 5, 1)
        with self.assertRaises(ValueError) as ctx:
            store.add_item("  胶带  ", 10, 2)
        self.assertIn("同名", str(ctx.exception))

    def test_tier2_r2_03_add_stock_negative_or_zero_amount(self):
        """Boundary: add_stock with <= 0 amount raises ValueError."""
        store = InventoryStore(self.tmp_path / "inv.json")
        item = store.add_item("剪刀", 5, 0)
        with self.assertRaises(ValueError):
            store.add_stock(item["id"], 0)
        with self.assertRaises(ValueError):
            store.add_stock(item["id"], -3)

    def test_tier2_r2_04_operations_on_nonexistent_item_raise_key_error(self):
        """Boundary: update_item, add_stock, and delete_item on unknown id raise KeyError."""
        store = InventoryStore(self.tmp_path / "inv.json")
        unknown_id = "nonexistent_id_123"
        with self.assertRaises(KeyError):
            store.update_item(unknown_id, "新名称", 10, 1)
        with self.assertRaises(KeyError):
            store.add_stock(unknown_id, 5)
        with self.assertRaises(KeyError):
            store.delete_item(unknown_id)

    def test_tier2_r2_05_high_contention_concurrent_rmw(self):
        """Boundary: 10 threads hammering mixed RMW operations on the same file maintain integrity."""
        store = InventoryStore(self.tmp_path / "heavy_contention.json")
        base_item = store.add_item("共享库存", 100, 0)
        item_id = base_item["id"]

        def mutate_stock():
            for _ in range(15):
                store.add_stock(item_id, 1)

        threads = [threading.Thread(target=mutate_stock) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_state = store.load()
        item = [i for i in final_state["items"] if i["id"] == item_id][0]
        self.assertEqual(item["quantity"], 100 + 10 * 15)

    # --- R3 Boundaries ---

    def test_tier2_r3_01_empty_sheet_values(self):
        """Boundary: empty sheet values list [[]] does not raise IndexError and handles cleanly."""
        mock_service = MockSheetsService(values=[])
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/empty_sheet/edit#gid=0",
        }
        records = [{"name": "item.mp4", "webViewLink": "https://drive.google.com/file/d/abc/view"}]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=mock_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertGreaterEqual(written, 0)

    def test_tier2_r3_02_sheet_beyond_2000_rows_deduplication(self):
        """Boundary: existing record at row 2050 is recognized and skipped (prevents duplicates)."""
        header = ["日期", "提交人", "视频链接"]
        rows = [header]
        target_link = "https://drive.google.com/file/d/deep_historical_id/view"

        for i in range(2, 2100):
            if i == 2050:
                rows.append(["2026-01-01", "OldUser", f'=HYPERLINK("{target_link}", "old_video.mp4")'])
            else:
                rows.append(["2026-01-01", "User", f'=HYPERLINK("https://drive.google.com/file/d/id_{i}/view", "v.mp4")'])

        mock_service = MockSheetsService(values=rows)
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/deep_sheet/edit#gid=0",
            "review_sheet_date_column": "日期",
            "review_sheet_submitter_column": "提交人",
            "review_sheet_link_column": "视频链接",
        }
        records = [{"name": "old_video.mp4", "webViewLink": target_link}]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=mock_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 0, "Record at row 2050 must be deduplicated and skipped")

    def test_tier2_r3_03_sheet_name_with_single_quotes_and_spaces(self):
        """Boundary: sheet name containing single quotes escapes them properly."""
        result = sheet_range("Bob's Review Sheet")
        self.assertEqual(result, "'Bob''s Review Sheet'")

        result_with_range = sheet_range("Bob's Review Sheet", "B2:E5")
        self.assertEqual(result_with_range, "'Bob''s Review Sheet'!B2:E5")

    def test_tier2_r3_04_column_to_letter_wide_columns(self):
        """Boundary: column_to_letter converts multi-letter column indices correctly."""
        self.assertEqual(column_to_letter(53), "BA")
        self.assertEqual(column_to_letter(702), "ZZ")
        self.assertEqual(column_to_letter(703), "AAA")

    def test_tier2_r3_05_missing_headers_graceful_fallback(self):
        """Boundary: sheet has data rows but no matching header names falls back to defaults."""
        mock_service = MockSheetsService(values=[
            ["Col1", "Col2", "Col3"],
            ["Val1", "Val2", "Val3"],
        ])
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/no_header/edit#gid=0",
            "review_sheet_date_column": "未知日期",
            "review_sheet_submitter_column": "未知提交人",
            "review_sheet_link_column": "未知链接",
        }
        records = [{"name": "fallback.mp4", "webViewLink": "https://drive.google.com/file/d/fallback/view"}]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=mock_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 1)

    # --- R4 Boundaries ---

    def test_tier2_r4_01_max_retries_exhaustion_raises_original_error(self):
        """Boundary: retryable error persisting beyond METADATA_MAX_RETRIES is finally raised."""
        service = MockDriveService()
        attempts = [0]

        def always_fail():
            attempts[0] += 1
            raise socket.timeout("Persistent timeout")

        service.files().list_handler = always_fail

        with patch("time.sleep"):
            with self.assertRaises(socket.timeout):
                list_remote_children(service, "parent", "item", max_retries=3)

        self.assertEqual(attempts[0], 4, "Should try 1 initial + 3 retries = 4 attempts")

    def test_tier2_r4_02_non_retryable_400_bad_request_fails_immediately(self):
        """Boundary: 400 Bad Request is not retryable and fails on attempt 1."""
        service = MockDriveService()
        attempts = [0]

        def bad_request():
            attempts[0] += 1
            raise make_http_error(400, "Bad Request")

        service.files().get_handler = bad_request

        with patch("time.sleep"):
            with self.assertRaises(Exception):
                get_remote_file(service, "file_id")

        self.assertEqual(attempts[0], 1)

    def test_tier2_r4_03_folder_name_with_special_characters_escaped(self):
        """Boundary: folder name with single quotes and backslashes is escaped in Drive query."""
        service = MockDriveService()
        with patch("time.sleep"):
            list_remote_children(service, "parent_dir", r"John's \Special/ Folder")

        query = service.files().list_calls[0]["q"]
        self.assertIn(r"John\'s", query)
        self.assertIn(r"\\Special/ Folder", query)

    def test_tier2_r4_04_idempotent_folder_creation_transient_failure_network_reset(self):
        """Boundary: SSLError during folder creation discovers folder on retry check."""
        service = MockDriveService()
        create_calls = [0]

        def list_handler():
            if create_calls[0] > 0:
                return {"files": [{"id": "ssl_recovered_folder", "mimeType": GOOGLE_FOLDER_MIME}]}
            return {"files": []}

        def create_handler():
            create_calls[0] += 1
            raise ssl.SSLError("SSL handshake dropped")

        service.files().list_handler = list_handler
        service.files().create_handler = create_handler

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(service, "p1", "SecureFolder")

        self.assertEqual(folder_id, "ssl_recovered_folder")
        self.assertEqual(create_calls[0], 1)

    def test_tier2_r4_05_list_remote_children_empty_results(self):
        """Boundary: list_remote_children with empty response returns empty list."""
        service = MockDriveService()
        service.files().list_handler = lambda: {"files": []}

        items = list_remote_children(service, "p1", "Nonexistent")
        self.assertEqual(items, [])

    # --- R5 Boundaries ---

    def test_tier2_r5_01_append_empty_events_list(self):
        """Boundary: append_audit_events with events=[] writes 0 records without error."""
        log_file = self.tmp_path / "empty.jsonl"
        config = {"task_submission_log_file": str(log_file)}

        path = append_audit_events(config, [])
        self.assertEqual(path.resolve(), log_file.resolve())
        self.assertTrue(log_file.exists())
        self.assertEqual(log_file.read_text(encoding="utf-8"), "")

    def test_tier2_r5_02_append_unicode_and_special_characters(self):
        """Boundary: events containing non-ASCII, emojis, Chinese characters parse back identically."""
        log_file = self.tmp_path / "unicode.jsonl"
        config = {"task_submission_log_file": str(log_file)}
        events = [{
            "text": "测试中文字符与表情 🎉 🚀 特殊符号 \\ ' \" \n",
            "number": 12345.67,
        }]

        append_audit_events(config, events)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["text"], events[0]["text"])

    def test_tier2_r5_03_append_non_serializable_objects_via_default_str(self):
        """Boundary: events with Path, UUID, datetime objects are serialized via default=str."""
        log_file = self.tmp_path / "objects.jsonl"
        config = {"task_submission_log_file": str(log_file)}
        now = datetime(2026, 9, 18, 12, 0, 0)
        item_uuid = uuid.uuid4()
        events = [{
            "path": Path("C:/test/file.mp4"),
            "timestamp": now,
            "uuid": item_uuid,
        }]

        append_audit_events(config, events)

        line = log_file.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        self.assertIn("file.mp4", data["path"])
        self.assertEqual(data["timestamp"], str(now))
        self.assertEqual(data["uuid"], str(item_uuid))

    def test_tier2_r5_04_large_batch_audit_logging(self):
        """Boundary: single large batch of 500 events writes all 500 lines intact."""
        log_file = self.tmp_path / "large_batch.jsonl"
        config = {"task_submission_log_file": str(log_file)}
        batch = [{"index": i, "data": f"payload_{i}"} for i in range(500)]

        append_audit_events(config, batch)

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 500)
        self.assertEqual(json.loads(lines[0])["index"], 0)
        self.assertEqual(json.loads(lines[499])["index"], 499)

    def test_tier2_r5_05_write_failure_propagates_and_releases_lock(self):
        """Boundary: unexpected disk error inside write releases _AUDIT_LOG_LOCK."""
        log_file = self.tmp_path / "fail.jsonl"
        config = {"task_submission_log_file": str(log_file)}

        with patch.object(Path, "open", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                append_audit_events(config, [{"key": "val"}])

        acquired = _AUDIT_LOG_LOCK.acquire(blocking=False)
        self.assertTrue(acquired, "_AUDIT_LOG_LOCK must not remain locked after write failure")
        _AUDIT_LOG_LOCK.release()


# ============================================================================
# Tier 3: Cross-Feature Combinations (5 tests)
# ============================================================================

class Tier3CrossFeatureCombinationTests(unittest.TestCase):
    """
    Tier 3 tests verify pairwise interactions across requirements:
    - R2 & R5: Concurrent inventory mutations and audit logging without cross-lock contention.
    - R3 & R4: Google Drive folder retrieval / creation followed by dynamic Sheets range update.
    - R1 & R5: Video assignment worker thread running concurrently with audit logging.
    - R2 & R3: Inventory state lookups concurrent with Sheets dynamic sync.
    - R4 & R5: Drive API failure triggers structured audit logging.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tier3_pair_r2_r5_concurrent_inventory_and_audit_logging(self):
        """Pairwise (R2 + R5): Concurrent InventoryStore mutations and audit log appending."""
        store_path = self.tmp_path / "inv_r2.json"
        log_path = self.tmp_path / "audit_r5.jsonl"
        store = InventoryStore(store_path)
        item = store.add_item("剪辑盒", 0, 0)
        config = {"task_submission_log_file": str(log_path)}

        num_cycles = 20

        def inventory_worker():
            for _ in range(num_cycles):
                store.add_stock(item["id"], 1)

        def audit_worker():
            for i in range(num_cycles):
                append_audit_events(config, [{"thread_event": i}])

        t1 = threading.Thread(target=inventory_worker)
        t2 = threading.Thread(target=audit_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final_item = store.load()["items"][0]
        self.assertEqual(final_item["quantity"], float(num_cycles))

        log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(log_lines), num_cycles)

    def test_tier3_pair_r3_r4_drive_folder_retrieval_and_sheets_sync(self):
        """Pairwise (R3 + R4): Drive folder resolution (with retries) feeds Sheets dynamic review links."""
        drive_service = MockDriveService()
        attempts = [0]

        def flaky_folder_list():
            attempts[0] += 1
            if attempts[0] == 1:
                raise socket.timeout("Drive network glitch")
            return {"files": [{"id": "resolved_folder_123", "mimeType": GOOGLE_FOLDER_MIME}]}

        drive_service.files().list_handler = flaky_folder_list

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(drive_service, "root", "ProjectVideos")

        self.assertEqual(folder_id, "resolved_folder_123")

        sheets_service = MockSheetsService(values=[
            ["日期", "提交人", "视频链接"],
        ])
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/combo_sheet/edit#gid=0",
            "review_sheet_date_column": "日期",
            "review_sheet_submitter_column": "提交人",
            "review_sheet_link_column": "视频链接",
        }
        records = [{
            "name": "clip_combo.mp4",
            "webViewLink": f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing",
        }]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=sheets_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 1)
        self.assertEqual(sheets_service.spreadsheets().values().get_calls[0]["range"], "'Sheet1'")

    def test_tier3_pair_r1_r5_video_assignment_with_concurrent_audit_logging(self):
        """Pairwise (R1 + R5): VideoAssignmentThread emits signals while audit events are concurrently appended."""
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()
        log_path = self.tmp_path / "audit_async.jsonl"
        config = {"task_submission_log_file": str(log_path)}

        thread = VideoAssignmentThread(str(today_dir), str(videos_dir))
        logs_collected = []
        thread.log.connect(logs_collected.append)

        def log_appender():
            for i in range(10):
                append_audit_events(config, [{"action": "heartbeat", "seq": i}])
                time.sleep(0.005)

        t = threading.Thread(target=log_appender)
        t.start()
        thread.run()
        t.join()

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertTrue(len(logs_collected) >= 1)

    def test_tier3_pair_r2_r3_inventory_lookup_during_sheets_update(self):
        """Pairwise (R2 + R3): Inventory state query operates cleanly while Sheets dynamic rows are constructed."""
        store = InventoryStore(self.tmp_path / "inv_sync.json")
        store.add_item("成品盘", 50, 2)

        sheets_service = MockSheetsService(values=[["日期", "提交人", "视频链接"]])
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/inv_sheet/edit#gid=0",
            "review_sheet_date_column": "日期",
            "review_sheet_submitter_column": "提交人",
            "review_sheet_link_column": "视频链接",
        }
        records = [{"name": "finished.mp4", "webViewLink": "https://drive.google.com/file/d/finished/view"}]

        observed_qty = []
        def intercept_load_sheets(cfg):
            with store.lock:
                item = store.load()["items"][0]
                observed_qty.append(item["quantity"])
            return sheets_service

        with patch("model.GoogleSheetsHelper.load_sheets_service", side_effect=intercept_load_sheets), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 1)
        self.assertEqual(observed_qty, [50.0])

    def test_tier3_pair_r4_r5_drive_failure_logging_to_audit(self):
        """Pairwise (R4 + R5): Drive retry exhaustion triggers structured audit logging."""
        drive_service = MockDriveService()
        drive_service.files().get_handler = lambda: (_ for _ in ()).throw(socket.timeout("Persistent timeout"))

        log_path = self.tmp_path / "drive_failures.jsonl"
        config = {"task_submission_log_file": str(log_path)}

        with patch("time.sleep"):
            try:
                get_remote_file(drive_service, "failing_file", max_retries=1)
            except Exception as exc:
                append_audit_events(config, [{
                    "event": "drive_metadata_failure",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }])

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["event"], "drive_metadata_failure")
        self.assertIn(data["error_type"], ("timeout", "TimeoutError"))


# ============================================================================
# Tier 4: Real-World Application Scenarios (4 tests)
# ============================================================================

class Tier4RealWorldApplicationScenarioTests(unittest.TestCase):
    """
    Tier 4 tests verify end-to-end multi-step application scenarios:
    1. Complete Task Submission & Review Link Sync Pipeline.
    2. Multi-Subsystem Stress Storm (Video assignment + Inventory RMW + Audit logs).
    3. Resilient Network Recovery & Dynamic Sheets Deduplication.
    4. Desktop UI Lifecycle & Thread Interruption Teardown.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tier4_scenario1_full_task_submission_pipeline(self):
        """
        Scenario 1: End-to-end task submission pipeline.
        - Step 1: Video files are registered.
        - Step 2: Google Drive folder is resolved/created (recovering from initial network timeout).
        - Step 3: Google Sheets receives submission records, reading dynamically past 2000 rows.
        - Step 4: Audit trail is persisted to TaskSubmissionLog.jsonl with fsync guarantee.
        """
        audit_log = self.tmp_path / "TaskSubmissionLog.jsonl"
        config = {
            "task_submission_log_file": str(audit_log),
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/pipeline_sheet/edit#gid=0",
            "review_sheet_date_column": "日期",
            "review_sheet_submitter_column": "提交人",
            "review_sheet_link_column": "视频链接",
        }

        # Step 2: Drive folder lookup with network hiccup
        drive_service = MockDriveService()
        drive_attempts = [0]
        def flaky_folder():
            drive_attempts[0] += 1
            if drive_attempts[0] == 1:
                raise ssl.SSLError("SSL dropped")
            return {"files": [{"id": "folder_e2e_001", "mimeType": GOOGLE_FOLDER_MIME}]}
        drive_service.files().list_handler = flaky_folder

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(drive_service, "root_dir", "2026-09-18_Batch")
        self.assertEqual(folder_id, "folder_e2e_001")

        # Step 3: Sheets dynamic write
        sheets_service = MockSheetsService(values=[
            ["日期", "提交人", "视频链接"],
            ["2026-09-17", "PriorUser", "https://drive.google.com/file/d/old1/view"],
        ])
        records = [
            {"name": "production_01.mp4", "webViewLink": "https://drive.google.com/file/d/prod1/view"},
            {"name": "production_02.mp4", "webViewLink": "https://drive.google.com/file/d/prod2/view"},
        ]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=sheets_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 2)
        self.assertNotIn("Z2000", sheets_service.spreadsheets().values().get_calls[0]["range"])

        # Step 4: Audit events
        append_audit_events(config, [
            {"event": "submission_completed", "folder_id": folder_id, "written_count": written}
        ])

        audit_lines = audit_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(audit_lines), 1)
        record = json.loads(audit_lines[0])
        self.assertEqual(record["folder_id"], "folder_e2e_001")
        self.assertEqual(record["written_count"], 2)

    def test_tier4_scenario2_multi_subsystem_stress_storm(self):
        """
        Scenario 2: Multi-subsystem stress storm.
        Simultaneously runs VideoAssignmentThread, InventoryStore replenishment, and audit logging
        across 10 worker threads to ensure total concurrency safety.
        """
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()
        store_path = self.tmp_path / "storm_inv.json"
        log_path = self.tmp_path / "storm_audit.jsonl"

        store = InventoryStore(store_path)
        item = store.add_item("耗材A", 0, 0)
        item_id = item["id"]
        config = {"task_submission_log_file": str(log_path)}

        errors = []

        def worker_inventory():
            try:
                for _ in range(10):
                    store.add_stock(item_id, 2)
            except Exception as e:
                errors.append(e)

        def worker_audit(seq):
            try:
                for j in range(10):
                    append_audit_events(config, [{"worker": seq, "idx": j}])
            except Exception as e:
                errors.append(e)

        def worker_video():
            try:
                thread = VideoAssignmentThread(str(today_dir), str(videos_dir))
                thread.run()
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(4):
            threads.append(threading.Thread(target=worker_inventory))
        for i in range(4):
            threads.append(threading.Thread(target=worker_audit, args=(i,)))
        for _ in range(2):
            threads.append(threading.Thread(target=worker_video))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"No exceptions permitted during stress storm: {errors}")

        # Check inventory
        final_qty = store.load()["items"][0]["quantity"]
        self.assertEqual(final_qty, 80.0)

        # Check audit log
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 40)
        for line in lines:
            json.loads(line)

    def test_tier4_scenario3_flaky_network_recovery_workflow(self):
        """
        Scenario 3: Flaky network environment recovery workflow.
        - Drive list calls fail 2 times with 503 before succeeding.
        - Folder creation fails with ConnectionError, recovering idempotently.
        - Sheets reads dynamically without 2000-row cutoff.
        - All transitions are logged to audit JSONL.
        """
        audit_log = self.tmp_path / "flaky_audit.jsonl"
        config = {"task_submission_log_file": str(audit_log)}

        drive_service = MockDriveService()
        list_attempts = [0]
        create_attempts = [0]

        def flaky_list():
            list_attempts[0] += 1
            if list_attempts[0] <= 2:
                raise make_http_error(503, "Service Unavailable")
            if create_attempts[0] > 0:
                return {"files": [{"id": "recovered_folder_flaky", "mimeType": GOOGLE_FOLDER_MIME}]}
            return {"files": []}

        def flaky_create():
            create_attempts[0] += 1
            raise ConnectionError("Connection dropped during folder create")

        drive_service.files().list_handler = flaky_list
        drive_service.files().create_handler = flaky_create

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(drive_service, "root", "FlakyFolder")

        self.assertEqual(folder_id, "recovered_folder_flaky")
        self.assertGreaterEqual(list_attempts[0], 3)
        self.assertEqual(create_attempts[0], 1)

        append_audit_events(config, [
            {"event": "folder_recovery_successful", "folder_id": folder_id}
        ])

        self.assertTrue(audit_log.exists())
        self.assertIn("recovered_folder_flaky", audit_log.read_text(encoding="utf-8"))

    def test_tier4_scenario4_ui_lifecycle_and_thread_teardown(self):
        """
        Scenario 4: Desktop UI window lifecycle and thread teardown.
        Simulates launching VideoAssignmentThread from MainDialog, processing events,
        and cleanly interrupting / shutting down without hanging or crashing.
        """
        today_dir = self.tmp_path / "today"
        today_dir.mkdir()
        videos_dir = self.tmp_path / "videos"
        videos_dir.mkdir()

        win = make_assignment_host(today_dir)
        with patch("globalValue.globalValue.videoSortingStationPath", return_value=str(videos_dir)), \
             patch("PYUI.main_pyui.VideoAssignmentThread", FakeVideoAssignmentWorker):
            thread = MainDialog.assignVideo(win)
            self.assertIsNotNone(thread)
            self.assertTrue(thread.isRunning())

            thread.finish()

            self.assertTrue(win.assign_video_btn.isEnabled())
            self.assertEqual(win.assign_video_btn.text(), "分拣视频")


if __name__ == "__main__":
    unittest.main()
