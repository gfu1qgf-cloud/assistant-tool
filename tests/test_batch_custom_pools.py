import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_paths import APP_ROOT
from app_plugins.builtin.batch_text_video.custom_pools import (
    POOL_TYPES, create_pool, get_pool, add_pool_paths, normalize_pools, music_source, assign_voices)
from app_plugins.builtin.batch_text_video.custom_pool_ui import NewPoolDialog, ImagePoolBindingDialog
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.pools import pairing_plan, apply_pairs, bind_pool_layers, new_copy
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker
from app_plugins.builtin.batch_text_video.engine import choose_music, render_static_text, probe, resolve_tools
from app_plugins.builtin.batch_text_video.playback import preview_key


class CustomPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def fixture(self, root, name):
        path = Path(root) / name
        path.write_bytes(b"test-fixture")
        return str(path)

    def state_with_voice(self, root):
        state = fresh_state()
        pool = create_pool(state, "语音池", "voice")
        add_pool_paths(pool, [self.fixture(root, "task_audio.wav"), self.fixture(root, "voice2.mp3")])
        return state, pool

    def test_validation_and_explicit_roles(self):
        state = fresh_state()
        for kind in POOL_TYPES:
            create_pool(state, kind, kind)
        self.assertEqual(len(normalize_pools(state)), len(POOL_TYPES))
        for name, kind in (("", "voice"), ("image", "image"), ("bad", "unknown")):
            with self.assertRaises(ValueError):
                create_pool(state, name, kind)
        broken = copy.deepcopy(state)
        broken["resource_pools"][0]["kind"] = "unknown"
        with self.assertRaises(ValueError):
            normalize_pools(broken)

    def test_voice_import_is_independent_of_music_copies_and_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            state, pool = self.state_with_voice(root)
            added = add_pool_paths(pool, [pool["items"][0]["path"], self.fixture(root, "wrong.mp4"), "missing.wav"])
            self.assertEqual(added, 0)
            self.assertEqual(len(pool["items"]), 2)
            self.assertEqual((state["music"], state["copy_pool"], state["jobs"]), ([], [], []))
            self.assertFalse(any("cursor" in item for item in pool["items"]))

    def test_voice_pairing_cycles_keeps_task_directories_and_can_explicitly_disable(self):
        with tempfile.TemporaryDirectory() as root:
            state, pool = self.state_with_voice(root)
            original_voice = self.fixture(root, "original.wav")
            copies = [{**new_copy(), "task_dir": str(Path(root) / str(i)), "voice_path": original_voice} for i in range(3)]
            video = self.fixture(root, "video.mp4")
            before = copy.deepcopy(copies)
            plan = pairing_plan(copies, [video])
            self.assertTrue(all("voice_path" not in pair for pair in assign_voices(plan, [], "task")))
            rotated = assign_voices(plan, pool["items"], "rotate")
            self.assertEqual([pair["voice_path"] for pair in rotated], [pool["items"][0]["path"], pool["items"][1]["path"], pool["items"][0]["path"]])
            self.assertEqual(apply_pairs(state, rotated), (3, 0))
            self.assertEqual([job["task_dir"] for job in state["jobs"]], [item["task_dir"] for item in copies])
            self.assertEqual(copies, before)
            self.assertEqual(apply_pairs(state, rotated), (0, 0))
            disabled = assign_voices(plan, [], "none")
            apply_pairs(state, disabled)
            self.assertTrue(all(job["voice_override"] and not job["voice_path"] for job in state["jobs"]))
            with self.assertRaisesRegex(ValueError, "为空"):
                assign_voices(plan, [], "rotate")

    def test_missing_voice_is_atomic_and_does_not_create_partial_queue(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            video = self.fixture(root, "video.mp4")
            pairs = assign_voices(pairing_plan([new_copy()], [video]), [{"path": "missing.wav"}], "rotate")
            before = copy.deepcopy(state)
            with self.assertRaisesRegex(ValueError, "人声文件不存在"):
                apply_pairs(state, pairs)
            self.assertEqual(state, before)

    def test_legacy_completed_job_is_not_invalidated_by_new_optional_field(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            plan = pairing_plan([new_copy()], [self.fixture(root, "video.mp4")])
            apply_pairs(state, plan)
            state["jobs"][0].update(status="已完成", output="done.mp4")
            state["jobs"][0].pop("voice_override", None)
            self.assertEqual(apply_pairs(state, plan), (0, 0))
            self.assertEqual(state["jobs"][0]["status"], "已完成")

    def test_profile_pools_are_independent_including_legacy_profile_without_field(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / "store")
            state, pool = self.state_with_voice(root)
            state = store.save(state, 0)
            first = state["active_profile"]
            second = store.create_profile(state, "其他配置", clone=False)
            self.assertEqual(second["resource_pools"], [])
            restored = store.select_profile(second, first)
            self.assertEqual(restored["resource_pools"][0]["items"], pool["items"])
            clone = store.create_profile(restored, "克隆", clone=True)
            clone["resource_pools"][0]["items"].clear()
            clone = store.save(clone, clone["revision"])
            clone = store.select_profile(clone, first)
            self.assertEqual(len(clone["resource_pools"][0]["items"]), 2)
            legacy = next(p for p in clone["profiles"] if p["name"] == "其他配置")
            legacy["data"].pop("resource_pools", None)
            clone = store.save(clone, clone["revision"])
            self.assertEqual(store.select_profile(clone, legacy["id"])["resource_pools"], [])

    def test_loading_legacy_file_does_not_write_or_invent_voice_pool(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            del state["resource_pools"]
            state["jobs"] = [{"id": "old", "voice_path": "task_audio.wav", "path": "video.mp4"}]
            text = json.dumps(state)
            store.path.write_text(text, encoding="utf8")
            self.assertEqual(store.load()["resource_pools"], [])
            self.assertEqual(store.path.read_text("utf8"), text)

    def test_music_pool_routing_and_completion_preserve_other_music_cursors(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / "store")
            state = fresh_state()
            state["music"] = [{"id": "default", "path": "default.wav", "cursor": 17}]
            pool = create_pool(state, "舒缓音乐", "music")
            add_pool_paths(pool, [self.fixture(root, "one.wav"), self.fixture(root, "two.wav")])
            state["settings"]["music_pool_id"] = pool["id"]
            state["jobs"] = [{"id": "job"}]
            state = store.save(state, 0)
            before = copy.deepcopy(state)
            with patch("app_plugins.builtin.batch_text_video.engine.clean_music", return_value=("clean.flac", 10, "key")):
                plan = choose_music(state, 3, root, "ffmpeg", "ffprobe", None, lambda text: None)
            self.assertEqual(plan["pool_id"], pool["id"])
            self.assertEqual(state, before)  # preparing / previewing never consumes
            output = self.fixture(root, "done.mp4")
            saved = store.completed(state, "job", output, plan)
            used = get_pool(saved, pool["id"], "music")
            self.assertEqual(used["items"][0]["cursor"], 3)
            self.assertEqual(used["next_id"], used["items"][1]["id"])
            self.assertEqual(saved["music"][0]["cursor"], 17)
            self.assertEqual(saved["next_music_id"], "")

    def test_invalid_music_binding_is_not_silently_replaced(self):
        state = fresh_state()
        state["settings"]["music_pool_id"] = "gone"
        with self.assertRaises(ValueError):
            music_source(state)
        pool = create_pool(state, "语音", "voice")
        state["settings"]["music_pool_id"] = pool["id"]
        with self.assertRaises(ValueError):
            music_source(state)

    def test_image_layer_binds_to_its_own_named_pool(self):
        state = fresh_state()
        state["image_pool"] = [{"id": "default", "path": "default.png"}]
        pool = create_pool(state, "水印", "image")
        pool["items"] = [{"id": "a", "path": "a.png"}, {"id": "b", "path": "b.png"}]
        layer = {"kind": "image", "source": "pool", "pool_id": pool["id"], "sequence_items": [], "sequence_cursor": 0}
        bound = bind_pool_layers([layer], state)[0]
        self.assertEqual([i["id"] for i in bound["sequence_items"]], ["a", "b"])
        bound["sequence_cursor"] = 1
        pool["items"].reverse()
        self.assertEqual(bind_pool_layers([bound], state)[0]["sequence_cursor"], 0)

    def test_wizard_types_and_menu_are_user_accessible(self):
        wizard = NewPoolDialog()
        self.assertEqual([wizard.kind.itemData(i) for i in range(wizard.kind.count())], ["image", "video", "visual"])
        wizard.category.setCurrentIndex(1)
        self.assertEqual([wizard.kind.itemData(i) for i in range(wizard.kind.count())], ["music", "voice"])
        wizard.kind.setCurrentIndex(1)
        self.assertIn("从头", wizard.note.text())
        wizard.category.setCurrentIndex(2)
        self.assertEqual([wizard.kind.itemData(i) for i in range(wizard.kind.count())], ["text", "subtitle"])
        wizard.deleteLater()
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            menu = next(a.menu() for a in dialog.menu_bar.actions() if a.text() == "资源")
            self.assertIn("新建资源池…", [action.text() for action in menu.actions()])
            dialog.close()
            dialog.deleteLater()

    def test_voice_drop_removal_and_reorder_do_not_route_to_task_import_or_delete_file(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            store = Store(Path(root) / "store")
            state, pool = self.state_with_voice(root)
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.tabs.setCurrentWidget(dialog.resources)
            dialog.resources.setCurrentWidget(dialog.custom_pages[pool["id"]])
            self.assertEqual(dialog.active_resource(), "pool:" + pool["id"])
            table = dialog.custom_tables[pool["id"]]
            third = self.fixture(root, "third.wav")
            mime = QtCore.QMimeData()
            mime.setUrls([QtCore.QUrl.fromLocalFile(third)])
            drop = QtGui.QDropEvent(QtCore.QPointF(2, 2), QtCore.Qt.DropAction.CopyAction, mime,
                                   QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier)
            table.dropEvent(drop)
            self.assertEqual(table.rowCount(), 3)
            self.assertEqual((dialog.state["music"], dialog.state["copy_pool"], dialog.state["jobs"]), ([], [], []))
            table.selectRow(2)
            dialog.move_resource("pool:" + pool["id"], -1)
            self.assertEqual(get_pool(dialog.state, pool["id"])["items"][1]["path"], third)
            QTest.keyClick(table, QtCore.Qt.Key.Key_Delete)
            self.assertEqual(table.rowCount(), 2)
            self.assertTrue(Path(third).is_file())
            self.assertEqual(len(get_pool(store.load(), pool["id"])["items"]), 2)
            dialog.close()
            dialog.deleteLater()

    def test_pairing_dialog_custom_video_voice_cycle_manual_override(self):
        with tempfile.TemporaryDirectory() as root:
            state, voices = self.state_with_voice(root)
            videos = create_pool(state, "背景", "video")
            add_pool_paths(videos, [self.fixture(root, "bg.mp4")])
            copies = [new_copy(), new_copy(), new_copy()]
            dialog = PairingDialog(copies, [], state=state, selected_pool=videos["id"], selected_voice_pool=voices["id"])
            self.assertEqual(len(dialog.pairs), 3)
            dialog.voice_mode.setCurrentIndex(dialog.voice_mode.findData("rotate"))
            self.assertEqual(dialog.pairs[1]["voice_path"], voices["items"][1]["path"])
            dialog.table.cellWidget(0, 4).setCurrentIndex(1)
            self.assertTrue(dialog.pairs[0]["voice_override"])
            self.assertEqual(dialog.pairs[0]["voice_path"], "")
            dialog.deleteLater()

    def test_named_video_pool_can_preview_without_creating_queue(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background") as request:
            store = Store(Path(root) / "store")
            state = fresh_state()
            pool = create_pool(state, "风景背景", "video")
            path = self.fixture(root, "landscape.mp4")
            add_pool_paths(pool, [path])
            state["copy_pool"] = [new_copy()]
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.copy_pool.setCurrentCell(0, 0)
            dialog.custom_tables[pool["id"]].setCurrentCell(0, 0)
            self.assertEqual(dialog.active_entry()["path"], path)
            request.assert_called_with(path)
            self.assertEqual(dialog.state["jobs"], [])
            dialog.close()
            dialog.deleteLater()

    def test_existing_image_control_can_bind_named_pool_then_choose_fixed_image(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            store = Store(Path(root) / "store")
            state = fresh_state()
            images = create_pool(state, "水印", "image")
            audio = create_pool(state, "语音", "voice")
            for name in ("one.png", "two.png"):
                image = QtGui.QImage(24, 24, QtGui.QImage.Format.Format_ARGB32)
                image.fill(QtGui.QColor("red"))
                image.save(str(Path(root) / name))
            add_pool_paths(images, [str(Path(root) / "one.png"), str(Path(root) / "two.png")])
            state["settings"]["layers"] += [{"id": "layer", "kind": "image", "path": images["items"][0]["path"]}]
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.layer_panel.select_layer_id("layer")
            picker = ImagePoolBindingDialog(dialog.state, dialog.layer_panel.current(), dialog)
            self.assertEqual(picker.pool.count(), 2)  # default + images, not voice
            picker.pool.setCurrentIndex(picker.pool.findData(images["id"]))
            picker.mode.setCurrentIndex(0)
            with patch("app_plugins.builtin.batch_text_video.custom_pool_ui.ImagePoolBindingDialog", return_value=picker), \
                 patch.object(picker, "exec", return_value=QtWidgets.QDialog.DialogCode.Accepted):
                dialog.pick_image_pool("layer")
            current = dialog.layer_panel.current()
            self.assertEqual((current["source"], current["pool_id"]), ("pool", images["id"]))
            self.assertEqual(len(current["sequence_items"]), 2)
            self.assertTrue(dialog.layer_panel.content_editor.choose_image_pool.isEnabled())
            picker = ImagePoolBindingDialog(dialog.state, current, dialog)
            picker.mode.setCurrentIndex(1)
            picker.image.setCurrentIndex(1)
            with patch("app_plugins.builtin.batch_text_video.custom_pool_ui.ImagePoolBindingDialog", return_value=picker), \
                 patch.object(picker, "exec", return_value=QtWidgets.QDialog.DialogCode.Accepted):
                dialog.pick_image_pool("layer")
            current = dialog.layer_panel.current()
            self.assertEqual(current["source"], "fixed")
            self.assertEqual(current["path"], images["items"][1]["path"])
            self.assertNotIn("pool_id", current)
            self.assertEqual(len(get_pool(store.load(), images["id"])["items"]), 2)
            dialog.close()
            dialog.deleteLater()

    def test_empty_image_pool_can_bind_rotation_but_not_fixed_image(self):
        picker = ImagePoolBindingDialog(fresh_state(), {"kind": "image", "source": "fixed"})
        ok = picker.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(ok.isEnabled())
        picker.mode.setCurrentIndex(0)
        self.assertTrue(ok.isEnabled())
        picker.deleteLater()

    def test_custom_image_import_worker_targets_only_its_named_pool(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            store = Store(Path(root) / "store")
            state = fresh_state()
            pool = create_pool(state, "小元素", "image")
            store.save(state, 0)
            path = str(Path(root) / "source.png")
            image = QtGui.QImage(32, 40, QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor("blue"))
            self.assertTrue(image.save(path))
            dialog = BatchTextVideoDialog(store=store)
            self.assertTrue(dialog.add_to_custom_pool(pool["id"], [path]))
            for _ in range(100):
                QTest.qWait(20)
                if dialog.image_import_worker is None:
                    break
            self.assertIsNone(dialog.image_import_worker)
            saved = store.load()
            imported = get_pool(saved, pool["id"])["items"]
            self.assertEqual(len(imported), 1)
            self.assertEqual(saved["image_pool"], [])
            self.assertTrue(Path(imported[0]["path"]).is_file())
            self.assertTrue(Path(imported[0]["thumbnail"]).is_file())
            self.assertTrue(Path(path).is_file())
            dialog.custom_tables[pool["id"]].selectAll()
            dialog.use_pool_images(True, "pool:" + pool["id"])
            self.assertEqual(dialog.layer_panel.current()["pool_id"], pool["id"])
            self.assertTrue(dialog.persist())
            dialog.close()
            dialog.deleteLater()

    def test_assign_voice_to_selected_queue_enables_narration_without_changing_music(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            store = Store(Path(root) / "store")
            state, voices = self.state_with_voice(root)
            copies = [new_copy(), new_copy()]
            apply_pairs(state, pairing_plan(copies, [self.fixture(root, "video.mp4")]))
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.custom_tables[voices["id"]].selectAll()
            dialog.jobs.selectAll()
            dialog.use_custom_voices(voices["id"])
            self.assertEqual([j["voice_path"] for j in store.load()["jobs"]], [i["path"] for i in voices["items"]])
            self.assertTrue(store.load()["settings"]["use_voice"])
            self.assertEqual(store.load()["music"], [])
            dialog.close()
            dialog.deleteLater()

    def test_delete_pool_freezes_image_layer_preserves_source_and_queue(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            store = Store(Path(root) / "store")
            state = fresh_state()
            pool = create_pool(state, "水印", "image")
            image_path = str(Path(root) / "image.png")
            image = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor("red"))
            self.assertTrue(image.save(image_path))
            pool["items"] = [{"id": "image", "path": image_path, "kind": "image", "name": "水印"}]
            state["settings"]["layers"] += [{"id": "layer", "kind": "image", "source": "pool", "pool_id": pool["id"],
                "sequence_items": copy.deepcopy(pool["items"]), "sequence_cursor": 0}]
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.tabs.setCurrentWidget(dialog.resources)
            dialog.resources.setCurrentWidget(dialog.custom_pages[pool["id"]])
            with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                dialog.delete_resource_pool()
            saved = store.load()
            self.assertEqual(saved["resource_pools"], [])
            layer = next(layer for layer in saved["settings"]["layers"] if layer["id"] == "layer")
            self.assertEqual(layer["source"], "sequence")
            self.assertEqual(layer["sequence_items"][0]["path"], image_path)
            self.assertTrue(Path(image_path).is_file())
            dialog.close()
            dialog.deleteLater()

    def test_music_selection_profile_switch_and_pool_delete_update_combo(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            store = Store(root)
            state = fresh_state()
            pool = create_pool(state, "音乐 A", "music")
            state["settings"]["music_pool_id"] = pool["id"]
            state = store.save(state, 0)
            first = state["active_profile"]
            second = store.create_profile(state, "空配置")
            second_id = second["active_profile"]
            store.select_profile(second, first)
            dialog = BatchTextVideoDialog(store=store)
            self.assertEqual(dialog.controls["music_pool_id"].currentData(), pool["id"])
            dialog.switch_profile(second_id)
            self.assertEqual(dialog.custom_tables, {})
            self.assertEqual(dialog.controls["music_pool_id"].currentData(), "")
            dialog.switch_profile(first)
            self.assertEqual(dialog.controls["music_pool_id"].currentData(), pool["id"])
            dialog.tabs.setCurrentWidget(dialog.resources)
            dialog.resources.setCurrentWidget(dialog.custom_pages[pool["id"]])
            with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                dialog.delete_resource_pool()
            self.assertEqual(store.load()["settings"]["music_pool_id"], "")
            self.assertEqual(dialog.controls["music_pool_id"].count(), 1)
            dialog.close()
            dialog.deleteLater()

    def test_preview_hash_tracks_selected_music_pool_and_explicit_no_voice(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            pool = create_pool(state, "音乐", "music")
            add_pool_paths(pool, [self.fixture(root, "music.wav")])
            job = {"path": self.fixture(root, "video.mp4"), "title": "", "body": ""}
            first = preview_key(job, state)
            state["settings"]["music_pool_id"] = pool["id"]
            second = preview_key(job, state)
            self.assertNotEqual(first, second)
            job["voice_override"] = True
            self.assertNotEqual(second, preview_key(job, state))

    def test_failed_export_does_not_advance_custom_music_pool(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            pool = create_pool(state, "音乐", "music")
            state["settings"]["music_pool_id"] = pool["id"]
            apply_pairs(state, pairing_plan([new_copy()], [self.fixture(root, "video.mp4")]))
            state = store.save(state, 0)
            worker = BatchWorker(store, state, [state["jobs"][0]["id"]])
            with patch.dict("app_plugins.builtin.batch_text_video.ui.RENDERERS", {"static_text": Mock(side_effect=ValueError("test failure"))}):
                worker.run()
            self.assertEqual(get_pool(store.load(), pool["id"]), get_pool(state, pool["id"]))


FFMPEG = os.environ.get("BATCH_TEST_FFMPEG", str(APP_ROOT / "runtime" / "ffmpeg" / "ffmpeg.exe"))


@unittest.skipUnless(Path(FFMPEG).is_file(), "Optional local FFmpeg smoke test")
class CustomPoolRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_real_render_custom_voice_music_and_explicit_no_voice(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            ffmpeg, ffprobe = resolve_tools(FFMPEG)
            def generate(name, source, seconds, *codec):
                target = root / name
                subprocess.run([ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source,
                    "-t", str(seconds), *codec, str(target)], check=True, capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return str(target)
            video = generate("bg.mp4", "color=c=blue:s=240x320:r=24", 1, "-c:v", "libx264", "-pix_fmt", "yuv420p")
            voice = generate("voice.wav", "sine=frequency=600:sample_rate=48000", 2)
            music = generate("music.wav", "sine=frequency=220:sample_rate=48000", 3)
            state = fresh_state()
            voice_pool = create_pool(state, "语音", "voice")
            music_pool = create_pool(state, "配乐", "music")
            add_pool_paths(voice_pool, [voice])
            add_pool_paths(music_pool, [music])
            state["settings"].update(aspect="source", use_voice=True, music_pool_id=music_pool["id"],
                ffmpeg_path=FFMPEG, skip_silence=False, loop_transition="none", output_dir=str(root / "out"))
            source = {**new_copy(), "title": "Test", "task_dir": str(root / "task"), "name": "with_voice"}
            apply_pairs(state, assign_voices(pairing_plan([source], [video]), voice_pool["items"], "rotate"))
            store = Store(root / "state")
            state = store.save(state, 0)
            output, plan = render_static_text(state["jobs"][0], state, store.directory)
            info = probe(output, ffprobe)
            self.assertEqual(output.parent, root / "task")
            self.assertAlmostEqual(info["duration"], 2, delta=.12)
            self.assertTrue(info["audio"])
            self.assertTrue(all(Path(p).is_file() for p in (video, voice, music)))
            saved = store.completed(state, state["jobs"][0]["id"], output, plan)
            self.assertAlmostEqual(get_pool(saved, music_pool["id"])["items"][0]["cursor"], 2, delta=.12)
            job = {**saved["jobs"][0], "name": "without_voice", "voice_path": "", "voice_override": True}
            short, _ = render_static_text(job, saved, store.directory)
            self.assertAlmostEqual(probe(short, ffprobe)["duration"], 1, delta=.12)


if __name__ == "__main__":
    unittest.main()
