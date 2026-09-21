# Test Infrastructure & Methodology (`TEST_INFRA.md`)

## 1. Executive Summary

This document describes the test infrastructure, methodology, architecture, and comprehensive test case inventory for the Bugfix and Stabilization track of the desktop application (`辅助小工具`).

The test suite is implemented in `tests/test_e2e_stabilization.py` and provides opaque-box end-to-end verification covering Requirements R1 through R5 across a rigorous 4-Tier Testing Methodology.

---

## 2. 4-Tier Testing Methodology

The testing strategy is structured into four distinct verification tiers:

| Tier | Focus | Coverage Target | Purpose |
|---|---|---|---|
| **Tier 1: Feature Coverage** | Core Requirements & Signatures | $\ge 5$ test cases per feature (R1–R5) = 25 tests | Validates primary behavior, method signatures, thread dispatches, and interface contracts. |
| **Tier 2: Boundary & Corner Cases** | Edge Limits, Errors, Scalability | $\ge 5$ test cases per feature (R1–R5) = 25 tests | Validates boundary extremes: empty inputs, missing paths, transient network retries, high concurrency contention, and corrupted payloads. |
| **Tier 3: Cross-Feature Combinations** | Pairwise Inter-Module Interactions | 5 pairwise tests | Verifies that simultaneous execution across modules (e.g., R2 inventory RMW during R5 audit logging) does not deadlock or contend on shared resources. |
| **Tier 4: Real-World Application Scenarios** | Full End-to-End Workflows | 4 end-to-end scenarios | Verifies full business pipelines, stress storms, network fault recovery, and UI application lifecycles. |

**Total Test Inventory:** 59 Test Cases.

---

## 3. Test Harness Architecture & Test Doubles

### 3.1 Headless PyQt5 Execution
- **Environment Isolation:** Configured with `os.environ["QT_QPA_PLATFORM"] = "offscreen"`.
- **GUI Event Lifecycle:** `QtWidgets.QApplication.instance() or QtWidgets.QApplication([])` initializes an offscreen Qt application.
- **`create_test_main_window()` Context Manager:** Safely stubs system-level tray notifications (`setupNotificationTray`), global hotkey registrations (`FakeHotkeyManager`), and config persistence during window initialization and teardown. Ensures any background threads are interrupted and cleanly waited on (`thread.wait()`) prior to window closure.

### 3.2 Google API Mock Doubles (`MockDriveService` & `MockSheetsService`)
- **Zero External Network Dependency:** All Google Drive (`v3`) and Google Sheets (`v4`) calls are mocked with Python test doubles implementing zero-argument `.execute()` semantics (compatible with `FakeRequest` and project test conventions).
- **Stateful Network Error Injection:** Supports configurable retry simulation for HTTP `429`, `500`, `502`, `503`, `504`, `socket.timeout`, `ssl.SSLError`, `ConnectionResetError`, and immediate failure on non-retryable HTTP `400`, `401`, `403`, `404`.
- **Fast Execution:** All backoff sleeps (`time.sleep`) are patched during retry tests, executing full 5-retry sequences in sub-millisecond time.

### 3.3 Disk & Concurrency Isolation
- **Filesystem Sandbox:** Every test method utilizes a dedicated `tempfile.TemporaryDirectory()`, creating isolated JSON state files and JSONL audit logs.
- **Multithreading Protection:** Concurrency tests employ `threading.Thread` pools with explicit joins, testing race conditions under process-wide `threading.RLock` synchronization.

---

## 4. Complete Test Case Inventory

### Tier 1: Feature Coverage (25 Tests)

#### Requirement R1: Video Assignment UI Threading (`PYUI/main_pyui.py`)
1. `test_tier1_r1_01_video_assignment_thread_class_structure`:
   - **Target**: `VideoAssignmentThread(QtCore.QThread)`
   - **Verification**: Inherits `QThread`; defines `log`, `completed`, `failed` signals; initializes threshold and directories.
2. `test_tier1_r1_02_assign_video_signature_preserved`:
   - **Target**: `MainDialog.assignVideo(self)`
   - **Verification**: Signature retains `def assignVideo(self)` without mandatory positional parameters.
3. `test_tier1_r1_03_assign_video_spawns_thread_and_disables_button`:
   - **Target**: UI button state & thread instantiation
   - **Verification**: Spawns `VideoAssignmentThread`, disables `assign_video_btn`, updates text to '正在分拣…'.
4. `test_tier1_r1_04_thread_emits_log_signals_instead_of_direct_ui_call`:
   - **Target**: Signal-based decoupled logging
   - **Verification**: Thread emits `log` signals instead of directly mutating GUI text widgets across thread boundaries.
5. `test_tier1_r1_05_thread_completion_triggers_cleanup_and_reenables_button`:
   - **Target**: Thread lifecycle and slot cleanup
   - **Verification**: `onAssignVideoFinished` re-enables button, restores text, and clears thread instance.

#### Requirement R2: InventoryStore RMW Locking (`model/InventoryManager.py`)
6. `test_tier1_r2_01_path_keyed_lock_registry`:
   - **Target**: `get_inventory_store_lock(path)`
   - **Verification**: Canonical paths share the same `threading.RLock`; distinct paths receive separate locks.
