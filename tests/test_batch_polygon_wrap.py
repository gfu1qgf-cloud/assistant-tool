import copy
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.polygon_wrap import normalize_polygons, polygon_exclusions, MAX_VERTICES
from app_plugins.builtin.batch_text_video.polygon_editor import PolygonCanvas, PolygonDialog
from app_plugins.builtin.batch_text_video.text_wrap import intervals, bind_exclusions, exclusions
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay
from app_plugins.builtin.batch_text_video.preview import Preview
from app_plugins.builtin.batch_text_video.store import Store, fresh_state, DEFAULTS
from test_batch_text_wrap import text, obstacle, disabled_defaults
import test_batch_text_wrap as wrap_tests


def triangle(**changes):
    item = dict(id='triangle', points=[[50, 15], [75, 62], [25, 62]], enabled=True)
    item.update(changes)
    return item


class PolygonWrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def image(self):
        image = QtGui.QImage(360, 640, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#182a3d')); return image

    def test_triangle_bands_vary_width_and_concave_shape_preserves_open_center(self):
        shape = polygon_exclusions([triangle()], 100, 100, 0)[0]
        top = intervals(100, 16, 2, [shape])
        bottom = intervals(100, 55, 2, [shape])
        self.assertGreater(top[0][1], bottom[0][1]+15)
        concave = triangle(points=[[10, 10], [30, 10], [30, 70], [70, 70], [70, 10], [90, 10], [90, 90], [10, 90]])
        shape = polygon_exclusions(normalize_polygons([concave]), 100, 100, 0)[0]
        self.assertEqual(intervals(100, 20, 10, [shape]), [(0., 10.), (30., 40.), (90., 10.)])
        self.assertEqual(intervals(100, 80, 5, [shape]), [(0., 10.), (90., 10.)])

    def test_margin_expands_the_actual_contour_and_resolution_scaling_matches(self):
        for width, height in ((100, 100), (1000, 1000)):
            gap = width*.03
            shape = polygon_exclusions([triangle()], width, height, gap)[0]
            self.assertTrue(shape.path.contains(QtCore.QPointF(width*.5, height*.14)))
            self.assertFalse(shape.path.contains(QtCore.QPointF(width*.25, height*.16)))
            self.assertAlmostEqual(shape.left()/width, .22, delta=.002)

    def test_rejects_crossing_duplicate_flat_nan_out_of_bounds_and_huge_shapes(self):
        bad = [dict(points=[[10, 10], [90, 90], [10, 90], [90, 10]]),
               dict(points=[[10, 10], [20, 10], [10, 10]]), dict(points=[[10, 10], [20, 20], [30, 30]]),
               dict(points=[[10, 10], [20, 20], [math.nan, 40]]), dict(points=[[10, 10], [20, 20], [101, 40]]),
               dict(points=[[True, 10], [20, 20], [30, 40]]), dict(points=[[20, 10], [40, 20]]),
               dict(points=[[i, 20] for i in range(MAX_VERTICES+1)]), dict(enabled=1)]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_polygons([triangle(**changes)])
        with self.assertRaises(ValueError): normalize_polygons([triangle(), triangle()])
        normalize_polygons([triangle(points=list(reversed(triangle()['points'])))])

    def test_large_contour_batch_validation_reuses_only_unchanged_geometry(self):
        from app_plugins.builtin.batch_text_video.polygon_wrap import _validate_geometry
        contour = triangle(points=[[50+30*math.cos(i*2*math.pi/128), 50+30*math.sin(i*2*math.pi/128)] for i in range(128)])
        _validate_geometry.cache_clear()
        for _ in range(782):
            normalize_polygons([contour])
        self.assertEqual(_validate_geometry.cache_info().misses, 1)
        self.assertEqual(_validate_geometry.cache_info().hits, 781)
        changed = copy.deepcopy(contour); changed['points'][0] = [79, 50]
        normalize_polygons([changed]); self.assertEqual(_validate_geometry.cache_info().misses, 2)

    def test_shaped_unicode_ink_never_crosses_polygon_and_all_text_is_consumed(self):
        for vertical in ('top', 'center', 'bottom'):
            layer = text(text=('Words 中文 😀 é around the outline. '*35), vertical=vertical,
                         wrap_targets=[], wrap_polygons=[triangle()])
            helper = wrap_tests.TextWrapTests()
            box, shapes, groups, total, _ = helper.shape(layer, [])
            self.assertTrue(math.isfinite(total))
            for native, _ in groups:
                length = 0
                for i in range(native.lineCount()):
                    line = native.lineAt(i); length += line.textLength()
                    rect = line.naturalTextRect().translated(box.topLeft())
                    self.assertFalse(shapes[0].path.intersects(rect))
                self.assertEqual(length, len(layer['text'].encode('utf-16-le'))//2)

    def test_disabled_polygon_preserves_rectangular_render_and_defaults_are_independent(self):
        layer = text(wrap_targets=[], wrap_polygons=[triangle(enabled=False)])
        reference = text(wrap_enabled=False)
        self.assertEqual(render_text_box(layer, 360, 640)[0], render_text_box(reference, 360, 640)[0])
        one, two = text(), text()
        one['wrap_polygons'].append(triangle())
        self.assertEqual(two['wrap_polygons'], [])

    def test_polygon_leaves_text_in_bbox_corners_and_clips_background_and_shadow(self):
        layer = text(text='Words around the hand drawn contour. '*35, wrap_targets=[], wrap_gap=0,
                     wrap_polygons=[triangle()], background_enabled=True, background_opacity=100,
                     background_color='#ff0000', shadow_enabled=True, shadow_blur=8)
        image, _ = render_text_box(layer, 360, 640)
        self.assertEqual(image.pixelColor(180, 240).alpha(), 0)
        self.assertGreater(image.pixelColor(100, 130).alpha(), 0, 'Outside triangle but inside its bbox stays available')

    def canvas(self, polygons=()):
        canvas = PolygonCanvas(self.image(), polygons)
        canvas.resize(500, 700); canvas.show(); self.app.processEvents()
        self.addCleanup(canvas.deleteLater)
        return canvas

    def click(self, canvas, point):
        QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=canvas.screen_point(point).toPoint())

    def test_draw_close_by_first_point_and_draft_undo_redo(self):
        canvas = self.canvas(); canvas.begin()
        for point in triangle()['points']: self.click(canvas, point)
        canvas.undo(); self.assertEqual(len(canvas.draft), 2)
        canvas.redo(); self.assertEqual(len(canvas.draft), 3)
        self.click(canvas, triangle()['points'][0])
        self.assertFalse(canvas.drawing); self.assertEqual(len(canvas.polygons), 1)
        canvas.undo(); self.assertEqual(canvas.polygons, [])
        canvas.redo(); self.assertEqual(len(canvas.polygons), 1)

    def test_drag_vertex_move_whole_shape_delete_and_cancel_gesture(self):
        canvas = self.canvas([triangle()]); before = copy.deepcopy(canvas.polygons)
        origin = canvas.screen_point([50, 15]).toPoint(); finish = canvas.screen_point([45, 12]).toPoint()
        QTest.mousePress(canvas, QtCore.Qt.MouseButton.LeftButton, pos=origin)
        QTest.mouseMove(canvas, finish); QTest.mouseRelease(canvas, QtCore.Qt.MouseButton.LeftButton, pos=finish)
        self.assertLess(canvas.polygons[0]['points'][0][0], 46)
        canvas.undo(); self.assertEqual(canvas.polygons, before)
        origin = canvas.screen_point([50, 40]).toPoint(); finish = canvas.screen_point([55, 44]).toPoint()
        QTest.mousePress(canvas, QtCore.Qt.MouseButton.LeftButton, pos=origin)
        QTest.mouseMove(canvas, finish); QTest.mouseRelease(canvas, QtCore.Qt.MouseButton.LeftButton, pos=finish)
        self.assertGreater(canvas.polygons[0]['points'][0][0], 54)
        canvas.delete_region(); self.assertEqual(canvas.polygons, [])
        canvas.undo(); self.assertEqual(len(canvas.polygons), 1)
        canvas.begin(); self.click(canvas, [20, 20]); canvas.cancel_gesture()
        self.assertFalse(canvas.drawing); self.assertEqual(len(canvas.polygons), 1)

    def test_invalid_crossing_drag_restores_previous_shape(self):
        square = triangle(points=[[20, 20], [80, 20], [80, 80], [20, 80]])
        canvas = self.canvas([square]); before = copy.deepcopy(canvas.polygons)
        errors = []; canvas.message.connect(errors.append)
        origin = canvas.screen_point([20, 20]).toPoint(); finish = canvas.screen_point([90, 70]).toPoint()
        QTest.mousePress(canvas, QtCore.Qt.MouseButton.LeftButton, pos=origin)
        QTest.mouseMove(canvas, finish); QTest.mouseRelease(canvas, QtCore.Qt.MouseButton.LeftButton, pos=finish)
        self.assertEqual(canvas.polygons, before); self.assertTrue(any('交叉' in message for message in errors))

    def test_double_click_adds_vertex_then_delete_and_undo_preserve_simple_shape(self):
        canvas = self.canvas([triangle(points=[[20, 20], [80, 20], [80, 80], [20, 80]])])
        QTest.mouseDClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=canvas.screen_point([50, 20]).toPoint())
        self.assertEqual(len(canvas.polygons[0]['points']), 5)
        normalize_polygons(canvas.polygons)
        canvas.delete_vertex(); self.assertEqual(len(canvas.polygons[0]['points']), 4)
        canvas.undo(); self.assertEqual(len(canvas.polygons[0]['points']), 5)

    def test_local_keyboard_shortcuts_and_incomplete_save_are_safe(self):
        dialog = PolygonDialog(self.image()); dialog.show(); self.app.processEvents()
        canvas = dialog.canvas
        for point in triangle()['points']: self.click(canvas, point)
        QTest.keyClick(canvas, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(canvas.draft), 2)
        dialog.accept(); self.assertNotEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        QTest.keyClick(canvas, QtCore.Qt.Key.Key_Y, QtCore.Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(canvas, QtCore.Qt.Key.Key_Return)
        self.assertEqual(len(canvas.polygons), 1)
        dialog.accept(); self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        dialog.deleteLater()

    def test_editor_save_cancel_signal_chain_and_global_undo(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'):
            state = fresh_state(); state['settings']['layers'] = disabled_defaults([text()])
            store = Store(root); store.save(state, 0)
            owner = BatchTextVideoDialog(store=store); owner.layer_panel.select_layer_id('paragraph')
            def accepted(dialog):
                dialog.canvas.polygons = [triangle()]
                return QtWidgets.QDialog.DialogCode.Accepted
            with patch.object(PolygonDialog, 'exec', accepted):
                owner.layer_panel.editor.wrap_properties.polygons_button.click()
            self.assertEqual(owner.layer_panel.current()['wrap_polygons'], [triangle()])
            self.assertTrue(owner.undo_edit()); owner.layer_panel.select_layer_id('paragraph')
            self.assertEqual(owner.layer_panel.current()['wrap_polygons'], [])
            self.assertTrue(owner.redo_edit()); owner.layer_panel.select_layer_id('paragraph')
            before = copy.deepcopy(owner.layer_panel.current())
            with patch.object(PolygonDialog, 'exec', return_value=QtWidgets.QDialog.DialogCode.Rejected):
                owner.layer_panel.editor.wrap_properties.polygons_button.click()
            self.assertEqual(owner.layer_panel.current(), before)
            owner.layer_panel.editor.wrap_properties.clear_polygons.click(); owner.persist()
            self.assertEqual(owner.layer_panel.current()['wrap_polygons'], [])
            owner.close()

    def test_composition_snapshot_is_bounded_and_never_captures_selection_guides(self):
        preview = Preview(); preview.canvas_size = QtCore.QSize(2160, 3840)
        preview.background = self.image()
        preview.set_layers([obstacle()], 'object')
        one = preview.composition_image()
        preview.show_bounds = False; preview.set_layers([])
        two = preview.composition_image()
        self.assertEqual(one, two); self.assertLessEqual(max(one.width(), one.height()), 1200)
        preview.deleteLater()

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Explicit real FFmpeg required')
    def test_real_polygon_export_and_playback_match_design(self):
        original = text
        def polygon_text(**kwargs):
            return original(**kwargs, wrap_targets=[], wrap_polygons=[triangle()])
        with patch('test_batch_text_wrap.text', polygon_text):
            wrap_tests.TextWrapTests.test_real_export_and_playback_match_wrapped_design_without_intermediate_video(self)

    def test_polygon_editor_screenshot(self):
        path = os.environ.get('LZX_POLYGON_SCREENSHOT')
        if not path:
            return
        layer = text(text='文字绕开手绘轮廓，轮廓外侧可以继续排版。'*30, font_min=30, font_max=30,
                     wrap_targets=[], wrap_polygons=[triangle()])
        image, _ = render_overlay('', '', 540, 960, {**DEFAULTS, 'darkness': 0, 'layers': disabled_defaults([layer])})
        opaque = QtGui.QImage(540, 960, QtGui.QImage.Format.Format_RGB32); opaque.fill(QtGui.QColor('#20252c'))
        painter = QtGui.QPainter(opaque); painter.drawImage(0, 0, image); painter.end()
        dialog = PolygonDialog(opaque, [triangle()]); dialog.show(); self.app.processEvents()
        self.assertTrue(dialog.grab().save(path)); dialog.close(); dialog.deleteLater()
