"""
Adversarial Stress Test Suite - Challenger 2
Focus:
1. Google Drive metadata retries & idempotency:
   - Simulate transient network errors (500, 502, 503, 504) and verify exponential backoff retries.
   - Verify that standard 403 (permission denied) fails immediately without retry.
   - Verify that rate-limit 403 (rateLimitExceeded / userRateLimitExceeded) retries up to METADATA_MAX_RETRIES.
   - Verify idempotent folder creation: simulate timeout on first create and verify retry re-uses existing folder rather than creating a duplicate.
2. Dynamic Google Sheets grid reading:
   - Test reading sheets beyond 2000 rows (e.g. 5,000+ rows) and verify full deduplication.
   - Test `column_to_letter` across wide ranges (1..2000+).
   - Verify complete absence of `Z2000` in codebase.
3. Hermetic network isolation enforcement:
   - Verify test suite strictly forbids live external network connections.
"""

import json
import os
import random
import socket
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

# Target modules
import model.GoogleDriveHelper as GoogleDriveHelperMod
from model.GoogleDriveHelper import (
    METADATA_MAX_RETRIES,
    GOOGLE_FOLDER_MIME,
    list_remote_children,
    get_remote_file,
    get_or_create_remote_folder,
    is_retryable_upload_error,
    retry_sleep_seconds,
    _is_rate_limit_403,
    escape_query_text,
)
import model.GoogleSheetsHelper as GoogleSheetsHelperMod
from model.GoogleSheetsHelper import (
    sheet_range,
    column_to_letter,
    write_review_video_links,
    existing_links,
    choose_write_start_row,
)


# ============================================================================
# Helpers and Mock Doubles
# ============================================================================

class MockHttpResponse:
    def __init__(self, status: int, reason: str = ""):
        self.status = status
        self.reason = reason


def make_http_error(status: int, reason: str = "", content: bytes = b""):
    try:
        from googleapiclient.errors import HttpError
        resp = MockHttpResponse(status, reason)
        return HttpError(resp, content or f"HTTP {status} {reason}".encode("utf-8"))
    except Exception:
        class FakeHttpError(Exception):
            def __init__(self, resp, content):
                self.resp = resp
                self.content = content
                super().__init__(f"<HttpError {resp.status} \"{resp.reason}\">")
        return FakeHttpError(MockHttpResponse(status, reason), content or f"HTTP {status} {reason}".encode("utf-8"))


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
        self.create_handler = lambda: {"id": "mock-created-id"}

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
    def __init__(self, values=None, sheet_name="ReviewSheet", sheet_id=101):
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
    def __init__(self, values=None, sheet_name="ReviewSheet", sheet_id=101):
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


def setUpModule():
    socket.socket.connect = _hermetic_socket_connect


def tearDownModule():
    socket.socket.connect = _ORIGINAL_SOCKET_CONNECT


# ============================================================================
# Test Suite 1: Google Drive Transient Errors and Exponential Backoff Retries
# ============================================================================

