"""Saved workbench parameters exclusively own the matte when opted in."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video import matting_assets as assets, matting_live as live, matting_render as render
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
from app_plugins.builtin.batch_text_video.matting_settings import normalize
from app_plugins.builtin.batch_text_video.components import resolve_sources


class ParameterOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.catalogue = assets.Catalogue(self.root/'catalogue')
        self.source = self.root/'person.png'
        self.rgb = np.full((48, 64, 3), 160, np.uint8); self.alpha = np.zeros((48, 64), np.float32)
        self.alpha[5:43, 5:59] = .95; self.alpha[18:29, 22:38] = 0
        self.native = np.full((48, 64), 255, np.uint8); Image.fromarray(np.dstack([self.rgb, self.native])).save(self.source)

    def layer(self, **values):
        return normalize_layers([dict(id='person', kind='image', path=str(self.source), use_saved_matte=True,
            key_method='ai', key_mode='green', key_protect_inside=True, key_matte_grow=2,
            key_cleanup=True, key_despill=True, key_alpha_low=35, key_alpha_high=80, **values)])[0]

    def publish(self, values):
        return self.catalogue.publish_dynamic(self.source, normalize(values), expected=assets.fingerprint(self.source))

    def test_workbench_fill_holes_wins_in_both_directions_and_all_other_parameters_stay_exact(self):
        for fill in (False, True):
            record = self.publish(dict(fill_holes=fill, grow=-.25, cleanup=False, opacity=65, despill='none'))
            layer = self.layer(); layer['key_protect_inside'] = not fill
            before = copy.deepcopy(layer); result = assets.apply_saved(layer, self.catalogue)
            self.assertEqual(result['_matting_binding']['parameters'], record['parameters'])
            self.assertEqual(result['key_mode'], 'none'); self.assertEqual(result['key_matte_grow'], 0)
            self.assertFalse(result['key_despill']); self.assertEqual(layer, before)
            self.assertEqual(assets.apply_saved(result, self.catalogue)['_matting_binding']['parameters'], record['parameters'])

    def test_no_saved_record_keeps_original_without_hidden_outer_key_or_ai(self):
        layer = self.layer(); result = assets.apply_saved(layer, self.catalogue)
        self.assertNotIn('_matting_binding', result); self.assertEqual(result['path'], str(self.source))
        self.assertEqual(result['key_mode'], 'none'); self.assertEqual(result['key_method'], 'normal')
        self.assertEqual(result['key_alpha_low'], 0); self.assertFalse(result['key_cleanup'])

    def test_unchecked_inline_ai_ignores_saved_workbench_and_uses_its_own_parameters(self):
        self.publish(dict(fill_holes=True, model='rvm', opacity=5))
        layer = self.layer(); layer.update(use_saved_matte=False, key_protect_inside=False, key_matte_grow=-.25)
        with patch.object(self.catalogue, 'lookup', side_effect=AssertionError('Unchecked AI must not load saved parameters')):
            result = assets.apply_saved(layer, self.catalogue)
            snapshot = assets.snapshot_layers([layer], self.catalogue)
        parameters = result['_matting_binding']['parameters']
        self.assertFalse(parameters['fill_holes']); self.assertEqual(parameters['grow'], -.25)
        self.assertEqual(parameters['model'], 'isnet'); self.assertEqual(parameters['opacity'], 100)
        self.assertNotIn('_matting_snapshot', snapshot[0])

    def test_outer_matte_controls_disabled_but_composition_controls_remain_enabled(self):
        panel = VisualProperties()
        try:
            layer = self.layer(); panel.set_layer(layer)
            self.assertTrue(panel.controls['use_saved_matte'].isChecked())
            for key, control in panel.controls.items():
                if key.startswith('key_'): self.assertFalse(control.isEnabled(), key)
            self.assertFalse(panel.matte_levels.isEnabled()); self.assertFalse(panel.edge_preset.isEnabled())
            self.assertTrue(panel.controls['blend_mode'].isEnabled()); self.assertTrue(panel.controls['media_loop'].isEnabled())
            self.assertTrue(panel.matting_button.isEnabled())
            panel.controls['use_saved_matte'].click()
            self.assertTrue(panel.controls['key_method'].isEnabled()); self.assertTrue(panel.controls['key_protect_inside'].isEnabled())
            self.assertTrue(panel.controls['key_matte_grow'].isEnabled()); self.assertEqual(panel.layer['key_matte_grow'], 2)
        finally: panel.deleteLater()

    def test_static_saved_cutout_is_used_as_is_without_rebuilding_or_adding_holes(self):
        result_path = self.root/'cutout.png'
        Image.fromarray(np.dstack([self.rgb, np.rint(self.alpha*255).astype(np.uint8)])).save(result_path)
        record = dict(id='saved', source=assets.fingerprint(self.source), result=assets.fingerprint(result_path), parameters=normalize(dict(fill_holes=False)))
        with patch.object(self.catalogue, 'lookup', return_value=record): bound = assets.apply_saved(self.layer(), self.catalogue)
        self.assertEqual(bound['path'], str(result_path)); self.assertNotEqual(bound['_matting_binding'].get('mode'), 'dynamic')
        self.assertEqual(bound['_matting_binding']['parameters'], record['parameters'])

    def test_legacy_inline_configs_without_flag_remain_inline(self):
        layer = normalize_layers([dict(id='old', kind='image', path=str(self.source), key_mode='green')])[0]
        self.assertFalse(layer['use_saved_matte'])
        self.assertTrue(normalize_layers([dict(id='plain', kind='image', path=str(self.source))])[0]['use_saved_matte'])

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), 'Explicit FFmpeg required')
    def test_design_and_frame_feed_honor_saved_hole_setting_not_outer_setting(self):
        for fill in (False, True):
            self.publish(dict(fill_holes=fill)); layer = self.layer(); layer['key_protect_inside'] = not fill
            bound = assets.apply_saved(layer, self.catalogue)
            base = dict(source=np.dstack([self.rgb, self.native]), rgb=self.rgb, alpha=self.alpha)
            with patch.object(render, 'infer', return_value=base):
                poster = live.poster(bound['_matting_binding'], QtCore.QSize(64, 48))
                feed = live.Feed(live.StreamInput(bound['_matting_binding'], .1, False, 10, os.environ['NODE_FFMPEG']))
                try: data = np.frombuffer(next(feed.frames()), np.uint8).reshape(48, 64, 4)
                finally: feed.close()
            expected = 255 if fill else 0
            self.assertEqual(poster.pixelColor(24, 20).alpha(), expected); self.assertEqual(data[20, 24, 3], expected)

    def test_pool_items_keep_independent_workbench_hole_choices(self):
        second = self.root/'second.png'; pixels = np.dstack([self.rgb, self.native]); pixels[0, 0, 0] += 1
        Image.fromarray(pixels).save(second)
        self.publish(dict(fill_holes=True))
        self.catalogue.publish_dynamic(second, normalize(dict(fill_holes=False)), expected=assets.fingerprint(second))
        layer = self.layer(); layer.update(source='sequence', sequence_items=[dict(id='a', path=str(self.source)), dict(id='b', path=str(second))])
        frozen = assets.snapshot_layers([layer], self.catalogue)
        one = resolve_sources(frozen)[0][0]; frozen[0]['sequence_cursor'] = 1
        two = resolve_sources(frozen)[0][0]
        self.assertTrue(one['_matting_binding']['parameters']['fill_holes'])
        self.assertFalse(two['_matting_binding']['parameters']['fill_holes'])
