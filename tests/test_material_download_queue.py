"""Multiple material jobs must queue without locking or clearing the next draft."""
import threading
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from model.InventoryManager import InventoryStore
from model.InventoryBackground import inventory_resources
from PYUI.utility_managers_pyui import InventoryManagerDialog, MaterialCopyThread
from tests import test_background_plugin_jobs as support


class MaterialDownloadQueueTests(unittest.TestCase):
    setUpClass = classmethod(support.BackgroundPluginJobsTests.setUpClass.__func__)
    setUp = support.BackgroundPluginJobsTests.setUp
    tearDown = support.BackgroundPluginJobsTests.tearDown
    wait = support.BackgroundPluginJobsTests.wait
    submit_hold = support.BackgroundPluginJobsTests.submit_hold

    def dialog(self):
        store = InventoryStore(self.root / "inventory.json", self.root / "library")
        dialog = InventoryManagerDialog(store, context=self.context)
        self.addCleanup(dialog.deleteLater)
        return dialog, store

    def submit(self, dialog, name, source):
        dialog.material_name_edit.setText(name)
        dialog.material_sources_edit.clear()
        dialog.material_sources_edit.add_paths([str(source)])
        dialog.add_material()
        return dialog.material_job_handles()[-1]

    def test_multiple_downloads_queue_and_form_remains_usable(self):
        dialog, store = self.dialog()
        hold, release = self.submit_hold(inventory_resources(store))
        self.addCleanup(release.set)
        self.wait(lambda: self.manager.get(hold.task_id)["status"] == "running")
        copied = []
        original = store.add_material
        def operation(job_store, payload, progress):
            copied.append((payload["name"], threading.get_ident()))
            return original(payload["name"], [self.source], progress_callback=progress)
        self.source = self.root / "本地测试.png"
        self.source.write_bytes(b"test")
        with patch("model.InventoryBackground.material_operation", side_effect=operation):
            first = self.submit(dialog, "甲素材", "https://drive.google.com/drive/folders/first")
            second = self.submit(dialog, "乙素材", "https://drive.google.com/drive/folders/second")
            self.assertEqual(len(dialog.material_background_jobs), 2)
            self.assertTrue(all(self.manager.get(h.task_id)["status"] == "queued" for h in (first, second)))
            for widget in (dialog.material_name_edit, dialog.material_sources_edit, dialog.material_submit_btn,
                           dialog.material_add_files_btn, dialog.material_paste_link_btn, dialog.material_table,
                           dialog.material_append_btn, dialog.person_add_btn):
                self.assertTrue(widget.isEnabled())
            self.assertFalse(dialog.material_remove_btn.isEnabled())
            self.assertFalse(dialog.person_remove_btn.isEnabled())
            dialog.material_name_edit.setText("尚未提交的第三项")
            dialog.material_sources_edit.add_paths([str(self.source)])
            heartbeats = []
            QtCore.QTimer.singleShot(0, lambda: heartbeats.append(True))
            self.wait(lambda: bool(heartbeats))
            dialog.close()  # Hide the window while all jobs remain in the host queue.
            release.set()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual([name for name, _ in copied], ["甲素材", "乙素材"])
        self.assertTrue(all(thread != threading.get_ident() for _, thread in copied))
        self.assertEqual({item["name"] for item in store.list_materials()}, {"甲素材", "乙素材"})
        self.assertEqual(dialog.material_name_edit.text(), "尚未提交的第三项")
        self.assertEqual(dialog.material_sources_edit.paths(), [str(self.source)])
        self.assertEqual(dialog.material_background_jobs, {})
        self.assertTrue(dialog.material_remove_btn.isEnabled())

    def test_resources_serialize_one_library_without_blocking_unrelated_job(self):
        dialog, store = self.dialog()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        order = []
        def operation(_store, payload, progress):
            order.append(payload["name"])
            if payload["name"] == "first":
                started.set()
                release.wait(3)
            progress("完成", 100)
            return {"id": payload["name"], "name": payload["name"]}
        with patch("model.InventoryBackground.material_operation", side_effect=operation), \
             patch.object(dialog, "refresh_materials"):
            first = self.submit(dialog, "first", "https://drive.google.com/drive/folders/first")
            second = self.submit(dialog, "second", "https://drive.google.com/drive/folders/second")
            unrelated = self.context.submit_background_task(title="不相关任务", runner=lambda _p, _c: {"paths": []}, kind="cpu")
            self.wait(lambda: started.is_set() and self.manager.get(unrelated.task_id)["status"] == "succeeded")
            self.assertEqual(self.manager.get(first.task_id)["status"], "running")
            self.assertEqual(self.manager.get(second.task_id)["status"], "queued")
            self.assertEqual(order, ["first"])
            release.set()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(order, ["first", "second"])

    def test_queued_cancel_and_failure_do_not_stop_later_jobs_or_show_modal_popup(self):
        dialog, store = self.dialog()
        hold, release = self.submit_hold(inventory_resources(store))
        self.addCleanup(release.set)
        seen = []
        def operation(_store, payload, _progress):
            seen.append(payload["name"])
            if payload["name"] == "bad": raise ValueError("测试下载失败")
            return {"id": payload["name"], "name": payload["name"]}
        with patch("model.InventoryBackground.material_operation", side_effect=operation), \
             patch.object(dialog, "refresh_materials"), \
             patch.object(QtWidgets.QMessageBox, "warning", side_effect=AssertionError("no blocking popup")):
            canceled = self.submit(dialog, "cancel", "https://drive.google.com/drive/folders/cancel")
            bad = self.submit(dialog, "bad", "https://drive.google.com/drive/folders/bad")
            good = self.submit(dialog, "good", "https://drive.google.com/drive/folders/good")
            canceled.requestInterruption()
            self.assertNotIn(canceled.task_id, dialog.material_background_jobs)
            release.set()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(seen, ["bad", "good"])
        self.assertEqual(self.manager.get(bad.task_id)["status"], "failed")
        self.assertTrue(bad.has_result_handler())
        self.assertEqual(set(dialog._material_error_dialogs), {bad.task_id})
        box = dialog._material_error_dialogs[bad.task_id]
        self.assertTrue(box.isVisible())
        self.assertFalse(box.isModal())
        self.assertEqual(box.thread(), self.app.thread())
        self.assertIn("测试下载失败", box.text())
        box.close()
        self.assertEqual(self.manager.get(good.task_id)["status"], "succeeded")
        self.assertEqual(dialog.material_background_jobs, {})

    def test_completion_uses_its_own_monitor_and_person_metadata(self):
        dialog, store = self.dialog()
        hold, release = self.submit_hold(inventory_resources(store))
        self.addCleanup(release.set)
        monitors, logs = [], []
        dialog.material_monitor_requested.connect(monitors.append)
        dialog.log.connect(logs.append)
        dialog.material_copy_operation = {"kind": "material_add", "name": "material", "monitor_folder_url": "https://drive.google.com/drive/folders/specific"}
        first = dialog._launch_material_worker(MaterialCopyThread(store, "material", [], dialog))
        dialog.material_copy_operation = {"kind": "person_add", "name": "person"}
        second = dialog._launch_material_worker(MaterialCopyThread(store, "person", [], dialog, operation="person_add", profile={"name": "person"}))
        def operation(_store, payload, _progress):
            return {"id": payload["name"], "name": payload["name"], "path": str(self.root / payload["name"])}
        with patch("model.InventoryBackground.material_operation", side_effect=operation), \
             patch.object(dialog, "refresh_materials"), patch.object(dialog, "refresh_people"):
            release.set()
            self.wait(lambda: not self.manager.is_busy())
        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0]["material_id"], "material")
        self.assertEqual(monitors[0]["folder_url"], "https://drive.google.com/drive/folders/specific")
        self.assertTrue(any("已添加人物“person”" in line for line in logs))

    def test_plugin_shutdown_cancels_every_queued_material_job(self):
        dialog, store = self.dialog()
        hold, release = self.submit_hold(inventory_resources(store))
        self.addCleanup(release.set)
        first = self.submit(dialog, "first", "https://drive.google.com/drive/folders/first")
        second = self.submit(dialog, "second", "https://drive.google.com/drive/folders/second")
        from app_plugins.builtin.inventory import InventoryPlugin
        plugin = InventoryPlugin.__new__(InventoryPlugin)
        plugin.dialog, plugin.material_sync_thread = dialog, None
        self.assertFalse(plugin.stop())
        self.assertEqual(self.manager.get(first.task_id)["status"], "canceled")
        self.assertEqual(self.manager.get(second.task_id)["status"], "canceled")
        release.set()
        self.wait(lambda: not self.manager.is_busy())


if __name__ == "__main__": unittest.main()