class TestGoogleDriveTransientAndBackoff(unittest.TestCase):
    """
    Simulate transient network errors (500, 502, 503, 504) and verify exponential backoff retries.
    """

    def test_transient_status_codes_recognized_as_retryable(self):
        """Verify is_retryable_upload_error classifies 429, 500, 502, 503, 504 as retryable."""
        for code in (429, 500, 502, 503, 504):
            err = make_http_error(code, f"Error {code}")
            self.assertTrue(
                is_retryable_upload_error(err),
                f"HTTP {code} must be recognized as retryable",
            )

        # Low-level OS / socket / SSL errors must also be retryable
        self.assertTrue(is_retryable_upload_error(socket.timeout("Timed out")))
        self.assertTrue(is_retryable_upload_error(ssl.SSLError("Handshake failed")))
        self.assertTrue(is_retryable_upload_error(ConnectionResetError("Connection reset")))
        self.assertTrue(is_retryable_upload_error(TimeoutError("Timeout")))
        self.assertTrue(is_retryable_upload_error(OSError("I/O error")))

        # Non-retryable HTTP status codes
        for code in (400, 401, 404, 405, 409):
            err = make_http_error(code, f"Error {code}")
            self.assertFalse(
                is_retryable_upload_error(err),
                f"HTTP {code} must NOT be retryable",
            )

    def test_exponential_backoff_sleep_duration(self):
        """Verify retry_sleep_seconds applies exponential backoff base = min(60, 2**retry_count) + jitter."""
        for attempt in range(1, 8):
            sleep_time = retry_sleep_seconds(attempt)
            base = min(60, 2 ** attempt)
            # base <= sleep_time <= base + 1.5
            self.assertGreaterEqual(sleep_time, base, f"Attempt {attempt}: sleep should be >= {base}")
            self.assertLessEqual(sleep_time, base + 1.5, f"Attempt {attempt}: sleep should be <= {base + 1.5}")

        # Capped at 60 seconds
        high_attempt_sleep = retry_sleep_seconds(20)
        self.assertGreaterEqual(high_attempt_sleep, 60.0)
        self.assertLessEqual(high_attempt_sleep, 61.5)

    def test_list_remote_children_retries_transient_500_exponential_backoff(self):
        """Simulate HTTP 500 transient error in list_remote_children with exponential backoff sleeps."""
        service = MockDriveService()
        attempts = [0]
        recorded_sleeps = []

        def flaky_list():
            attempts[0] += 1
            if attempts[0] <= 3:
                raise make_http_error(500, "Internal Server Error")
            return {"files": [{"id": "item_500", "name": "target_name", "mimeType": "video/mp4"}]}

        service.files().list_handler = flaky_list

        with patch("time.sleep", side_effect=lambda s: recorded_sleeps.append(s)):
            result = list_remote_children(service, "parent_dir", "target_name")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "item_500")
        self.assertEqual(attempts[0], 4, "Should succeed on 4th attempt (1 initial + 3 retries)")
        self.assertEqual(len(recorded_sleeps), 3)
        # Sleep values should correspond to attempts 1, 2, 3: ~2s, ~4s, ~8s
        self.assertGreaterEqual(recorded_sleeps[0], 2.0)
        self.assertGreaterEqual(recorded_sleeps[1], 4.0)
        self.assertGreaterEqual(recorded_sleeps[2], 8.0)

    def test_get_remote_file_retries_transient_502_bad_gateway(self):
        """Simulate HTTP 502 Bad Gateway in get_remote_file."""
        service = MockDriveService()
        attempts = [0]
        recorded_sleeps = []

        def flaky_get():
            attempts[0] += 1
            if attempts[0] <= 2:
                raise make_http_error(502, "Bad Gateway")
            return {"id": "file_502", "name": "video_502.mp4", "mimeType": "video/mp4"}

        service.files().get_handler = flaky_get

        with patch("time.sleep", side_effect=lambda s: recorded_sleeps.append(s)):
            result = get_remote_file(service, "file_502")

        self.assertEqual(result["id"], "file_502")
        self.assertEqual(attempts[0], 3)
        self.assertEqual(len(recorded_sleeps), 2)

    def test_get_or_create_remote_folder_retries_503_service_unavailable(self):
        """Simulate HTTP 503 Service Unavailable during initial list inside get_or_create_remote_folder."""
        service = MockDriveService()
        list_attempts = [0]

        def flaky_list():
            list_attempts[0] += 1
            if list_attempts[0] <= 2:
                raise make_http_error(503, "Service Unavailable")
            return {"files": [{"id": "recovered_503_folder", "mimeType": GOOGLE_FOLDER_MIME}]}

        service.files().list_handler = flaky_list

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(service, "root", "Folder503")

        self.assertEqual(folder_id, "recovered_503_folder")
        self.assertEqual(list_attempts[0], 3)
        self.assertEqual(len(service.files().create_calls), 0)

    def test_transient_504_gateway_timeout_exhaustion_raises(self):
        """Simulate HTTP 504 Gateway Timeout that persists beyond METADATA_MAX_RETRIES."""
        service = MockDriveService()
        attempts = [0]

        def always_504():
            attempts[0] += 1
            raise make_http_error(504, "Gateway Timeout")

        service.files().get_handler = always_504

        with patch("time.sleep"):
            with self.assertRaises(Exception) as ctx:
                get_remote_file(service, "timeout_id")

        self.assertEqual(attempts[0], METADATA_MAX_RETRIES + 1, "Must try 1 initial + METADATA_MAX_RETRIES attempts")
        self.assertIn("504", str(ctx.exception))

    def test_mixed_transient_errors_in_single_retry_cycle(self):
        """Simulate a sequence of different transient errors (500, 502, socket.timeout) succeeding eventually."""
        service = MockDriveService()
        attempts = [0]
        sequence = [
            make_http_error(500, "Internal Error"),
            make_http_error(502, "Bad Gateway"),
            socket.timeout("Socket dropped"),
        ]

        def mixed_list():
            attempts[0] += 1
            if attempts[0] <= len(sequence):
                raise sequence[attempts[0] - 1]
            return {"files": [{"id": "success_id", "mimeType": GOOGLE_FOLDER_MIME}]}

        service.files().list_handler = mixed_list

        with patch("time.sleep"):
            result = list_remote_children(service, "root", "MyDir")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "success_id")
        self.assertEqual(attempts[0], 4)


