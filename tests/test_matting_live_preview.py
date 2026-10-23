"""Real Qt parameter events, latest-result preview, and material-local history."""
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from qt_compat import QtCore, QtTest, QtWidgets
from app_plugins.builtin.batch_text_video import matting_render as render
from app_plugins.builtin.batch_text_video.matting_ui import MattingDialog
import test_matting_studio as fixture


class LiveMattingTests(unittest.TestCase):
    setUp = fixture.MattingTests.setUp
    wait = fixture.MattingTests.wait
    publish = fixture.MattingTests.publish

    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def dialog(self):
        dialog = MattingDialog([str(self.source)], catalogue=self.catalogue)
        def cleanup():
            dialog.cancel_work(); self.wait(lambda: dialog.worker is None)
            dialog.close(); dialog.deleteLater()
        self.addCleanup(cleanup)
        dialog.show(); self.wait(lambda: dialog.worker is None)
        return dialog

    def idle(self, dialog):
        self.wait(lambda: dialog.worker is None and not dialog.timer.isActive() and dialog._pending is None)

    def assert_current_image(self, dialog):
        np.testing.assert_array_equal(render.image_array(dialog.canvas.cutout), render.refined(dialog.base, dialog.values))

    def test_saved_result_parameter_edit_automatically_rebuilds_base_and_changes_preview(self):
        self.publish(); before = self.catalogue.path.read_bytes()
        dialog = self.dialog(); self.assertIsNone(dialog.base)
        original = render.image_array(dialog.canvas.cutout)
        dialog.controls['opacity'].setValue(25); self.idle(dialog)
        self.assertIsNotNone(dialog.base)
        self.assertLess(render.image_array(dialog.canvas.cutout)[20, 20, 3], original[20, 20, 3])
        self.assert_current_image(dialog)
        self.assertEqual(self.catalogue.path.read_bytes(), before)

    def test_editing_without_generate_updates_mask_and_reuses_inference(self):
        dialog = self.dialog()
        dialog.controls['opacity'].setValue(50); self.idle(dialog)
        calls = fixture.FakePredictor.calls
        dialog.controls['blur'].setValue(2); dialog.controls['grow'].setValue(2); self.idle(dialog)
        self.assert_current_image(dialog); self.assertEqual(fixture.FakePredictor.calls, calls)
        dialog.view.setCurrentIndex(dialog.view.findData('mask'))
        self.assertFalse(dialog.canvas.mask.isNull())
        dialog.view.setCurrentIndex(dialog.view.findData('original'))
        self.assertIn('原始素材', dialog.view_note.text())

    def test_ctrl_z_y_and_shift_z_in_numeric_editor_restore_values_and_preview(self):
        dialog = self.dialog(); control = dialog.controls['opacity']
        control.setFocus(); control.selectAll()
        QtTest.QTest.keyClicks(control, '40'); QtTest.QTest.keyClick(control, QtCore.Qt.Key.Key_Tab)
        self.idle(dialog); self.assertEqual(dialog.values['opacity'], 40)
        control.setFocus()
        QtTest.QTest.keyClick(control.lineEdit(), QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.idle(dialog); self.assertEqual(dialog.values['opacity'], 100); self.assertEqual(control.value(), 100)
        self.assert_current_image(dialog)
        QtTest.QTest.keyClick(control.lineEdit(), QtCore.Qt.Key.Key_Y, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.idle(dialog); self.assertEqual(dialog.values['opacity'], 40)
        dialog.undo_edit(); self.idle(dialog)
        QtTest.QTest.keyClick(control.lineEdit(), QtCore.Qt.Key.Key_Z,
                              QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
        self.idle(dialog); self.assertEqual(dialog.values['opacity'], 40); self.assert_current_image(dialog)

    def test_parameter_brush_clear_and_reset_share_one_undo_history(self):
        dialog = self.dialog(); dialog.controls['opacity'].setValue(40); self.idle(dialog)
        stroke = dict(mode='remove', radius=.05, points=[[.5, .5]])
        dialog.canvas.strokes.append(copy.deepcopy(stroke)); dialog.add_stroke(stroke); self.idle(dialog)
        dialog.clear_button.click(); self.idle(dialog); self.assertFalse(dialog.values['strokes'])
        dialog.undo_edit(); self.idle(dialog); self.assertEqual(dialog.values['strokes'], [stroke])
        before = copy.deepcopy(dialog.values)
        dialog.reset_values(); self.idle(dialog); self.assertEqual(dialog.values['opacity'], 100)
        dialog.undo_edit(); self.idle(dialog); self.assertEqual(dialog.values, before)
        dialog.redo_edit(); self.idle(dialog); self.assertFalse(dialog.values['strokes']); self.assert_current_image(dialog)

    def test_each_material_keeps_its_draft_and_undo_redo_history(self):
        second = self.root/'second.png'; Image.fromarray(self.pixels).save(second)
        dialog = self.dialog(); dialog.controls['opacity'].setValue(25); self.idle(dialog)
        dialog.add_sources([str(second)]); dialog.files.setCurrentRow(1); self.idle(dialog)
        dialog.controls['opacity'].setValue(60); self.idle(dialog)
        dialog.undo_edit(); self.idle(dialog); self.assertEqual(dialog.values['opacity'], 100)
        dialog.files.setCurrentRow(0); self.idle(dialog); self.assertEqual(dialog.values['opacity'], 25)
        self.assert_current_image(dialog)
        dialog.undo_edit(); self.idle(dialog); self.assertEqual(dialog.values['opacity'], 100)
        dialog.files.setCurrentRow(1); self.idle(dialog)
        dialog.redo_edit(); self.idle(dialog); self.assertEqual(dialog.values['opacity'], 60)
        self.assert_current_image(dialog)

    def test_pending_changes_reuse_slow_inference_and_never_paint_old_parameter_result(self):
        dialog = self.dialog(); started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Slow(fixture.FakePredictor):
            def predict(self, rgb):
                started.set()
                if not release.wait(5): raise RuntimeError('test gate timeout')
                return super().predict(rgb)
        with patch.object(render, 'Predictor', Slow):
            dialog.controls['opacity'].setValue(40); self.wait(started.is_set)
            dialog.controls['opacity'].setValue(20)
            self.wait(lambda: dialog._pending is not None)
            self.assertFalse(dialog.worker.cancel.is_set())
            images = []
            original = dialog.canvas.set_images
            def display(source, cutout=None):
                if cutout is not None: images.append(render.image_array(cutout)[20, 20, 3])
                original(source, cutout)
            with patch.object(dialog.canvas, 'set_images', side_effect=display):
                release.set(); self.idle(dialog)
            self.assertEqual(Slow.calls, 1)
            self.assertEqual(images, [26]); self.assert_current_image(dialog)

    def test_invalid_clip_draft_can_be_undone_without_saving_or_losing_valid_history(self):
        dialog = self.dialog(); dialog.controls['black_clip'].setValue(10); self.idle(dialog)
        dialog.controls['white_clip'].setValue(5)
        self.assertFalse(dialog._parameters_valid); self.assertFalse(dialog.save_button.isEnabled())
        self.assertTrue(dialog.undo_edit()); self.idle(dialog)
        self.assertEqual(dialog.values['black_clip'], 10); self.assertEqual(dialog.controls['white_clip'].value(), 100)
        dialog.undo_edit(); self.idle(dialog); self.assertEqual(dialog.values['black_clip'], 0)
        self.assert_current_image(dialog)

    def test_explicit_cancel_discards_pending_preview_and_does_not_restart(self):
        dialog = self.dialog(); started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Slow(fixture.FakePredictor):
            def predict(self, rgb):
                started.set()
                if not release.wait(5): raise RuntimeError('test gate timeout')
                return super().predict(rgb)
        with patch.object(render, 'Predictor', Slow):
            dialog.controls['opacity'].setValue(40); self.wait(started.is_set)
            dialog.controls['opacity'].setValue(20); self.wait(lambda: dialog._pending is not None)
            dialog.cancel_work(); release.set(); self.idle(dialog)
            self.assertIsNone(dialog.base); self.assertTrue(dialog.canvas.cutout.isNull())
            self.assertIn('已取消', dialog.status.text())

    def test_switching_material_during_refinement_locks_new_source_until_loaded(self):
        second = self.root/'second.png'; Image.fromarray(self.pixels).save(second)
        dialog = self.dialog(); started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Slow(fixture.FakePredictor):
            def predict(self, rgb):
                started.set()
                if not release.wait(5): raise RuntimeError('test gate timeout')
                return super().predict(rgb)
        with patch.object(render, 'Predictor', Slow):
            dialog.controls['opacity'].setValue(40); self.wait(started.is_set)
            dialog.add_sources([str(second)]); dialog.files.setCurrentRow(1)
            self.assertFalse(dialog.parameter_page.isEnabled()); self.assertFalse(dialog.undo_edit())
            release.set(); self.idle(dialog)
            self.assertEqual(Path(dialog.path), second); self.assertEqual(dialog.values['opacity'], 100)
            self.assertTrue(dialog.parameter_page.isEnabled())

    def test_model_change_undo_rebuilds_the_correct_base(self):
        dialog = self.dialog(); model = dialog.controls['model']
        model.setCurrentIndex(model.findData('rvm')); self.idle(dialog)
        self.assertEqual(dialog.base['model'], 'rvm')
        dialog.undo_edit(); self.idle(dialog); self.assertEqual(dialog.base['model'], 'isnet')
        self.assertEqual(model.currentData(), 'isnet'); self.assert_current_image(dialog)

    def test_visible_brush_buttons_draw_a_stroke_and_ctrl_z_removes_it(self):
        dialog = self.dialog()
        self.assertTrue(dialog.brush_buttons['keep'].isVisible())
        self.assertFalse(dialog.brush_buttons['keep'].isEnabled())
        dialog.submit('infer'); self.idle(dialog)
        dialog.brush_buttons['remove'].click()
        self.assertEqual(dialog.canvas.mode, 'remove')
        point = dialog.canvas.image_rect().center().toPoint()
        QtTest.QTest.mouseClick(dialog.canvas, QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.idle(dialog); self.assertEqual(len(dialog.values['strokes']), 1)
        QtTest.QTest.keyClick(dialog.canvas, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.idle(dialog); self.assertFalse(dialog.values['strokes']); self.assert_current_image(dialog)


class InlineMattingTests(unittest.TestCase):
    from test_batch_review_regressions import BatchReviewRegressions as Fixture
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    wait = Fixture.wait

    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_inline_key_preview_and_ctrl_z_from_a_lazily_created_numeric_editor(self):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        path = Path(self.temp.name)/'green.png'; Image.new('RGB', (64, 48), '#00ff00').save(path)
        layer = normalize_layers([dict(id='visual', kind='image', path=str(path), key_mode='green', width=100, height=100)])[0]
        dialog = self.dialog
        dialog.layer_panel.set_layers([layer]); dialog.layer_panel.select_layer_id('visual')
        dialog.edit_timer.stop(); dialog.refresh_editor_baseline()
        dialog.update_preview(); self.wait(lambda: dialog.overlay_worker is None)
        original_alpha = dialog.preview.overlay.pixelColor(dialog.preview.overlay.width()//2, dialog.preview.overlay.height()//2).alpha()
        editor = dialog.layer_panel.editor.visual_properties
        editor.sections.sections['visual'].set_expanded(True)
        mode = editor.controls['key_mode']; mode.setCurrentIndex(mode.findData('blue'))
        self.wait(lambda: not dialog.edit_timer.isActive() and dialog.overlay_worker is None)
        self.assertEqual(dialog.layer_panel.current()['key_mode'], 'blue')
        self.assertGreater(dialog.preview.overlay.pixelColor(dialog.preview.overlay.width()//2, dialog.preview.overlay.height()//2).alpha(), 240)
        numeric = editor.controls['key_similarity']; numeric.setFocus(); self.app.processEvents()
        QtTest.QTest.keyClick(numeric.lineEdit(), QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.wait(lambda: dialog.overlay_worker is None)
        self.assertEqual(dialog.layer_panel.current()['key_mode'], 'green')
        self.assertEqual(dialog.preview.overlay.pixelColor(dialog.preview.overlay.width()//2, dialog.preview.overlay.height()//2).alpha(), original_alpha)


if __name__ == '__main__': unittest.main()
