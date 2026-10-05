import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, BatchWorker
from app_plugins.builtin.batch_text_video.undo import EditHistory


class HistoryTests(unittest.TestCase):
    def test_bound_limit_and_independent_snapshots(self):
        history = EditHistory(limit=2)
        original = {"value":0}
        for value in range(1,4):
            history.record({"value":value-1}, {"value":value})
        self.assertEqual(len(history.past),2)
        output = []
        self.assertTrue(history.undo(lambda snapshot:output.append(snapshot) or True))
        self.assertEqual(output[-1], {"value":2})
        self.assertTrue(history.redo(lambda snapshot:output.append(snapshot) or True))
        self.assertEqual(output[-1], {"value":3})
        history.record(original, {"value":9})
        original["value"] = 99
        self.assertEqual(history.past[-1][0], {"value":0})

    def test_failed_restore_keeps_history_pointer_and_new_edit_clears_redo(self):
        history = EditHistory()
        history.record(0,1)
        self.assertFalse(history.undo(lambda _value:False))
        self.assertEqual(len(history.past),1)
        history.undo(lambda _value:True)
        history.record(0,2)
        self.assertEqual(history.future,[])

    def test_byte_limit_and_noop_records(self):
        history = EditHistory(byte_limit=25)
        history.record("same","same")
        self.assertFalse(history.past)
        history.record("a"*50,"b"*50)
        self.assertFalse(history.past)


class EditorUndoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def dialog(self, root):
        path = Path(root)/"video.mp4"
        path.write_bytes(b"fixture")
        state = fresh_state()
        state["jobs"] = [{"id":"j", "path":str(path), "title":"Old title", "body":"Old body",
                          "name":"Original", "voice_path":"", "status":"已完成", "output":"old.mp4"}]
        state["music"] = [{"id":"m", "path":"music.wav", "cursor":4, "last_used":"old", "audio_key":"key"}]
        state["next_music_id"] = "m"
        store = Store(Path(root)/"private")
        store.save(state,0)
        return BatchTextVideoDialog(store=store)

    def test_pending_text_can_undo_before_autosave_and_redo(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            before_music = copy.deepcopy(dialog.state["music"])
            dialog.title.setPlainText("New title")
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(dialog.title.toPlainText(),"Old title")
            self.assertEqual(dialog.store.load()["jobs"][0]["title"],"Old title")
            self.assertTrue(dialog.redo_edit())
            self.assertEqual(dialog.title.toPlainText(),"New title")
            self.assertEqual(dialog.store.load()["music"],before_music)
            self.assertEqual(dialog.state["jobs"][0]["output"],"old.mp4")
            dialog.close()

    def test_geometry_moves_in_one_step_and_second_gesture_is_separate(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            layer = dialog.layer_panel.current()
            identifier = layer["id"]
            old = {field:layer[field] for field in ("x","y","width","height")}
            for x in (55,60,65):
                dialog._preview_geometry_edited(identifier,{**old,"x":x})
            dialog._finish_geometry_edit()
            self.assertEqual(len(dialog.edit_history.past),1)
            dialog._preview_geometry_edited(identifier,{**old,"x":70})
            dialog._finish_geometry_edit()
            self.assertEqual(len(dialog.edit_history.past),2)
            dialog.undo_edit()
            self.assertEqual(dialog.layer_panel.current()["x"],65)
            dialog.undo_edit()
            self.assertEqual(dialog.layer_panel.current()["x"],old["x"])
            dialog.redo_edit()
            self.assertEqual(dialog.layer_panel.current()["x"],65)
            dialog.close()

    def test_parameter_and_layer_add_remove_reorder_can_be_undone(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            original = dialog.controls["background_blur"].value()
            dialog.controls["background_blur"].setValue(12)
            dialog.persist()
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(dialog.controls["background_blur"].value(),original)
            self.assertEqual(dialog.state["jobs"][0]["status"],"已完成")
            self.assertTrue(dialog.redo_edit())
            self.assertEqual(dialog.controls["background_blur"].value(),12)
            count = len(dialog.layer_panel.values())
            dialog.layer_panel.add_text()
            dialog.persist()
            new_id = dialog.layer_panel.current()["id"]
            self.assertEqual(len(dialog.layer_panel.values()),count+1)
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(len(dialog.layer_panel.values()),count)
            self.assertTrue(dialog.redo_edit())
            dialog.layer_panel.select_layer_id(new_id)
            dialog.layer_panel.remove_image()
            dialog.persist()
            self.assertTrue(dialog.undo_edit())
            self.assertIn(new_id,[item["id"] for item in dialog.layer_panel.values()])
            order = [item["id"] for item in dialog.layer_panel.values()]
            dialog.layer_panel.select_layer_id(order[-1])
            dialog.layer_panel.move_layer(1)
            dialog.persist()
            self.assertTrue(dialog.undo_edit())
            self.assertEqual([item["id"] for item in dialog.layer_panel.values()],order)
            dialog.close()

    def test_font_weight_and_fixed_layer_text_undo(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            layer = dialog.layer_panel.current()
            identifier = layer["id"]
            original = copy.deepcopy(layer)
            layer.update(font_weight=650, source="fixed", text="Fixed new text")
            dialog.layer_panel._text_properties_changed({"font_weight":650})
            dialog.persist()
            self.assertTrue(dialog.undo_edit())
            layer = next(item for item in dialog.layer_panel.values() if item["id"] == identifier)
            self.assertEqual(layer["font_weight"],original["font_weight"])
            self.assertEqual(layer["text"],original["text"])
            self.assertTrue(dialog.redo_edit())
            layer = next(item for item in dialog.layer_panel.values() if item["id"] == identifier)
            self.assertEqual(layer["font_weight"],650)
            self.assertEqual(layer["text"],"Fixed new text")
            dialog.close()

    def test_real_shortcut_sequence_does_not_double_undo(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            dialog.show()
            dialog.title.setFocus()
            dialog.title.setPlainText("Step 1")
            dialog.persist()
            dialog.title.setPlainText("Step 2")
            dialog.persist()
            QTest.keyClick(dialog.title,QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(dialog.title.toPlainText(),"Step 1")
            QTest.keyClick(dialog.title,QtCore.Qt.Key.Key_Y,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(dialog.title.toPlainText(),"Step 2")
            dialog.close()

    def test_uncommitted_layer_name_keeps_native_input_undo(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            dialog.show()
            editor = dialog.layer_panel.name
            original = editor.text()
            editor.setFocus()
            editor.setCursorPosition(len(original))
            QTest.keyClicks(editor,"X")
            QTest.keyClick(editor,QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(editor.text(),original)
            self.assertEqual(dialog.layer_panel.current()["name"],original)
            dialog.close()

    def test_key_filter_does_not_touch_widgets_during_destruction(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(BatchTextVideoDialog,"request_background"), \
                patch.object(BatchTextVideoDialog,"update_preview"):
            for index in range(12):
                dialog = BatchTextVideoDialog(store=Store(Path(root)/str(index)))
                dialog.close()
                dialog.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None,int(QtCore.QEvent.Type.DeferredDelete))

    def test_resource_or_profile_changes_clear_editor_history(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            dialog.controls["background_blur"].setValue(12)
            dialog.persist()
            self.assertTrue(dialog.edit_history.past)
            dialog.state["backgrounds"].append("new-resource.mp4")
            dialog.persist()
            self.assertFalse(dialog.edit_history.past)
            dialog.controls["background_blur"].setValue(10)
            dialog.persist()
            dialog.state = dialog.store.create_profile(dialog.state,"Other",False)
            dialog.apply_profile_widgets()
            self.assertFalse(dialog.edit_history.past)
            self.assertFalse(dialog.undo_edit())
            self.assertEqual(dialog.controls["background_blur"].value(),0)
            dialog.close()

    def test_export_clears_history_and_busy_undo_cannot_change_worker_snapshot(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"), \
                patch.object(BatchWorker,"start"):
            dialog = self.dialog(root)
            dialog.title.setPlainText("New title")
            dialog.persist()
            self.assertTrue(dialog.edit_history.past)
            dialog._launch_batch(["j"])
            self.assertFalse(dialog.edit_history.past)
            self.assertFalse(dialog.undo_edit())
            self.assertEqual(dialog.worker.state["jobs"][0]["title"],"New title")
            dialog._batch_finished()
            self.assertFalse(dialog.edit_history.past)
            dialog.close()

    def test_failed_store_write_does_not_move_history_or_overwrite_newer_record(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            dialog.controls["background_blur"].setValue(12)
            dialog.persist()
            external = dialog.store.load()
            external["settings"]["background_blur"] = 20
            newer = dialog.store.save(external,external["revision"])
            history = len(dialog.edit_history.past)
            self.assertFalse(dialog.undo_edit())
            self.assertEqual(len(dialog.edit_history.past),history)
            self.assertEqual(dialog.controls["background_blur"].value(),12)
            self.assertEqual(dialog.store.load(),newer)
            dialog.close()

    def test_ctrl_z_and_both_redo_keys_work_while_typing(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = self.dialog(root)
            dialog.show()
            dialog.title.setFocus()
            dialog.title.setPlainText("New title")
            def key(code, modifiers):
                event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,code,modifiers)
                QtCore.QCoreApplication.sendEvent(dialog.title,event)
            key(QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(dialog.title.toPlainText(),"Old title")
            key(QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(dialog.title.toPlainText(),"New title")
            key(QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            key(QtCore.Qt.Key.Key_Y,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(dialog.title.toPlainText(),"New title")
            dialog.close()


if __name__ == "__main__":
    unittest.main()