# ============================================================================
# Test Suite 2: 403 Error Discrimination (Standard 403 vs Rate-Limit 403)
# ============================================================================

class TestGoogleDrive403Discrimination(unittest.TestCase):
    """
    Verify:
    1. Standard 403 (permission denied) fails immediately without retry.
    2. Rate-limit 403 (rateLimitExceeded / userRateLimitExceeded) retries up to METADATA_MAX_RETRIES.
    """

    def test_standard_403_permission_denied_fails_immediately_no_retry(self):
        """Standard 403 (permission denied) must fail on attempt 1 without any retry sleep."""
        body = json.dumps({
            "error": {
                "code": 403,
                "message": "The caller does not have permission",
                "errors": [
                    {
                        "message": "The caller does not have permission",
                        "domain": "global",
                        "reason": "forbidden"
                    }
                ]
            }
        }).encode("utf-8")

        perm_error = make_http_error(403, "Forbidden", content=body)
        self.assertFalse(
            is_retryable_upload_error(perm_error),
            "Standard permission 403 must NOT be retryable",
        )

        service = MockDriveService()
        attempts = [0]
        sleep_calls = []

        def fail_perm():
            attempts[0] += 1
            raise perm_error

        service.files().list_handler = fail_perm

        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            with self.assertRaises(Exception):
                list_remote_children(service, "parent", "test_item")

        self.assertEqual(attempts[0], 1, "Standard 403 must fail immediately on attempt 1")
        self.assertEqual(len(sleep_calls), 0, "No sleep should occur for standard 403")

    def test_standard_403_insufficient_permissions_fails_immediately(self):
        """Verify get_remote_file immediately fails on standard 403 insufficientFilePermissions."""
        body = json.dumps({
            "error": {
                "code": 403,
                "message": "The user does not have sufficient permissions for file.",
                "errors": [{"reason": "insufficientFilePermissions"}]
            }
        }).encode("utf-8")
        perm_error = make_http_error(403, "Forbidden", content=body)

        service = MockDriveService()
        attempts = [0]
        service.files().get_handler = lambda: (_ for _ in ()).throw(perm_error)

        with patch("time.sleep"):
            with self.assertRaises(Exception):
                get_remote_file(service, "forbidden_file_id")

        self.assertFalse(is_retryable_upload_error(perm_error))

    def test_standard_403_access_not_configured_fails_immediately(self):
        """Verify 403 accessNotConfigured is non-retryable."""
        body = json.dumps({
            "error": {
                "code": 403,
                "message": "Drive API has not been used in project before or it is disabled.",
                "errors": [{"reason": "accessNotConfigured"}]
            }
        }).encode("utf-8")
        perm_error = make_http_error(403, "Forbidden", content=body)
        self.assertFalse(is_retryable_upload_error(perm_error))

    def test_rate_limit_403_ratelimitexceeded_triggers_retries(self):
        """Verify 403 with reason rateLimitExceeded is recognized as retryable and retries."""
        body = json.dumps({
            "error": {
                "code": 403,
                "message": "User Rate Limit Exceeded. Rate of requests for user exceeded divide quota.",
                "errors": [
                    {
                        "message": "User Rate Limit Exceeded",
                        "domain": "usageLimits",
                        "reason": "rateLimitExceeded"
                    }
                ]
            }
        }).encode("utf-8")

        rate_limit_err = make_http_error(403, "Forbidden", content=body)
        self.assertTrue(
            _is_rate_limit_403(rate_limit_err),
            "rateLimitExceeded must be detected by _is_rate_limit_403",
        )
        self.assertTrue(
            is_retryable_upload_error(rate_limit_err),
            "rateLimitExceeded 403 must be retryable",
        )

        service = MockDriveService()
        attempts = [0]
        recorded_sleeps = []

        def rate_limited_list():
            attempts[0] += 1
            if attempts[0] <= 2:
                raise rate_limit_err
            return {"files": [{"id": "item_after_rate_limit", "name": "file.mp4"}]}

        service.files().list_handler = rate_limited_list

        with patch("time.sleep", side_effect=lambda s: recorded_sleeps.append(s)):
            result = list_remote_children(service, "parent", "file.mp4")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "item_after_rate_limit")
        self.assertEqual(attempts[0], 3, "Should succeed on 3rd attempt after 2 rateLimit retries")
        self.assertEqual(len(recorded_sleeps), 2)

    def test_rate_limit_403_userratelimitexceeded_triggers_retries(self):
        """Verify 403 with reason userRateLimitExceeded is recognized as retryable."""
        body = json.dumps({
            "error": {
                "code": 403,
                "message": "The user rate limit has been exceeded.",
                "errors": [
                    {
                        "message": "The user rate limit has been exceeded.",
                        "domain": "usageLimits",
                        "reason": "userRateLimitExceeded"
                    }
                ]
            }
        }).encode("utf-8")

        rate_limit_err = make_http_error(403, "Forbidden", content=body)
        self.assertTrue(_is_rate_limit_403(rate_limit_err))
        self.assertTrue(is_retryable_upload_error(rate_limit_err))

        service = MockDriveService()
        attempts = [0]

        def rate_limited_get():
            attempts[0] += 1
            if attempts[0] <= 3:
                raise rate_limit_err
            return {"id": "recovered_file_id", "name": "recovered.mp4"}

        service.files().get_handler = rate_limited_get

        with patch("time.sleep"):
            file_meta = get_remote_file(service, "recovered_file_id")

        self.assertEqual(file_meta["id"], "recovered_file_id")
        self.assertEqual(attempts[0], 4)

    def test_rate_limit_403_exhaustion_raises_after_max_retries(self):
        """Verify rate-limit 403 that persists beyond METADATA_MAX_RETRIES raises."""
        body = json.dumps({
            "error": {
                "errors": [{"reason": "rateLimitExceeded", "message": "Rate limit"}]
            }
        }).encode("utf-8")
        rate_limit_err = make_http_error(403, "Forbidden", content=body)

        service = MockDriveService()
        attempts = [0]

        def persistent_rate_limit():
            attempts[0] += 1
            raise rate_limit_err

        service.files().list_handler = persistent_rate_limit

        with patch("time.sleep"):
            with self.assertRaises(Exception):
                list_remote_children(service, "parent", "item")

        self.assertEqual(attempts[0], METADATA_MAX_RETRIES + 1)

    def test_rate_limit_403_plain_string_and_json_detection(self):
        """Verify _is_rate_limit_403 handles both plain string and JSON formats."""
        err1 = make_http_error(403, "Forbidden", content=b"userRateLimitExceeded")
        err2 = make_http_error(403, "Forbidden", content=b"Rate limit exceeded. Please wait.")
        err3 = make_http_error(403, "Forbidden", content=json.dumps({
            "error": {"message": "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute'"}
        }).encode("utf-8"))
        err4 = make_http_error(403, "Forbidden", content=b"Access denied: unauthorized project")

        self.assertTrue(_is_rate_limit_403(err1))
        self.assertTrue(_is_rate_limit_403(err2))
        self.assertTrue(_is_rate_limit_403(err3))
        self.assertFalse(_is_rate_limit_403(err4), "unauthorized project must NOT be retryable")