7. `test_tier1_r2_02_inventory_store_exposes_lock_property`:
   - **Target**: `InventoryStore.lock`
   - **Verification**: Store instance exposes path-keyed lock matching global registry.
8. `test_tier1_r2_03_add_item_is_thread_safe_under_lock`:
   - **Target**: `InventoryStore.add_item`
   - **Verification**: Adds item under lock and persists state cleanly to disk.
9. `test_tier1_r2_04_add_stock_concurrent_increments`:
   - **Target**: `InventoryStore.add_stock`
   - **Verification**: 5 concurrent threads increment stock; verifies 100% sum retention without lost updates.
10. `test_tier1_r2_05_atomic_write_uses_unique_temporary_file`:
    - **Target**: `InventoryStore._write`
    - **Verification**: Generates UUID-based unique temporary filename (`.tmp`) prior to `os.replace` to prevent Windows `WinError 32` collisions.

#### Requirement R3: Dynamic Sheets Grid Range (`model/GoogleSheetsHelper.py`)
11. `test_tier1_r3_01_sheet_range_default_empty_a1`:
    - **Target**: `sheet_range(sheet_name)`
    - **Verification**: Default `a1=""` returns quoted sheet name `'SheetName'` without cell bounds.
12. `test_tier1_r3_02_sheet_range_with_explicit_a1`:
    - **Target**: `sheet_range(sheet_name, a1)`
    - **Verification**: Returns `'SheetName'!A1:D10`.
13. `test_tier1_r3_03_column_to_letter_mapping`:
    - **Target**: `column_to_letter(col_index)`
    - **Verification**: 1 -> 'A', 26 -> 'Z', 27 -> 'AA', 28 -> 'AB', 52 -> 'AZ'.
14. `test_tier1_r3_04_write_review_video_links_dynamic_read_range`:
    - **Target**: `write_review_video_links`
    - **Verification**: Issues dynamic range `'Sheet1'` to Sheets API without hardcoded `Z2000`.
15. `test_tier1_r3_05_absence_of_z2000_in_source`:
    - **Target**: Source code inspection
    - **Verification**: `model/GoogleSheetsHelper.py` contains zero occurrences of `"Z2000"`.

#### Requirement R4: Drive Metadata Retry & Idempotency (`model/GoogleDriveHelper.py`)
16. `test_tier1_r4_01_list_remote_children_retries_transient_error`:
    - **Target**: `list_remote_children`
    - **Verification**: Retries on transient `socket.timeout` and recovers on attempt 2.
17. `test_tier1_r4_02_get_remote_file_retries_and_reraises_404`:
    - **Target**: `get_remote_file`
    - **Verification**: Re-raises HTTP 404 immediately without retry looping.
18. `test_tier1_r4_03_metadata_max_retries_constant`:
    - **Target**: `METADATA_MAX_RETRIES`
    - **Verification**: Defined with value 5.
19. `test_tier1_r4_04_get_or_create_remote_folder_idempotent_on_retry`:
    - **Target**: `get_or_create_remote_folder`
    - **Verification**: Queries `list_remote_children` upon retry before creating, avoiding duplicate folder creation.
20. `test_tier1_r4_05_get_or_create_remote_folder_returns_existing_without_duplicate`:
    - **Target**: Pre-creation existence check
    - **Verification**: Returns existing folder ID immediately with 0 create calls.

#### Requirement R5: Task Submission Audit Locking (`model/TaskSubmissionHelper.py`)
21. `test_tier1_r5_01_audit_log_lock_defined`:
    - **Target**: `_AUDIT_LOG_LOCK`
    - **Verification**: Module-level `threading.RLock` instance is defined.
22. `test_tier1_r5_02_append_audit_events_acquires_lock`:
    - **Target**: `append_audit_events`
    - **Verification**: Acquires `_AUDIT_LOG_LOCK` during file append.
23. `test_tier1_r5_03_append_audit_events_preserves_batch_fsync`:
    - **Target**: Crash recovery guarantee
    - **Verification**: Preserves batch `os.fsync` exactly once per call.
24. `test_tier1_r5_04_append_audit_events_valid_jsonl_output`:
    - **Target**: Log file format
    - **Verification**: Generates valid, parseable JSONL records.
25. `test_tier1_r5_05_append_audit_events_respects_custom_config_path`:
    - **Target**: Config path resolution
    - **Verification**: Custom log path is respected.

---

### Tier 2: Boundary & Corner Cases (25 Tests)

#### Requirement R1 Boundaries
26. `test_tier2_r1_01_empty_today_dir_no_images`: Empty image directory completes with (0,0) without errors.
27. `test_tier2_r1_02_empty_video_root_no_videos`: Empty video directory completes with processed=0, moved=0.
28. `test_tier2_r1_03_nonexistent_paths_prevent_thread_launch`: Missing directories show error and return cleanly.
29. `test_tier2_r1_04_corrupt_video_frame_gracefully_skipped`: Corrupt video frames returning `None` are skipped.
30. `test_tier2_r1_05_target_file_collision_appends_match_suffix`: Colliding target file renames with `_match` suffix.

