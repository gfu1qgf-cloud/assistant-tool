"""Reject duplicate drafts before submission; race failures stay visible and safe."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from model.InventoryManager import InventoryStore, MaterialNameConflictError
from model.InventoryBackground import inventory_resources
from PYUI.utility_managers_pyui import InventoryManagerDialog, MaterialCopyThread
from tests import test_background_plugin_jobs as support


class MaterialSubmissionValidationTests(unittest.TestCase):
    setUpClass = classmethod(support.BackgroundPluginJobsTests.setUpClass.__func__)
    setUp = support.BackgroundPluginJobsTests.setUp
    tearDown = support.BackgroundPluginJobsTests.tearDown
    wait = support.BackgroundPluginJobsTests.wait
    submit_hold = support.BackgroundPluginJobsTests.submit_hold

    def set_up_dialog(self, *, standalone=False):
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        dialog = InventoryManagerDialog(store, context=None if standalone else self.context)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(lambda: [box.close() for box in list(dialog._material_error_dialogs.values())])
        return dialog, store

    def draft(self, dialog, name, source="https://drive.google.com/drive/folders/test"):
        dialog.material_name_edit.setText(name)
        dialog.material_sources_edit.clear()
        dialog.material_sources_edit.add_paths([str(source)])

    def source(self, name):
        source = self.root / name
        source.write_bytes(name.encode())
        return source

    def assert_rejected_without_submission(self, dialog, name, warning_text):
        paths = dialog.material_sources_edit.paths()
        with patch.object(self.context, "submit_background_task") as submit, \
             patch("PYUI.utility_managers_pyui.MaterialCopyThread") as worker, \
             patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog.add_material()
        submit.assert_not_called()
        worker.assert_not_called()
        warning.assert_called_once()
        self.assertIn(warning_text, warning.call_args.args[2])
        self.assertEqual(dialog.material_name_edit.text(), name)
        self.assertEqual(dialog.material_sources_edit.paths(), paths)
        self.assertIsNone(dialog.material_copy_thread)
        self.assertIsNone(dialog.material_copy_operation)

    def test_store_preflight_is_normalized_and_read_only(self):
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        self.assertEqual(store.validate_material_name("  New  "), "New")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            store.validate_material_name(" ")
        self.assertFalse(store.path.exists())
        self.assertFalse(store.material_root.exists())
        source = self.source("existing.txt")
        store.add_material("Unique", [source])
        before = store.path.read_bytes()
        files_before = set(store.material_root.rglob("*"))
        with self.assertRaises(MaterialNameConflictError):
            store.validate_material_name("  UNIQUE ")
        with self.assertRaises(MaterialNameConflictError):
            store.add_material("unique", [source])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(set(store.material_root.rglob("*")), files_before)

    def test_existing_material_checked_from_fresh_disk_state_and_draft_preserved(self):
        dialog, store = self.set_up_dialog()
        # Added after the UI's last refresh: checking just the table would miss it.
        external = InventoryStore(store.path, store.material_root)
        external.add_material("唯一-男主播", [self.source("existing.txt")])
        before = store.path.read_bytes()
        self.draft(dialog, "唯一-男主播")
        self.assert_rejected_without_submission(dialog, "唯一-男主播", "追加素材")
        self.assertEqual(store.path.read_bytes(), before)

    def test_standalone_submit_also_rejects_case_insensitive_duplicate(self):
        dialog, store = self.set_up_dialog(standalone=True)
        store.add_material("Unique", [self.source("existing.txt")])
        self.draft(dialog, "  UNIQUE  ")
        self.assert_rejected_without_submission(dialog, "  UNIQUE  ", "同名素材")

    def test_pending_name_checked_before_worker_or_queue_and_released_on_cancel(self):
        dialog, store = self.set_up_dialog()
        hold, release = self.submit_hold(inventory_resources(store))
        self.addCleanup(release.set)
        self.draft(dialog, "Unique")
        dialog.add_material()
        first = dialog.material_job_handles()[0]
        self.assertEqual(self.manager.get(first.task_id)["status"], "queued")
        self.draft(dialog, "  UNIQUE  ")
        self.assert_rejected_without_submission(dialog, "  UNIQUE  ", "排队或导入")
        first.requestInterruption()
        self.assertFalse(dialog.material_background_jobs)
        self.assertFalse(dialog._material_error_dialogs)
        dialog.add_material()
        retry = dialog.material_job_handles()[0]
        self.assertNotEqual(first.task_id, retry.task_id)
        retry.requestInterruption()
        release.set()
        self.wait(lambda: not self.manager.is_busy())

    def test_running_name_also_rejected_without_locking_different_drafts(self):
        dialog, store = self.set_up_dialog()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def operation(_store, payload, _progress):
            started.set()
            release.wait(3)
            return {"id": "test", "name": payload["name"]}
        with patch("model.InventoryBackground.material_operation", side_effect=operation), \
             patch.object(dialog, "refresh_materials"):
            self.draft(dialog, "Unique")
            dialog.add_material()
            self.wait(started.is_set)
            self.draft(dialog, "unique")
            self.assert_rejected_without_submission(dialog, "unique", "排队或导入")
            self.assertTrue(dialog.material_submit_btn.isEnabled())
            release.set()
            self.wait(lambda: not self.manager.is_busy())

    def test_post_submission_race_shows_main_thread_nonmodal_error_and_later_job_runs(self):
        dialog, store = self.set_up_dialog()
        hold, release = self.submit_hold(inventory_resources(store))
        self.addCleanup(release.set)
        original = self.source("original.txt")
        self.draft(dialog, "Unique", original)
        dialog.add_material()
        first = dialog.material_job_handles()[0]
        self.draft(dialog, "Later", self.source("later.txt"))
        dialog.add_material()
        second = dialog.material_job_handles()[1]
        # A separate writer can win after preflight: backend checks must remain.
        store.add_material("Unique", [original])
        shown_threads = []
        show_error = dialog._show_background_material_error
        def capture(*args):
            shown_threads.append(threading.get_ident())
            return show_error(*args)
        dialog.close()
        with patch.object(dialog, "_show_background_material_error", side_effect=capture):
            release.set()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(first.task_id)["status"], "failed")
        self.assertEqual(self.manager.get(second.task_id)["status"], "succeeded")
        self.assertEqual(shown_threads, [threading.get_ident()])
        box = dialog._material_error_dialogs[first.task_id]
        self.assertEqual(box.parent(), self.window)
        self.assertTrue(box.isVisible())
        self.assertFalse(box.isModal())
        self.assertEqual(box.windowModality(), QtCore.Qt.NonModal)
        self.assertEqual(box.textFormat(), QtCore.Qt.TextFormat.PlainText)
        self.assertIn("同名素材", box.text())
        self.assertFalse(dialog.material_background_jobs)
        first.show_result()
        self.assertEqual(len(dialog._material_error_dialogs), 1)
        box.close()
        self.assertFalse(dialog._material_error_dialogs)
        first.show_result()
        self.assertTrue(dialog._material_error_dialogs[first.task_id].isVisible())
        self.assertEqual({item["name"] for item in store.list_materials()}, {"Unique", "Later"})

    def test_failed_name_is_available_for_resubmission(self):
        dialog, store = self.set_up_dialog()
        calls = []
        def operation(_store, payload, _progress):
            calls.append(payload["name"])
            if len(calls) == 1:
                raise ValueError("下载失败")
            return {"id": "test", "name": payload["name"]}
        with patch("model.InventoryBackground.material_operation", side_effect=operation), \
             patch.object(dialog, "refresh_materials"):
            self.draft(dialog, "Unique")
            dialog.add_material()
            self.wait(lambda: not self.manager.is_busy())
            self.draft(dialog, "Unique")
            dialog.add_material()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(calls, ["Unique", "Unique"])

    def test_appending_to_existing_name_is_not_rejected(self):
        dialog, store = self.set_up_dialog()
        material = store.add_material("Unique", [self.source("original.txt")])
        source = self.source("additional.txt")
        dialog.material_copy_operation = {"kind": "material_append", "name": "Unique"}
        with patch.object(store, "validate_material_name", side_effect=AssertionError("append is not add")):
            handle = dialog._launch_material_worker(MaterialCopyThread(
                store, "Unique", [source], dialog, material_id=material["id"]))
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(handle.task_id)["status"], "succeeded")
        self.assertEqual(len(store.list_materials()), 1)
        self.assertEqual({path.name for path in Path(material["path"]).iterdir()},
                         {"original.txt", "additional.txt"})


if __name__ == "__main__":
    unittest.main()