# ============================================================================
# Test Suite 3: Idempotent Folder Creation
# ============================================================================

class TestGoogleDriveFolderCreationIdempotency(unittest.TestCase):
    """
    Verify idempotent folder creation:
    Simulate network timeout after first create() and verify retry re-uses
    existing folder rather than creating a duplicate.
    """

    def test_idempotent_folder_creation_server_created_client_timeout(self):
        """
        Adversarial scenario:
        1. Pre-check list returns [] (folder does not exist initially).
        2. First create() succeeds on server, but client drops connection (socket.timeout).
        3. On retry 1, get_or_create_remote_folder queries list_remote_children FIRST.
        4. list_remote_children finds the folder created on step 2.
        5. Function returns folder ID without issuing another create() call!
        """
        service = MockDriveService()
        created_folder_id = "server_created_folder_uuid_9999"
        create_attempts = [0]

        def list_handler():
            # If create was attempted, the folder now exists on the server!
            if create_attempts[0] >= 1:
                return {
                    "files": [
                        {
                            "id": created_folder_id,
                            "name": "TargetFolder",
                            "mimeType": GOOGLE_FOLDER_MIME,
                        }
                    ]
                }
            return {"files": []}

        def create_handler():
            create_attempts[0] += 1
            # Simulate socket timeout after server handled request
            raise socket.timeout("Read timed out after server created folder")

        service.files().list_handler = list_handler
        service.files().create_handler = create_handler

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(service, "parent_100", "TargetFolder")

        self.assertEqual(folder_id, created_folder_id)
        # CRITICAL ASSERTION: create() must have been called EXACTLY ONCE!
        self.assertEqual(
            create_attempts[0],
            1,
            "Create must NOT be called a second time when folder was created on first try!",
        )
        self.assertEqual(
            len(service.files().create_calls),
            1,
            "No duplicate create call should be made on retry!",
        )

    def test_idempotent_folder_creation_pre_existing_folder(self):
        """If folder already exists before call, create() is called 0 times."""
        service = MockDriveService()
        service.files().list_handler = lambda: {
            "files": [{"id": "preexisting_id", "name": "ExistingDir", "mimeType": GOOGLE_FOLDER_MIME}]
        }

        folder_id = get_or_create_remote_folder(service, "root", "ExistingDir")
        self.assertEqual(folder_id, "preexisting_id")
        self.assertEqual(len(service.files().create_calls), 0)

    def test_idempotent_folder_creation_transient_failure_then_successful_create(self):
        """
        Scenario where first create fails BEFORE server creates folder (e.g. 503 error).
        On retry 1, list returns empty. Then second create succeeds.
        """
        service = MockDriveService()
        create_attempts = [0]

        service.files().list_handler = lambda: {"files": []}

        def flaky_create():
            create_attempts[0] += 1
            if create_attempts[0] == 1:
                raise make_http_error(503, "Service Unavailable")
            return {"id": "created_on_attempt_2"}

        service.files().create_handler = flaky_create

        with patch("time.sleep"):
            folder_id = get_or_create_remote_folder(service, "root", "FreshDir")

        self.assertEqual(folder_id, "created_on_attempt_2")
        self.assertEqual(create_attempts[0], 2)

    def test_concurrent_folder_creation_idempotency_stress(self):
        """
        Simulate 5 threads trying to get_or_create_remote_folder for the same folder simultaneously.
        First thread creates it; remaining threads find it. All 5 return identical ID.
        """
        service = MockDriveService()
        shared_state = {"folder_id": None, "create_count": 0, "lock": threading.Lock()}

        def thread_safe_list():
            with shared_state["lock"]:
                if shared_state["folder_id"]:
                    return {"files": [{"id": shared_state["folder_id"], "mimeType": GOOGLE_FOLDER_MIME}]}
                return {"files": []}

        def thread_safe_create():
            with shared_state["lock"]:
                if shared_state["folder_id"] is None:
                    shared_state["create_count"] += 1
                    shared_state["folder_id"] = f"concurrent_id_{shared_state['create_count']}"
                return {"id": shared_state["folder_id"]}

        service.files().list_handler = thread_safe_list
        service.files().create_handler = thread_safe_create

        results = []

        def worker():
            with patch("time.sleep"):
                fid = get_or_create_remote_folder(service, "parent_dir", "ConcurFolder")
                results.append(fid)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 5)
        # All returned IDs must be identical
        self.assertEqual(len(set(results)), 1, f"All threads must return same folder ID: {results}")
        self.assertEqual(shared_state["create_count"], 1)

    def test_query_escaping_in_list_remote_children(self):
        """Verify escape_query_text properly escapes special characters in Drive queries."""
        raw_name = "User's Folder \\ \"Special\" [Test]"
        escaped = escape_query_text(raw_name)
        self.assertEqual(escaped, "User\\'s Folder \\\\ \"Special\" [Test]")


