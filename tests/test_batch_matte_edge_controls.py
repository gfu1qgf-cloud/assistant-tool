"""Usable basic despill and fractional matte geometry across preview/render."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore, QtWidgets
import test_chroma_edge_despill as helper
from app_plugins.builtin.batch_text_video.visual_media import keyed_image, VisualProperties, normalize_visual
from app_plugins.builtin.batch_text_video.matting_settings import refine, normalize


class MatteEdgeControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def layer(self, **values):
        result = dict(key_mode='green', key_method='normal', key_similarity=.05,
                      key_softness=.6, key_despill=True, key_despill_mode='basic')
        result.update(values); normalize_visual(result); return result

    def test_basic_strength_zero_is_identity_and_partial_strength_interpolates(self):
        image = helper.sample(); original = helper.pixels(keyed_image(image, self.layer(key_despill=False)))
        zero = helper.pixels(keyed_image(image, self.layer(key_despill_amount=0)))
        np.testing.assert_array_equal(zero, original)
        full = helper.pixels(keyed_image(image, self.layer()))
        half = helper.pixels(keyed_image(image, self.layer(key_despill_amount=50)))
        self.assertGreater(half[35, 12, 1], full[35, 12, 1])
        self.assertLess(half[35, 12, 1], original[35, 12, 1])
        np.testing.assert_array_equal(half[:, :, 3], original[:, :, 3])

    def test_basic_luma_restores_brightness_without_reintroducing_green(self):
        image = helper.sample(); layer = self.layer()
        source = helper.pixels(keyed_image(image, dict(layer, key_despill=False)))[35, 12, :3].astype(float)
        dark = helper.pixels(keyed_image(image, dict(layer, key_despill_basic_luma=0)))[35, 12, :3].astype(float)
        light = helper.pixels(keyed_image(image, dict(layer, key_despill_basic_luma=100)))[35, 12, :3].astype(float)
        weights = np.array([.299, .587, .114])
        self.assertGreater(light@weights, dark@weights+30)
        self.assertLess(abs(light@weights-source@weights), 2)
        self.assertLess(light[1]-light[0], 2)

    def test_edge_softness_reduces_mid_band_but_keeps_full_edge_and_core(self):
        from app_plugins.builtin.batch_text_video.chroma_despill import edge_mask
        alpha = np.zeros((40, 80), np.uint8); alpha[8:32, 8:72] = 255
        base = edge_mask(alpha, self.layer())
        soft = edge_mask(alpha, self.layer(key_despill_softness=100))
        self.assertTrue(np.all(soft <= base))
        self.assertTrue(np.any((base > soft) & (soft > 0)))
        self.assertEqual(soft[0, 0], 255); self.assertEqual(soft[20, 40], 0)

    def test_basic_preserves_unspilled_skin_and_recovers_bright_robe_luminance(self):
        data = np.full((12, 20, 4), (190, 155, 120, 255), np.uint8)
        data[:, 10:, :3] = (210, 235, 200)
        layer = self.layer(key_despill_basic_luma=100)
        result = helper.pixels(keyed_image(helper.picture(data), layer))
        np.testing.assert_array_equal(result[:, :10], data[:, :10])
        weights = np.array([.299, .587, .114])
        self.assertLess(abs(result[5, 15, :3]@weights-data[5, 15, :3]@weights), 2)
        self.assertLessEqual(int(result[5, 15, 1]), int(result[5, 15, 0]))

    def test_fractional_shrink_expand_and_native_transparency(self):
        image = helper.sample(); base = helper.pixels(keyed_image(image, self.layer(key_despill=False)))
        small = helper.pixels(keyed_image(image, self.layer(key_despill=False, key_matte_grow=-.5)))
        full = helper.pixels(keyed_image(image, self.layer(key_despill=False, key_matte_grow=-1)))
        wide = helper.pixels(keyed_image(image, self.layer(key_despill=False, key_matte_grow=.5)))
        self.assertLess(small[:, :, 3].sum(), base[:, :, 3].sum())
        self.assertGreater(small[:, :, 3].sum(), full[:, :, 3].sum())
        self.assertGreater(wide[:, :, 3].sum(), base[:, :, 3].sum())
        rgba = helper.pixels(image); rgba[10:30, 10:30, 3] = 0
        expanded = helper.pixels(keyed_image(helper.picture(rgba), self.layer(key_matte_grow=2)))
        self.assertFalse(expanded[10:30, 10:30, 3].any())

    def test_ai_grow_accepts_fractions_and_preserves_keep_brush_and_native_alpha(self):
        rgb = np.full((30, 40, 3), 160, np.uint8); alpha = np.zeros((30, 40), np.float32)
        alpha[5:25, 5:35] = 1; native = np.full((30, 40), 255, np.uint8); native[5, 7] = 0
        half = refine(rgb, alpha, native, dict(grow=-.5))
        full = refine(rgb, alpha, native, dict(grow=-1))
        self.assertGreater(half[:, :, 3].sum(), full[:, :, 3].sum())
        self.assertEqual(half[5, 7, 3], 0)
        keep = refine(rgb, alpha, native, dict(grow=-.5, strokes=[dict(mode='keep', radius=.03, points=[[5/39, 6/29]])]))
        self.assertEqual(keep[6, 5, 3], 255)

    def test_new_settings_validate_and_ui_enables_only_relevant_controls(self):
        for field in ('key_matte_grow', 'key_despill_basic_luma', 'key_despill_softness'):
            with self.subTest(field=field), self.assertRaises(ValueError): self.layer(**{field: float('nan')})
        panel = VisualProperties()
        try:
            panel.set_layer(self.layer())
            self.assertTrue(panel.controls['key_despill_amount'].isEnabled())
            self.assertTrue(panel.controls['key_despill_basic_luma'].isEnabled())
            self.assertTrue(panel.controls['key_matte_grow'].isEnabled())
            self.assertFalse(panel.controls['key_despill_width'].isEnabled())
            panel.controls['key_matte_grow'].setValue(-.25)
            self.assertEqual(panel.layer['key_matte_grow'], -.25)
            panel.controls['key_despill_mode'].setCurrentIndex(panel.controls['key_despill_mode'].findData('edge'))
            self.assertTrue(panel.controls['key_despill_softness'].isEnabled())
            self.assertFalse(panel.controls['key_despill_basic_luma'].isEnabled())
        finally: panel.deleteLater()

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), 'Explicit FFmpeg required')
    def test_native_and_design_match_basic_strength_luma_and_fractional_geometry(self):
        processor = helper.EdgeDespillTests()
        for mode in ('green', 'blue'):
            image = helper.sample(blue=mode == 'blue')
            for method in ('normal', 'enhanced'):
                for amount, grow in ((0, 0), (37.5, -.5), (100, .25)):
                    with self.subTest(mode=mode, method=method, amount=amount, grow=grow):
                        layer = self.layer(key_mode=mode, key_method=method, key_despill_amount=amount,
                            key_matte_grow=grow, key_despill_basic_luma=75)
                        expected = helper.pixels(keyed_image(image, layer)); actual = processor.native(image, layer)
                        visible = (expected[:, :, 3] > 10) & (actual[:, :, 3] > 10)
                        delta = abs(expected.astype(int)-actual.astype(int))
                        self.assertLessEqual(delta[:, :, 3].max(), 2)
                        self.assertLessEqual(delta[:, :, :3][visible].max(), 3)

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), 'Explicit FFmpeg required')
    def test_native_and_design_match_soft_edge_band(self):
        processor = helper.EdgeDespillTests()
        for softness in (35.5, 100):
            layer = self.layer(key_despill_mode='edge', key_despill_softness=softness,
                               key_matte_grow=-.25, key_despill_luma=75)
            processor.compare(helper.sample(), layer)

    def test_saved_ai_grow_composes_once_with_layer_adjustment(self):
        from app_plugins.builtin.batch_text_video import matting_assets as assets
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'source.png'; helper.sample().save(str(source))
            record = dict(id='ai', mode='dynamic', source=assets.fingerprint(source), parameters=normalize(dict(grow=.5)))
            layer = normalize_layers([dict(id='p', kind='image', path=str(source), key_method='ai',
                key_matte_grow=-.25, key_despill=True, key_mode='green', key_despill_softness=65)])[0]
            with patch.object(assets.Catalogue, 'lookup', return_value=record): bound = assets.apply_saved(layer)
            parameters = bound['_matting_binding']['parameters']
            self.assertEqual(parameters['grow'], .25); self.assertEqual(parameters['despill_softness'], 65)
            self.assertEqual(assets.apply_saved(bound)['_matting_binding']['parameters'], parameters)

    def test_save_reload_and_node_conversion_keep_adjustments(self):
        from app_plugins.builtin.batch_text_video.store import Store, fresh_state, DEFAULTS
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'source.png'; helper.sample().save(str(source))
            layer = normalize_layers([dict(self.layer(key_matte_grow=-.5, key_despill_basic_luma=65,
                key_despill_amount=75, key_despill_softness=80), id='p', kind='image', path=str(source))])[0]
            state = fresh_state(); state['settings']['layers'] = [layer]; store = Store(Path(folder)/'data'); store.save(state, 0)
            saved = store.load()['settings']['layers'][0]; graph, _ = component_graph(saved, DEFAULTS, fallback_registry())
            node = next(node for node in graph['nodes'] if node['kind'] == 'batch_text_video.visual_layer')
            for key in ('key_matte_grow', 'key_despill_basic_luma', 'key_despill_softness', 'key_despill_amount'):
                self.assertEqual(saved[key], layer[key]); self.assertEqual(node['params']['layer'][key], layer[key])

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), 'Explicit FFmpeg required')
    def test_real_moving_video_recomputes_fractional_matte_and_basic_despill(self):
        from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source
        from app_plugins.builtin.batch_text_video.visual_media import is_animated
        processor = helper.EdgeDespillTests()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); frames = [helper.pixels(helper.sample()), np.roll(helper.pixels(helper.sample()), 9, axis=1)]
            for index, frame in enumerate(frames): helper.picture(frame).save(str(root/f'frame{index}.png'))
            video = root/'moving.mov'
            processor.ffmpeg(['-framerate', '10', '-i', str(root/'frame%d.png'), '-frames:v', '2',
                              '-c:v', 'png', '-pix_fmt', 'rgba', str(video)])
            layer = self.layer(key_matte_grow=-.5, key_despill_amount=45, key_despill_basic_luma=80)
            filters = []; args = ['-i', str(video)]
            pad, _ = add_keyed_source(args, filters, layer, 0, 'moving', 10, is_animated, duration=.2)
            raw = processor.ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '['+pad+']',
                '-frames:v', '2', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'])
            actual = np.frombuffer(raw, np.uint8).reshape(2, 80, 120, 4)
            for index, frame in enumerate(frames):
                expected = helper.pixels(keyed_image(helper.picture(frame), layer))
                self.assertLessEqual(abs(actual[index].astype(int)-expected.astype(int)).max(), 2)
            self.assertFalse(np.array_equal(actual[0, :, :, 3], actual[1, :, :, 3]))


import test_batch_review_regressions as fixture


class MattePresetUndoTests(unittest.TestCase):
    setUp = fixture.BatchReviewRegressions.setUp
    tearDown = fixture.BatchReviewRegressions.tearDown
    wait = fixture.BatchReviewRegressions.wait

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def test_quick_edge_preset_is_one_undo_and_redo_restores_all_parameters(self):
        from PyQt6.QtTest import QTest
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        path = Path(self.temp.name)/'green.png'; helper.sample().save(str(path))
        owner = self.dialog
        layer = normalize_layers([dict(id='person', kind='image', path=str(path), key_mode='green')])[0]
        owner.layer_panel.set_layers([layer]); owner.layer_panel.select_layer_id('person')
        owner.edit_timer.stop(); owner.refresh_editor_baseline(); owner.edit_history.clear()
        editor = owner.layer_panel.editor.visual_properties
        editor.sections.sections['visual'].set_expanded(True)
        editor.edge_preset.click(); owner.persist(); self.wait(lambda: owner.overlay_worker is None)
        selected = copy.deepcopy(owner.layer_panel.current())
        self.assertEqual(selected['key_matte_grow'], -.5)
        self.assertEqual(selected['key_despill_softness'], 70)
        self.assertEqual(len(owner.edit_history.past), 1)
        control = editor.controls['key_matte_grow']; control.setFocus(); self.app.processEvents()
        QTest.keyClick(control.lineEdit(), QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.wait(lambda: owner.overlay_worker is None)
        self.assertEqual(owner.layer_panel.current()['key_matte_grow'], 0)
        self.assertFalse(owner.layer_panel.current()['key_despill'])
        self.assertTrue(owner.redo_edit()); self.wait(lambda: owner.overlay_worker is None)
        for key in ('key_matte_grow', 'key_despill_softness', 'key_despill_amount', 'key_despill_luma'):
            self.assertEqual(owner.layer_panel.current()[key], selected[key])
        screenshot = os.environ.get('LZX_MATTE_CONTROLS_SCREENSHOT')
        if screenshot:
            editor = owner.layer_panel.editor.visual_properties
            editor.controls['key_despill_mode'].setCurrentIndex(editor.controls['key_despill_mode'].findData('basic'))
            owner.edit_timer.stop(); self.app.processEvents()
            self.assertTrue(owner.grab().save(screenshot))
            editor = owner.layer_panel.editor.visual_properties
            self.assertTrue(editor.sections.sections['visual'].content.grab().save(str(Path(screenshot).with_name('matte-properties.png'))))
