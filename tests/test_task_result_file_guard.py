import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model import TaskResultFileGuard as guard
from model.GoogleDriveHelper import sync_directory_contents
from model.TaskResultExporter import copy_if_updated, export_task
from model.TaskResultOrganizer import (
    file_identity, load_pending_changed_file_batches, run_task_result_organizer,
    save_pending_changed_file_batches,
)


def complete_file(path, data=b"completed-video"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    older = time.time() - 10
    os.utime(path, (older, older))
    return path


@contextmanager
def external_writer(path):
    # A separate process with a paused writer. The mtime is old and readers
    # can open it: neither timestamp heuristics nor an ordinary read suffice.
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; f=open(sys.argv[1], 'r+b'); print('READY', flush=True); "
         "sys.stdin.readline(); f.close()", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        if process.stdout.readline().strip() != "READY":
            raise AssertionError("test writer failed to start")
        yield
    finally:
        try:
            process.communicate("stop\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        if process.returncode:
            raise AssertionError("test writer did not exit normally")


class TaskResultFileGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "1006" / "result"
        self.source = complete_file(self.root / "1006" / "task" / "1" / "final.mp4")
        self.target = self.output / "1.mp4"
        self.task = SimpleNamespace(task_id="1", admin="Admin", creator="Me",
                                    task_name="Title", task_type="video", task_date="1006")
        self.profile = {"mode": "single", "task_dir": "task",
                        "candidate_names": ["final.mp4"], "output_name_template": "{task_id}.mp4"}
        self.config = {"task_table_file_name": "tasks.ods", "task_export_profiles": {"video": self.profile},
                       "run_export": True, "run_upload": False, "upload_only_changed_files": True,
                       "task_result_pending_file": str(self.root / "pending.json"),
                       "enable_video_review_detection": False, "compress_enabled": False,
                       "drive_parent_folder_id": "test-parent", "review_sheet_enabled": False,
                       "video_filename_creator_marker": "TEST",
                       "task_submission_enabled": False, "video_upload_history_enabled": False}
        (self.root / "1006" / "tasks.ods").touch()
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def export(self, **kwargs):
        return export_task(self.task, date(2026, 10, 6), self.root / "1006", self.output,
                           self.root / "wsp", self.profile, {}, False, **kwargs)

    def organize(self, **kwargs):
        with patch("model.TaskResultExporter.ReadTaskOds", return_value=[self.task]):
            return run_task_result_organizer([date(2026, 10, 6)], self.root,
                                             {**self.config, **kwargs})

    def test_completed_copy_is_atomic_and_unchanged_is_not_reexported(self):
        old = complete_file(self.target, b"old")
        original_replace = os.replace
        def commit(temporary, destination):
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(Path(temporary).read_bytes(), self.source.read_bytes())
            self.assertTrue(guard.is_copy_temporary(temporary))
            original_replace(temporary, destination)
        with patch.object(guard.os, "replace", side_effect=commit):
            self.assertTrue(copy_if_updated(self.source, self.target))
        self.assertFalse(copy_if_updated(self.source, self.target))
        self.assertEqual(list(self.output.glob(".task-result-*.tmp")), [])

    def test_recent_video_is_deferred_without_waiting_or_marking_exported(self):
        self.source.write_bytes(b"just-rendered")
        remembered, deferred = [], []
        with patch.object(guard.time, "sleep", side_effect=AssertionError("must not wait")):
            self.assertEqual(self.export(exported_file_callback=lambda *x: remembered.append(x),
                                         deferred_file_callback=lambda *x: deferred.append(x)), [])
        self.assertEqual(remembered, [])
        self.assertEqual(len(deferred), 1)
        self.assertFalse(self.target.exists())

    def test_empty_video_is_deferred_but_empty_nonvideo_can_be_copied(self):
        complete_file(self.source, b"")
        complete_file(self.target, b"")
        self.assertEqual(self.export(), [])
        notes = complete_file(self.root / "empty.txt", b"")
        self.assertTrue(copy_if_updated(notes, self.root / "copy.txt"))

    def test_changed_source_discards_snapshot_and_preserves_old_output(self):
        complete_file(self.target, b"old-intact")
        real_copy = guard.shutil.copy2
        def copying(src, dst):
            real_copy(src, dst)
            os.utime(src, (time.time(), time.time()))
        with patch.object(guard.shutil, "copy2", side_effect=copying):
            with self.assertRaises(guard.FileStillWritingError):
                copy_if_updated(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"old-intact")
        self.assertEqual(list(self.output.glob(".task-result-*.tmp")), [])

    def test_real_copy_failure_is_not_disguised_as_rendering(self):
        complete_file(self.target, b"old-intact")
        def fail_copy(_src, dst):
            Path(dst).write_bytes(b"partial")
            raise OSError("disk full")
        with patch.object(guard.shutil, "copy2", side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.export()
        self.assertEqual(self.target.read_bytes(), b"old-intact")
        self.assertEqual(list(self.output.glob(".task-result-*.tmp")), [])

    def test_busy_destination_defers_without_overwriting_or_leaking_temporary(self):
        complete_file(self.target, b"old-intact")
        error = PermissionError("busy")
        error.winerror = 32
        with patch.object(guard.os, "replace", side_effect=error):
            self.assertEqual(self.export(), [])
        self.assertEqual(self.target.read_bytes(), b"old-intact")
        self.assertEqual(list(self.output.glob(".task-result-*.tmp")), [])
        self.assertEqual(self.export(), [self.target])

    def test_missing_source_during_open_is_deferred(self):
        self.source.unlink()
        with self.assertRaises(guard.FileStillWritingError):
            copy_if_updated(self.source, self.target)

    def test_all_videos_keeps_existing_numbering_when_one_is_deferred(self):
        second = complete_file(self.source.parent / "other.mp4")
        self.profile.update(mode="all_videos", output_name_template="{task_id}-{index}.mp4")
        paths = list(self.source.parent.rglob("*.mp4"))
        held = paths[0]
        real_copy = guard.copy_if_ready_and_updated
        def copy(src, dst):
            if Path(src) == held:
                raise guard.FileStillWritingError("writer")
            return real_copy(src, dst)
        with patch("model.TaskResultExporter.copy_if_ready_and_updated", side_effect=copy):
            first = self.export()
        self.assertEqual([x.name for x in first], ["1-2.mp4"])
        self.assertEqual([x.name for x in self.export()], ["1-1.mp4"])
        self.assertEqual(second.read_bytes(), b"completed-video")

    def test_copy_tree_skips_only_busy_file_and_never_registers_it(self):
        complete_file(self.source.parent / "notes.txt", b"notes")
        self.source.write_bytes(b"still-new")
        self.profile.update(mode="copy_tree", output_directory_template="{task_id}")
        remembered = []
        result = self.export(exported_file_callback=lambda path, _task: remembered.append(path))
        self.assertEqual([x.name for x in result], ["notes.txt"])
        self.assertEqual(remembered, result)

    def test_full_upload_recursive_exclusions_and_copy_temporary(self):
        complete_file(self.output / "nested" / "good.mp4")
        blocked = complete_file(self.output / "nested" / "blocked.mp4")
        complete_file(self.output / ".task-result-leftover.tmp")
        with patch("model.GoogleDriveHelper.get_or_create_remote_folder", return_value="folder"), \
                patch("model.GoogleDriveHelper.sync_file") as sync:
            sync_directory_contents(object(), self.output, "parent", excluded_files={file_identity(blocked)})
        self.assertEqual([call.args[1].name for call in sync.call_args_list], ["good.mp4"])

    def test_full_mode_excludes_old_output_and_compressed_sibling(self):
        complete_file(self.target, b"old")
        compressed = complete_file(self.output / "[SHANA]1.mp4", b"old-compressed")
        safe = complete_file(self.output / "other.mp4")
        self.source.write_bytes(b"new-render")
        with patch("model.TaskResultOrganizer.load_drive_service", return_value=object()), \
                patch("model.TaskResultOrganizer.upload_local_dirs_to_drive_batch") as upload:
            result = self.organize(run_upload=True, upload_only_changed_files=False)
        exclusions = upload.call_args.kwargs["excluded_files"]
        self.assertIn(file_identity(self.target), exclusions)
        self.assertIn(file_identity(compressed), exclusions)
        self.assertNotIn(file_identity(safe), exclusions)
        self.assertEqual(result["deferred_file_count"], 1)

    def test_only_busy_files_does_not_open_network(self):
        self.source.write_bytes(b"recent")
        with patch("model.TaskResultOrganizer.load_drive_service") as load, \
                patch("model.TaskResultOrganizer.pending_review_recovery_records", return_value=[]):
            result = self.organize(run_upload=True, drive_parent_folder_id="")
        load.assert_not_called()
        self.assertEqual(result["deferred_file_count"], 1)
        self.assertEqual(result["changed_file_count"], 0)

    def test_full_mode_only_busy_files_does_not_open_network(self):
        complete_file(self.target, b"old")
        self.source.write_bytes(b"recent")
        with patch("model.TaskResultOrganizer.load_drive_service") as load:
            result = self.organize(run_upload=True, upload_only_changed_files=False)
        load.assert_not_called()
        self.assertEqual(result["deferred_file_count"], 1)

    def test_direct_result_mode_detects_a_recent_render(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"direct-render")
        dates = [date(2026, 10, 6)]
        save_pending_changed_file_batches(self.config, self.root, dates, [(self.output, [self.target])])
        with patch("model.TaskResultOrganizer.load_drive_service") as load, \
                patch("model.TaskResultOrganizer.pending_review_recovery_records", return_value=[]):
            result = self.organize(run_upload=True, run_export=False)
        load.assert_not_called()
        self.assertEqual(result["deferred_file_count"], 1)
        self.assertEqual(load_pending_changed_file_batches(self.config, self.root, dates),
                         [(self.output, [self.target])])

    def test_manual_skip_of_safe_files_keeps_busy_pending(self):
        complete_file(self.target, b"old")
        safe = complete_file(self.output / "safe.mp4")
        dates = [date(2026, 10, 6)]
        save_pending_changed_file_batches(self.config, self.root, dates, [(self.output, [self.target, safe])])
        self.source.write_bytes(b"new-render")
        with patch("model.TaskResultOrganizer.build_routed_batches", return_value=[]), \
                patch("model.TaskResultOrganizer.load_drive_service") as load:
            self.organize(run_upload=True)
        load.assert_not_called()
        self.assertEqual(load_pending_changed_file_batches(self.config, self.root, dates),
                         [(self.output, [self.target])])

    def test_readonly_source_can_be_copied(self):
        try:
            self.source.chmod(0o400)
            self.assertTrue(copy_if_updated(self.source, self.target))
        finally:
            self.source.chmod(0o600)
            if self.target.exists():
                self.target.chmod(0o600)

    def test_safe_upload_leaves_busy_old_pending_entry_for_later(self):
        complete_file(self.target, b"old")
        safe = complete_file(self.output / "safe.mp4")
        dates = [date(2026, 10, 6)]
        save_pending_changed_file_batches(self.config, self.root, dates, [(self.output, [self.target, safe])])
        self.source.write_bytes(b"new-render")
        with patch("model.TaskResultOrganizer.load_drive_service", return_value=object()), \
                patch("model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch", return_value=[]) as upload, \
                patch("model.TaskResultOrganizer.pending_review_recovery_records", return_value=[]), \
                patch("model.TaskResultOrganizer.print_person_folder_links", return_value={}):
            self.organize(run_upload=True)
        received = [path for _root, files, _prefix in upload.call_args.args[1] for path in files]
        self.assertEqual(received, [safe])
        waiting = load_pending_changed_file_batches(self.config, self.root, dates)
        self.assertEqual(waiting, [(self.output, [self.target])])
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_cancel_keeps_safe_and_busy_pending_entries(self):
        complete_file(self.target, b"old")
        safe = complete_file(self.output / "safe.mp4")
        dates = [date(2026, 10, 6)]
        save_pending_changed_file_batches(self.config, self.root, dates, [(self.output, [self.target, safe])])
        self.source.write_bytes(b"new-render")
        with patch("model.TaskResultExporter.ReadTaskOds", return_value=[self.task]), \
                patch("model.TaskResultOrganizer.load_drive_service") as load:
            result = run_task_result_organizer(dates, self.root,
                {**self.config, "run_upload": True, "enable_video_review_detection": True, "video_detection_mode": "ask"},
                detection_mode_resolver=lambda _files: "cancel")
        load.assert_not_called()
        self.assertTrue(result["cancelled"])
        files = load_pending_changed_file_batches(self.config, self.root, dates)[0][1]
        self.assertEqual(set(files), {safe, self.target})

    @unittest.skipUnless(os.name == "nt", "mandatory Windows file sharing")
    def test_external_paused_writer_skipped_then_retried_without_resolve(self):
        other = complete_file(self.source.parent.parent / "2" / "final.mp4", b"other-video")
        task2 = SimpleNamespace(**{**vars(self.task), "task_id": "2"})
        with external_writer(self.source):
            # Deliberately demonstrate that plain read access is not a test.
            self.assertEqual(self.source.read_bytes(), b"completed-video")
            with patch("model.TaskResultExporter.ReadTaskOds", return_value=[self.task, task2]):
                first = run_task_result_organizer([date(2026, 10, 6)], self.root, self.config)
            self.assertFalse(self.target.exists())
            self.assertEqual(first["changed_file_count"], 1)
            self.assertEqual(first["deferred_file_count"], 1)
            self.assertEqual(first["deferred_files"][0]["source"], str(self.source))
        second = self.organize()
        self.assertEqual(second["changed_file_count"], 1)
        self.assertEqual(second["deferred_file_count"], 0)
        self.assertEqual(self.target.read_bytes(), b"completed-video")
        self.assertEqual(other.read_bytes(), b"other-video")

    @unittest.skipUnless(os.name == "nt", "mandatory Windows file sharing")
    def test_lease_blocks_writers_until_copy_finishes_and_releases_afterwards(self):
        with guard.read_without_writer(self.source):
            with self.assertRaises(PermissionError):
                with self.source.open("r+b"):
                    self.fail("writer should not be allowed")
        with self.source.open("r+b") as handle:
            handle.write(b"ok")

    @unittest.skipUnless(os.name == "nt", "mandatory Windows file sharing")
    def test_busy_source_does_not_erase_existing_output_or_register_it(self):
        complete_file(self.target, b"previous-final")
        remembered = []
        with external_writer(self.source):
            self.assertEqual(self.export(exported_file_callback=lambda *x: remembered.append(x)), [])
        self.assertEqual(remembered, [])
        self.assertEqual(self.target.read_bytes(), b"previous-final")

    @unittest.skipUnless(os.name == "nt", "mandatory Windows file sharing")
    def test_writer_can_start_during_large_copy_then_snapshot_is_discarded(self):
        complete_file(self.target, b"previous-final")
        writer = external_writer(self.source)
        real_copy = guard.shutil.copy2
        entered = False
        def copying(src, dst):
            nonlocal entered
            writer.__enter__()
            entered = True
            real_copy(src, dst)
        try:
            with patch.object(guard.shutil, "copy2", side_effect=copying):
                self.assertEqual(self.export(), [])
            self.assertEqual(self.target.read_bytes(), b"previous-final")
            self.assertEqual(list(self.output.glob(".task-result-*.tmp")), [])
        finally:
            if entered:
                writer.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