# ============================================================================
# Test Suite 4: Dynamic Google Sheets Scale & Deduplication (>2000 rows)
# ============================================================================

class TestGoogleSheetsScaleAndDeduplication(unittest.TestCase):
    """
    Test reading sheets beyond 2000 rows (e.g. 5,000+ rows) and verify full deduplication.
    """

    def test_reading_and_deduplicating_sheet_with_5000_plus_rows(self):
        """
        Stress test: Sheet with 5,500 historical rows.
        - Rows 1..5500 populated with hyperlinks.
        - Target historical records placed at rows 2050, 3500, and 5490.
        - Input: 3 existing records + 2 brand new records.
        - Verify: Exactly 2 records are written, and 3 are deduplicated and skipped!
        """
        total_historical_rows = 5500
        header = ["日期", "提交人", "视频链接"]
        rows = [header]

        historical_ids = {
            2050: "historical_deep_2050",
            3500: "historical_deep_3500",
            5490: "historical_deep_5490",
        }

        for r in range(2, total_historical_rows + 1):
            if r in historical_ids:
                fid = historical_ids[r]
                rows.append(["2026-08-01", "OldStaff", f'=HYPERLINK("https://drive.google.com/file/d/{fid}/view", "video_{r}.mp4")'])
            else:
                rows.append(["2026-08-01", "OldStaff", f'=HYPERLINK("https://drive.google.com/file/d/filler_{r}/view", "f_{r}.mp4")'])

        mock_service = MockSheetsService(values=rows)
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/stress_sheet_5000/edit#gid=101",
            "review_sheet_date_column": "日期",
            "review_sheet_submitter_column": "提交人",
            "review_sheet_link_column": "视频链接",
            "review_sheet_submitter": "TestChallenger",
        }

        # 3 duplicate records + 2 brand new records
        records = [
            {"name": "v_2050.mp4", "webViewLink": f"https://drive.google.com/file/d/{historical_ids[2050]}/view"},
            {"name": "brand_new_1.mp4", "webViewLink": "https://drive.google.com/file/d/brand_new_1_id/view"},
            {"name": "v_3500.mp4", "webViewLink": f"https://drive.google.com/file/d/{historical_ids[3500]}/view"},
            {"name": "brand_new_2.mp4", "webViewLink": "https://drive.google.com/file/d/brand_new_2_id/view"},
            {"name": "v_5490.mp4", "webViewLink": f"https://drive.google.com/file/d/{historical_ids[5490]}/view"},
        ]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=mock_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written_count = write_review_video_links(config, records)

        # Verification: Only 2 brand new records written
        self.assertEqual(written_count, 2, "Must only write the 2 new records, skipping 3 duplicates past row 2000")

        # Verify dynamic read range was used without any Z2000 limit
        get_calls = mock_service.spreadsheets().values().get_calls
        self.assertEqual(len(get_calls), 1)
        self.assertEqual(get_calls[0]["range"], "'ReviewSheet'")
        self.assertNotIn("Z2000", get_calls[0]["range"])

        # Verify update call values
        update_calls = mock_service.spreadsheets().values().update_calls
        self.assertEqual(len(update_calls), 1)
        written_body = update_calls[0]["body"]["values"]
        self.assertEqual(len(written_body), 2)
        self.assertIn("brand_new_1_id", written_body[0][2])
        self.assertIn("brand_new_2_id", written_body[1][2])

    def test_10000_rows_extreme_scale_performance_and_deduplication(self):
        """
        Adversarial Scale Test: 10,000 rows in sheet.
        Verify deduplication operates efficiently (< 1.0s) and accurately.
        """
        rows = [["日期", "提交人", "视频链接"]]
        for i in range(1, 10001):
            rows.append(["2026-01-01", "User", f'=HYPERLINK("https://drive.google.com/file/d/scale_{i}/view", "clip_{i}.mp4")'])

        # Benchmark existing_links extraction
        start_t = time.time()
        links = existing_links(rows, link_col=3, header_row=1)
        elapsed = time.time() - start_t

        self.assertEqual(len(links), 10000)
        self.assertLess(elapsed, 1.0, f"Extracting 10,000 links must take < 1s, took {elapsed:.3f}s")

        # Test lookup
        self.assertIn("https://drive.google.com/file/d/scale_1/view", links)
        self.assertIn("https://drive.google.com/file/d/scale_5000/view", links)
        self.assertIn("https://drive.google.com/file/d/scale_10000/view", links)
        self.assertNotIn("https://drive.google.com/file/d/nonexistent/view", links)

    def test_all_duplicate_records_writes_zero_rows(self):
        """When all input records already exist in sheet, zero rows are written."""
        mock_service = MockSheetsService(values=[
            ["日期", "提交人", "视频链接"],
            ["2026-09-01", "User", '=HYPERLINK("https://drive.google.com/file/d/id_1/view", "v1.mp4")'],
            ["2026-09-02", "User", '=HYPERLINK("https://drive.google.com/file/d/id_2/view", "v2.mp4")'],
        ])
        config = {
            "review_sheet_enabled": True,
            "review_sheet_url": "https://docs.google.com/spreadsheets/d/dup_sheet/edit#gid=101",
        }
        records = [
            {"name": "v1.mp4", "webViewLink": "https://drive.google.com/file/d/id_1/view"},
            {"name": "v2.mp4", "webViewLink": "https://drive.google.com/file/d/id_2/view"},
        ]

        with patch("model.GoogleSheetsHelper.load_sheets_service", return_value=mock_service), \
             patch("model.GoogleSheetsHelper.remember_review_submissions"):
            written = write_review_video_links(config, records)

        self.assertEqual(written, 0)
        self.assertEqual(len(mock_service.spreadsheets().values().update_calls), 0)


