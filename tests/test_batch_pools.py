import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.pools import (
    migrate_pools, merge_copies, add_videos, new_copy, pairing_plan, apply_pairs, bind_pool_layers)
from app_plugins.builtin.batch_text_video.components import resolve_sources, commit_sequences, new_text_component
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.task_text_ui import TaskTextImportDialog
from app_plugins.builtin.batch_text_video.task_texts import entry_from_record


class PoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def videos(self, root, count=3):
        paths = [str(Path(root)/f"{i+1}.mp4") for i in range(count)]
        for path in paths:
            Path(path).write_bytes(b"generated-fixture")
        return paths

    def copies(self, count=3):
        return [{**new_copy(), "id":f"copy{i}", "title":f"Title {i}", "body":f"Body {i}",
                 "name":f"Task {i}", "label":str(i)} for i in range(count)]

    def records(self, root):
        return [{"task_id":str(1000+i),"task_type":"reels","target_dir":str(Path(root)/str(i)),
                 "task_audio_text":f"Title {i}\nBody {i}", "task_name":"中文", "source_row":i+2,
                 "source_sheet":"Sheet1"} for i in range(4)]

    def test_old_jobs_migrate_once_without_losing_outputs_or_cursors(self):
        state = fresh_state()
        del state["copy_pool"], state["image_pool"]
        state["jobs"] = [{**self.copies(1)[0], "path":"old.mp4", "output":"done.mp4", "status":"已完成"}]
        before = copy.deepcopy(state["jobs"])
        state["music"] = [{"id":"m", "cursor":19}]
        migrate_pools(state)
        self.assertEqual(state["jobs"], before)
        self.assertEqual(state["copy_pool"][0]["title"],"Title 0")
        self.assertEqual(state["backgrounds"],["old.mp4"])
        state["copy_pool"].clear()
        state["backgrounds"].clear()
        migrate_pools(state)
        self.assertEqual(state["copy_pool"],[])
        self.assertEqual(state["backgrounds"],[])
        self.assertEqual(state["music"][0]["cursor"],19)

    def test_store_migrates_legacy_json_and_keeps_original_until_save(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            del state["copy_pool"],state["image_pool"],state["profiles"],state["active_profile"]
            state["jobs"] = [{**self.copies(1)[0],"path":"legacy.mp4"}]
            original = json.dumps(state)
            store.path.write_text(original,"utf8")
            self.assertEqual(len(store.load()["copy_pool"]),1)
            self.assertEqual(store.path.read_text("utf8"),original)

    def test_video_and_table_import_do_not_create_export_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            paths = self.videos(root)
            self.assertEqual(add_videos(state,paths*2),3)
            entries = [entry_from_record(record) for record in self.records(root)]
            self.assertEqual(merge_copies(state,entries,True),(4,0))
            self.assertEqual(merge_copies(state,entries,True),(0,0))
            self.assertEqual(state["jobs"],[])
            self.assertEqual(len(state["copy_pool"]),4)
            self.assertTrue(all(not entry["path"] for entry in state["copy_pool"]))

    def test_pairing_modes_and_manual_override_do_not_change_sources(self):
        copies, videos = self.copies(5),["1.mp4","2.mp4"]
        before = copy.deepcopy(copies)
        plan = pairing_plan(copies,videos,"copies")
        self.assertEqual([p["path"] for p in plan],videos*2+[videos[0]])
        self.assertEqual(len(pairing_plan(copies,videos,"videos")),2)
        self.assertEqual([p["path"] for p in pairing_plan(copies,videos,"single")],[videos[0]]*5)
        with self.assertRaisesRegex(ValueError,"数量相同"):
            pairing_plan(copies,videos,"one_to_one")
        plan[0]["path"] = "manual.mp4"
        self.assertEqual(copies,before)

    def test_reapplying_pairing_deduplicates_preserves_completed_and_updates_changed_copy(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            state["copy_pool"] = self.copies(2)
            paths = self.videos(root,2)
            plan = pairing_plan(state["copy_pool"],paths)
            self.assertEqual(apply_pairs(state,plan),(2,0))
            ids = [job["id"] for job in state["jobs"]]
            state["jobs"][0].update(status="已完成",output="done.mp4")
            self.assertEqual(apply_pairs(state,plan),(0,0))
            self.assertEqual(state["jobs"][0]["status"],"已完成")
            state["copy_pool"][0]["body"] = "Changed"
            self.assertEqual(apply_pairs(state,plan),(0,1))
            self.assertEqual(state["jobs"][0]["status"],"待生成")
            self.assertEqual([job["id"] for job in state["jobs"]],ids)

    def test_bad_pairing_is_atomic_and_variants_have_stable_readable_names(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            paths = self.videos(root,2)
            source = self.copies(1)
            plan = pairing_plan(source,paths,"videos")
            invalid = plan + [{"copy":source[0],"path":"missing.mp4"}]
            with self.assertRaisesRegex(ValueError,"不存在"):
                apply_pairs(state,invalid)
            self.assertEqual(state["jobs"],[])
            with self.assertRaisesRegex(ValueError,"重复"):
                apply_pairs(state,[plan[0],plan[0]])
            self.assertEqual(state["jobs"],[])
            apply_pairs(state,plan)
            self.assertEqual([job["name"] for job in state["jobs"]],["Task 0","Task 0_2"])
            apply_pairs(state,plan)
            self.assertEqual(len(state["jobs"]),2)

    def test_video_only_plan_supports_fixed_components(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            apply_pairs(state,pairing_plan([],self.videos(root,2),"videos"))
            self.assertEqual(len(state["jobs"]),2)
            self.assertTrue(all(not job["copy_id"] for job in state["jobs"]))

    def test_pool_rotation_reads_field_and_preserves_next_id_when_pool_reordered(self):
        state = fresh_state()
        state["copy_pool"] = self.copies()
        layer = new_text_component()
        layer.update(source="pool",pool_field="title")
        layers = bind_pool_layers([layer],state)
        resolved,plan = resolve_sources(layers)
        self.assertEqual(resolved[0]["text"],"Title 0")
        committed = commit_sequences(layers,plan)
        self.assertEqual(committed[0]["sequence_cursor"],1)
        state["copy_pool"].reverse()
        rebound = bind_pool_layers(committed,state)
        self.assertEqual(resolve_sources(rebound)[0][0]["text"],"Title 1")
        self.assertEqual(layers[0]["sequence_cursor"],0) # preview is read-only
        rebound[0]["pool_field"] = "full"
        self.assertEqual(resolve_sources(bind_pool_layers(rebound,state))[0][0]["text"],"Title 1\nBody 1")

    def test_fixed_content_is_not_overwritten_when_binding_pool(self):
        layer = new_text_component()
        layer["text"] = "Manual"
        state = fresh_state()
        state["copy_pool"] = self.copies()
        self.assertEqual(bind_pool_layers([layer],state),[layer])
        self.assertEqual(resolve_sources([layer],"Job","Body")[0][0]["text"],"Manual")

    def test_empty_pool_blocks_enabled_rotation_but_not_disabled_rotation(self):
        layer = new_text_component()
        layer["source"] = "pool"
        with self.assertRaises(ValueError):
            resolve_sources(bind_pool_layers([layer],fresh_state()))
        layer["enabled"] = False
        self.assertEqual(resolve_sources(bind_pool_layers([layer],fresh_state()))[1],[])

    def test_profiles_are_independent_and_resumable(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            state["copy_pool"] = self.copies(1)
            state["settings"]["darkness"] = 31
            state["music"] = [{"id":"m", "path":"song.mp3", "cursor":7}]
            state = store.save(state,0)
            first = state["active_profile"]
            second = store.create_profile(state,"另一位管理员",clone=False)
            self.assertEqual(second["copy_pool"],[])
            self.assertNotEqual(second["active_profile"],first)
            second["settings"]["darkness"] = 9
            second = store.save(second,second["revision"])
            restored = store.select_profile(second,first)
            self.assertEqual(restored["settings"]["darkness"],31)
            self.assertEqual(restored["music"][0]["cursor"],7)
            self.assertEqual(len(restored["copy_pool"]),1)
            restored["music"][0]["cursor"] = 12
            restored = store.save(restored,restored["revision"])
            clone = store.create_profile(restored,"副本",clone=True)
            clone["music"][0]["cursor"] = 22
            clone = store.save(clone,clone["revision"])
            self.assertEqual(store.select_profile(clone,first)["music"][0]["cursor"],12)
            with self.assertRaisesRegex(ValueError,"重复"):
                store.create_profile(clone,"副本",clone=True)

    def test_delete_video_multiple_selection_and_reopen_never_deletes_source_files(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(Path(root)/"store")
            dialog = BatchTextVideoDialog(store=store)
            paths = self.videos(root)
            dialog.add_files(paths)
            dialog.show()
            table = dialog.video_pool
            table.selectAll()
            QTest.keyClick(table,QtCore.Qt.Key.Key_Delete)
            self.assertEqual(dialog.state["backgrounds"],[])
            self.assertEqual(table.rowCount(),0)
            self.assertTrue(all(Path(path).is_file() for path in paths))
            self.assertEqual(Store(store.directory).load()["backgrounds"],[])
            dialog.close()

    def test_deleting_current_copy_clears_editor_and_does_not_remove_composed_queue(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(Path(root)/"store")
            state = fresh_state()
            state["copy_pool"] = self.copies(2)
            state["backgrounds"] = self.videos(root,1)
            apply_pairs(state,pairing_plan(state["copy_pool"],state["backgrounds"]))
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.copy_pool.setCurrentCell(0,0)
            dialog.copy_pool.selectRow(0)
            dialog.remove_resource("copy_pool")
            self.assertEqual(dialog.current_copy_id,"")
            self.assertEqual(dialog.title.toPlainText(),"")
            self.assertEqual(len(store.load()["jobs"]),2)
            self.assertEqual(len(store.load()["copy_pool"]),1)
            dialog.close()

    def test_deleting_preview_video_does_not_blank_selected_copy(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(Path(root)/"store")
            state = fresh_state()
            state["copy_pool"] = self.copies(1)
            state["backgrounds"] = self.videos(root,1)
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.copy_pool.setCurrentCell(0,0)
            dialog.video_pool.setCurrentCell(0,0)
            dialog.remove_resource("backgrounds")
            self.assertEqual(store.load()["copy_pool"][0]["title"],"Title 0")
            self.assertEqual(dialog.title.toPlainText(),"Title 0")
            dialog.close()

    def test_delete_queue_clears_editor_without_removing_library_assets(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(Path(root)/"store")
            state = fresh_state()
            state["copy_pool"] = self.copies(1)
            state["backgrounds"] = self.videos(root,1)
            apply_pairs(state,pairing_plan(state["copy_pool"],state["backgrounds"]))
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.jobs.selectAll()
            QTest.keyClick(dialog.jobs,QtCore.Qt.Key.Key_Delete)
            self.assertEqual(dialog.title.toPlainText(),"")
            self.assertEqual(dialog.current_id,"")
            self.assertEqual(len(store.load()["backgrounds"]),1)
            self.assertEqual(len(store.load()["copy_pool"]),1)
            dialog.close()

    def test_removing_pool_video_while_queue_editor_active_never_blanks_queue_text(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(Path(root)/"store")
            state = fresh_state()
            state["copy_pool"] = self.copies(1)
            state["backgrounds"] = self.videos(root,1)
            apply_pairs(state,pairing_plan(state["copy_pool"],state["backgrounds"]))
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.preview_video = state["backgrounds"][0]
            dialog.video_pool.selectAll()
            dialog.remove_resource("backgrounds")
            self.assertEqual(dialog.title.toPlainText(),"Title 0")
            self.assertEqual(store.load()["jobs"][0]["title"],"Title 0")
            self.assertEqual(store.load()["jobs"][0]["body"],"Body 0")
            self.assertTrue(Path(state["backgrounds"][0]).exists())
            dialog.close()

    def test_busy_work_blocks_resource_delete_and_profile_switch(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog.add_files(self.videos(root,1))
            dialog.video_pool.selectAll()
            before = copy.deepcopy(dialog.state)
            dialog.worker = Mock()
            dialog.remove_resource("backgrounds")
            self.assertEqual(dialog.state,before)
            dialog.worker = None
            dialog.close()

    def test_multiselect_table_import_has_bulk_checks_and_import_highlighted(self):
        with tempfile.TemporaryDirectory() as root:
            dialog = TaskTextImportDialog(records=self.records(root))
            self.assertEqual(dialog.list.selectionMode(),dialog.list.SelectionMode.ExtendedSelection)
            dialog.check_all(False)
            dialog.list.clearSelection()
            selection = dialog.list.selectionModel()
            for row in (1,3):
                selection.select(dialog.list.model().index(row,0),QtCore.QItemSelectionModel.SelectionFlag.Select |
                                  QtCore.QItemSelectionModel.SelectionFlag.Rows)
            dialog.set_selected_checks(True)
            self.assertEqual([dialog.list.item(row,0).checkState() == QtCore.Qt.CheckState.Checked for row in range(4)],
                             [False,True,False,True])
            dialog.import_highlighted()
            self.assertEqual([entry["task_id"] for entry in dialog.entries],["1001","1003"])

    def test_pairing_dialog_scope_and_row_override(self):
        copies, paths = self.copies(3),["1.mp4","2.mp4"]
        dialog = PairingDialog(copies,paths,[copies[1]["id"]],[paths[0]])
        self.assertEqual(len(dialog.pairs),3)
        dialog.only_copies.setChecked(True)
        self.assertEqual(len(dialog.pairs),1)
        self.assertEqual(dialog.pairs[0]["copy"]["id"],copies[1]["id"])
        dialog.table.cellWidget(0,1).setCurrentIndex(1)
        self.assertEqual(dialog.pairs[0]["path"],paths[1])
        dialog.only_videos.setChecked(True)
        self.assertEqual(dialog.pairs[0]["path"],paths[0])

    def test_menu_structure_and_profile_switch_updates_controls_without_stale_editor(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(root)
            state = fresh_state()
            state["copy_pool"] = self.copies(1)
            state["settings"]["darkness"] = 31
            state = store.save(state,0)
            original = state["active_profile"]
            state = store.create_profile(state,"祷告词",False)
            state["settings"]["darkness"] = 9
            store.save(state,state["revision"])
            dialog = BatchTextVideoDialog(store=store)
            self.assertEqual([a.text() for a in dialog.menu_bar.actions()],["编辑","资源","批量","视图","配置"])
            self.assertEqual([dialog.resources.tabText(i) for i in range(4)],["视频","文案","图片","音乐"])
            dialog.switch_profile(original)
            self.assertEqual(dialog.controls["darkness"].value(),31)
            self.assertEqual(dialog.copy_pool.rowCount(),1)
            self.assertEqual(dialog.title.toPlainText(),"")
            dialog.copy_pool.setCurrentCell(0,0)
            dialog.title.setPlainText("Edited original")
            dialog.persist()
            new_id = next(p["id"] for p in dialog.state["profiles"] if p["name"] == "祷告词")
            dialog.switch_profile(new_id)
            self.assertEqual(dialog.controls["darkness"].value(),9)
            self.assertEqual(dialog.copy_pool.rowCount(),0)
            self.assertEqual(store.load()["copy_pool"],[])
            dialog.switch_profile(original)
            self.assertEqual(dialog.state["copy_pool"][0]["title"],"Edited original")
            dialog.close()

    def test_adding_rotating_text_reads_pool_and_fixed_frame_can_choose_copy(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(root)
            state = fresh_state()
            state["copy_pool"] = self.copies(2)
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.layer_panel.add_rotating_text()
            layer = dialog.layer_panel.current()
            self.assertEqual(layer["source"],"pool")
            self.assertEqual(len(layer["sequence_items"]),2)
            self.assertEqual(resolve_sources([layer])[0][0]["text"],"Title 0\nBody 0")
            self.assertTrue(dialog.persist())
            with patch.object(QtWidgets.QInputDialog,"getItem",side_effect=[("2. 1 — Title 1",True),("标题",True)]):
                dialog.pick_layer_copy(layer["id"])
            self.assertEqual(dialog.layer_panel.current()["text"],"Title 1")
            self.assertEqual(dialog.layer_panel.current()["source"],"fixed")
            dialog.close()

    def test_music_and_image_remove_are_safe_and_reset_missing_next_id(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(root)
            state = fresh_state()
            image = Path(root)/"asset.png"
            image.write_bytes(b"fixture")
            state["image_pool"] = [{"id":"image","path":str(image),"name":"Image"}]
            state["music"] = [{"id":"m", "path":"song.mp3", "cursor":8}]
            state["next_music_id"] = "m"
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.music.selectAll()
            QTest.keyClick(dialog.music,QtCore.Qt.Key.Key_Delete)
            self.assertEqual(dialog.state["music"],[])
            self.assertEqual(dialog.state["next_music_id"],"")
            dialog.image_pool.selectAll()
            QTest.keyClick(dialog.image_pool,QtCore.Qt.Key.Key_Delete)
            self.assertEqual(dialog.state["image_pool"],[])
            self.assertTrue(image.exists())
            dialog.close()

    def test_multiselect_reorder_keeps_selection_attached_to_same_music_ids(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(root)
            state = fresh_state()
            state["music"] = [{"id":str(i),"path":f"song{i}.mp3","cursor":i+1} for i in range(5)]
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            select = dialog.music.selectionModel()
            for row in (1,3):
                select.select(dialog.music.model().index(row,0),QtCore.QItemSelectionModel.SelectionFlag.Select |
                              QtCore.QItemSelectionModel.SelectionFlag.Rows)
            dialog.move_resource("music",-1)
            self.assertEqual([item["id"] for item in dialog.state["music"]],["1","0","3","2","4"])
            self.assertEqual(set(dialog.music.identifiers()),{"1","3"})
            self.assertEqual([item["cursor"] for item in dialog.state["music"]],[2,1,4,3,5])
            dialog.remove_resource("music")
            self.assertEqual([item["id"] for item in dialog.state["music"]],["0","2","4"])
            dialog.close()

    def test_copy_preview_uses_chosen_background_without_implicit_queue_entry(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(Path(root)/"store"))
            paths = self.videos(root,2)
            dialog.add_files(paths)
            dialog.video_pool.setCurrentCell(1,0)
            dialog.add_manual_copy()
            dialog.title.setPlainText("Preview")
            dialog.persist()
            self.assertEqual(dialog.active_entry()["path"],paths[1])
            self.assertEqual(dialog.state["jobs"],[])
            dialog.close()

    def test_failed_pool_export_only_successful_job_advances_rotation(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            state["copy_pool"] = self.copies(2)
            apply_pairs(state,pairing_plan(state["copy_pool"],self.videos(root,2)))
            layer = new_text_component()
            layer.update(source="pool",pool_field="body")
            state["settings"]["layers"] += bind_pool_layers([layer],state)
            state = store.save(state,0)
            output = Path(root)/"output.mp4"
            output.write_bytes(b"fixture")
            worker = BatchWorker(store,state,[job["id"] for job in state["jobs"]])
            renderer = Mock(side_effect=[ValueError("fixture failure"),(output,None)])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS",{"static_text":renderer}):
                worker.run()
            saved = store.load()
            rotating = next(layer for layer in saved["settings"]["layers"] if layer["source"] == "pool")
            self.assertEqual(rotating["sequence_cursor"],1)
            self.assertEqual([job["status"] for job in saved["jobs"]],["需处理","已完成"])
            self.assertEqual(next(layer for layer in saved["profiles"][0]["data"]["settings"]["layers"]
                                  if layer["source"] == "pool")["sequence_cursor"],1)

    def test_switch_profile_cancels_uncommitted_drag_in_old_profile_not_new_profile(self):
        from app_plugins.builtin.batch_text_video.layers import componentize_layers
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(root)
            state = store.save(fresh_state(),0)
            first = state["active_profile"]
            state = store.create_profile(state,"另一种排版",False)
            second = state["active_profile"]
            state["settings"]["layers"] = componentize_layers(None,state["settings"])
            next(layer for layer in state["settings"]["layers"] if layer["id"] == "body")["x"] = 25
            state = store.save(state,state["revision"])
            store.select_profile(state,first)
            dialog = BatchTextVideoDialog(store=store)
            original = next(layer for layer in dialog.layer_panel.values() if layer["id"] == "body")
            geometry = {key:original[key] for key in ("x","y","width","height")}
            dialog.layer_panel.set_geometry("body",{**geometry,"x":70})
            dialog.preview._drag = {"id":"body","values":geometry,"changed":True}
            dialog.switch_profile(second)
            self.assertEqual(next(layer for layer in dialog.layer_panel.values() if layer["id"] == "body")["x"],25)
            self.assertEqual(next(layer for layer in dialog.state["settings"]["layers"] if layer["id"] == "body")["x"],25)
            dialog.switch_profile(first)
            self.assertEqual(next(layer for layer in dialog.layer_panel.values() if layer["id"] == "body")["x"],geometry["x"])
            dialog.close()


if __name__ == "__main__":
    unittest.main()