#### Requirement R2 Boundaries
31. `test_tier2_r2_01_add_item_empty_name_raises_value_error`: Whitespace/empty name raises `ValueError` and releases lock.
32. `test_tier2_r2_02_add_item_duplicate_name_case_insensitive`: Case-insensitive duplicate name raises `ValueError`.
33. `test_tier2_r2_03_add_stock_negative_or_zero_amount`: Stock increment $\le 0$ raises `ValueError`.
34. `test_tier2_r2_04_operations_on_nonexistent_item_raise_key_error`: Unknown item IDs raise `KeyError`.
35. `test_tier2_r2_05_high_contention_concurrent_rmw`: 10 threads hammering mixed RMW mutations maintain state consistency.

#### Requirement R3 Boundaries
36. `test_tier2_r3_01_empty_sheet_values`: Empty sheet grid handles cleanly without `IndexError`.
37. `test_tier2_r3_02_sheet_beyond_2000_rows_deduplication`: Deduplicates historical record at row 2050 (beyond 2000-row barrier).
38. `test_tier2_r3_03_sheet_name_with_single_quotes_and_spaces`: Escapes quotes in sheet name (`'Bob''s Review Sheet'`).
39. `test_tier2_r3_04_column_to_letter_wide_columns`: Converts wide columns: 53 -> 'BA', 702 -> 'ZZ', 703 -> 'AAA'.
40. `test_tier2_r3_05_missing_headers_graceful_fallback`: Missing header names fall back to defaults gracefully.

#### Requirement R4 Boundaries
41. `test_tier2_r4_01_max_retries_exhaustion_raises_original_error`: Retries exhausted (attempt 1 + 3 retries) re-raises error.
42. `test_tier2_r4_02_non_retryable_400_bad_request_fails_immediately`: 400 Bad Request fails on attempt 1 without retry.
43. `test_tier2_r4_03_folder_name_with_special_characters_escaped`: Folder name with quotes and backslashes is safely escaped.
44. `test_tier2_r4_04_idempotent_folder_creation_transient_failure_network_reset`: SSLError recovers via retry list check.
45. `test_tier2_r4_05_list_remote_children_empty_results`: Empty list results return empty list `[]`.

#### Requirement R5 Boundaries
46. `test_tier2_r5_01_append_empty_events_list`: `events=[]` creates file, writes 0 lines without error.
47. `test_tier2_r5_02_append_unicode_and_special_characters`: Emojis, Chinese characters, and quotes persist in UTF-8.
48. `test_tier2_r5_03_append_non_serializable_objects_via_default_str`: `Path`, `datetime`, `UUID` serialize via `default=str`.
49. `test_tier2_r5_04_large_batch_audit_logging`: Batch of 500 events writes all 500 lines atomically.
50. `test_tier2_r5_05_write_failure_propagates_and_releases_lock`: Disk write error propagates `OSError` and releases lock.

---

### Tier 3: Cross-Feature Combinations (5 Tests)
51. `test_tier3_pair_r2_r5_concurrent_inventory_and_audit_logging`:
    Concurrent threads executing inventory stock additions and audit logging simultaneously without cross-lock contention.
52. `test_tier3_pair_r3_r4_drive_folder_retrieval_and_sheets_sync`:
    Google Drive folder resolution with retries directly feeds Google Sheets dynamic review link update.
53. `test_tier3_pair_r1_r5_video_assignment_with_concurrent_audit_logging`:
    Video assignment background thread executes while audit events are concurrently appended to JSONL.
54. `test_tier3_pair_r2_r3_inventory_lookup_during_sheets_update`:
    Inventory state read transactions execute concurrently during Sheets row compilation.
55. `test_tier3_pair_r4_r5_drive_failure_logging_to_audit`:
    Drive metadata retry exhaustion triggers structured audit logging of the failure event.

---

### Tier 4: Real-World Application Scenarios (4 Tests)
56. `test_tier4_scenario1_full_task_submission_pipeline`:
    End-to-end task submission pipeline: Drive folder lookup/creation with network retry -> Sheets dynamic range write -> Audit logging with fsync.
57. `test_tier4_scenario2_multi_subsystem_stress_storm`:
    Simultaneous execution of VideoAssignmentThread, InventoryStore replenishment, and audit logging across 10 threads.
58. `test_tier4_scenario3_flaky_network_recovery_workflow`:
    Flaky network environment simulating 503 errors and connection drops, recovering idempotently.
59. `test_tier4_scenario4_ui_lifecycle_and_thread_teardown`:
    Desktop window lifecycle: VideoAssignmentThread launch, UI responsiveness, and clean shutdown.

---

## 5. How to Run the Tests

The test suite can be run using Python's standard `unittest` framework:

```powershell
# Run the complete E2E stabilization test suite directly:
python tests/test_e2e_stabilization.py

# Or run via unittest module discovery:
python -m unittest discover -s tests -p "test_e2e_stabilization.py"

# Or run a specific test tier (e.g. Tier 1):
python -m unittest tests.test_e2e_stabilization.Tier1FeatureCoverageTests
```