# ============================================================================
# Test Suite 5: column_to_letter Wide Range Oracle Verification
# ============================================================================

class TestGoogleSheetsColumnToLetter(unittest.TestCase):
    """
    Test column_to_letter across wide ranges (1 to 2,000+).
    """

    @staticmethod
    def _reference_oracle(col_idx: int) -> str:
        """Independent reference bijective base-26 implementation."""
        assert col_idx >= 1
        chars = []
        while col_idx > 0:
            col_idx, rem = divmod(col_idx - 1, 26)
            chars.append(chr(ord('A') + rem))
        return "".join(reversed(chars))

    def test_column_to_letter_oracle_verification_range_1_to_2000(self):
        """Exhaustively verify column_to_letter against reference oracle from col 1 to 2,000."""
        for i in range(1, 2001):
            expected = self._reference_oracle(i)
            actual = column_to_letter(i)
            self.assertEqual(
                actual,
                expected,
                f"Column {i} mismatch: expected {expected}, got {actual}",
            )

    def test_column_to_letter_key_anchors_and_extreme_ranges(self):
        """Verify key standard anchors and extreme boundaries."""
        anchors = {
            1: "A",
            26: "Z",
            27: "AA",
            28: "AB",
            52: "AZ",
            53: "BA",
            702: "ZZ",
            703: "AAA",
            704: "AAB",
            1378: "AZZ",
            1379: "BAA",
            16384: "XFD",      # Excel & Google Sheets max column
            18278: "ZZZ",
            18279: "AAAA",
        }
        for col_idx, expected_letter in anchors.items():
            self.assertEqual(
                column_to_letter(col_idx),
                expected_letter,
                f"Failed for column index {col_idx}",
            )

    def test_column_to_letter_invalid_inputs_raise_value_error(self):
        """Non-positive indices must raise ValueError."""
        for invalid_idx in (0, -1, -50, -9999):
            with self.assertRaises(ValueError):
                column_to_letter(invalid_idx)

    def test_sheet_range_formatting(self):
        """Verify sheet_range formatting with quotes and cell coordinates."""
        self.assertEqual(sheet_range("Sheet1"), "'Sheet1'")
        self.assertEqual(sheet_range("Sheet1", "A1:B10"), "'Sheet1'!A1:B10")
        self.assertEqual(sheet_range("My'Sheet", "C5"), "'My''Sheet'!C5")


# ============================================================================
# Test Suite 6: Complete Absence of Z2000 in Codebase
# ============================================================================

class TestAbsenceOfZ2000(unittest.TestCase):
    """
    Verify complete absence of `Z2000` across all production code in the codebase.
    """

    def test_zero_occurrences_of_z2000_in_production_code(self):
        """Scan all .py source files in model/, PYUI/, app_plugins/ for Z2000."""
        search_dirs = [
            _WORKSPACE_ROOT / "model",
            _WORKSPACE_ROOT / "PYUI",
            _WORKSPACE_ROOT / "app_plugins",
        ]

        violations = []
        for sdir in search_dirs:
            if not sdir.exists():
                continue
            for py_file in sdir.rglob("*.py"):
                text = py_file.read_text(encoding="utf-8", errors="ignore")
                if "z2000" in text.lower():
                    violations.append(str(py_file.relative_to(_WORKSPACE_ROOT)))

        self.assertEqual(
            violations,
            [],
            f"Found forbidden Z2000 hardcoded limit in production files: {violations}",
        )


# ============================================================================
# Test Suite 7: Hermetic Network Isolation Verification
# ============================================================================

class TestHermeticNetworkIsolationEnforcement(unittest.TestCase):
    """
    Verify that the test environment strictly forbids live external network connections.
    """

    def test_hermetic_guard_blocks_external_ip_connections(self):
        """Verify outbound connections to external IPs raise Hermetic test network violation."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                s.connect(("8.8.8.8", 53))
            self.assertIn("Hermetic test network violation", str(ctx.exception))
            self.assertIn("forbidden", str(ctx.exception))
        finally:
            s.close()

    def test_hermetic_guard_blocks_external_domain_connections(self):
        """Verify outbound connections to googleapis.com raise Hermetic violation."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                s.connect(("www.googleapis.com", 443))
            self.assertIn("Hermetic test network violation", str(ctx.exception))
        finally:
            s.close()

    def test_hermetic_guard_allows_localhost(self):
        """Verify local loopback addresses (127.0.0.1, localhost) are not blocked by guard."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError) as ctx:
                s.connect(("127.0.0.1", 59999))
            self.assertNotIsInstance(ctx.exception, RuntimeError)
            self.assertNotIn("Hermetic test network violation", str(ctx.exception))
        finally:
            s.close()

    def test_real_service_loaders_require_explicit_mocking(self):
        """Verify calling unmocked load_sheets_service with nonexistent credentials raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            GoogleSheetsHelperMod.load_sheets_service({
                "review_sheet_credentials_file": "definitely_nonexistent_credentials_12345.json",
                "review_sheet_token_file": "definitely_nonexistent_token_12345.json",
            })


if __name__ == "__main__":
    unittest.main()
