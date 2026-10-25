import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.matting_settings import refine, normalize
from app_plugins.builtin.batch_text_video import matting_assets as assets
from app_plugins.builtin.batch_text_video import matting_live as live
from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
from app_plugins.builtin.batch_text_video.layers import normalize_layers


class AiRefinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def arrays(self):
        rgb = np.full((48, 64, 3), 160, np.uint8)
        alpha = np.zeros((48, 64), np.float32); alpha[5:43, 5:59] = .95
        alpha[18:29, 22:38] = 0
        native = np.full((48, 64), 255, np.uint8); native[21, 25] = 0
        return rgb, alpha, native

    def test_fill_uses_ai_confidence_and_preserves_native_alpha_and_remove_brush(self):
        rgb, alpha, native = self.arrays()
        before = refine(rgb, alpha, native, {})
        after = refine(rgb, alpha, native, dict(fill_holes=True))
        self.assertEqual(before[20, 24, 3], 0); self.assertEqual(after[20, 24, 3], 255)
        self.assertEqual(after[21, 25, 3], 0); self.assertEqual(after[0, 0, 3], 0)
        painted = refine(rgb, alpha, native, dict(fill_holes=True, strokes=[dict(mode='remove', radius=.04, points=[[.5, .5]])]))
        self.assertEqual(painted[24, 32, 3], 0)

    def test_open_background_channels_are_not_mistaken_for_enclosed_holes(self):
        rgb, alpha, native = self.arrays(); alpha[:23, 28:32] = 0
        self.assertEqual(refine(rgb, alpha, native, dict(fill_holes=True))[20, 29, 3], 0)

    def test_hole_fill_restores_original_subject_color_instead_of_black_foreground_estimate(self):
        rgb, alpha, native = self.arrays()
        result = refine(np.zeros_like(rgb), alpha, native, dict(fill_holes=True), source_rgb=rgb)
        np.testing.assert_array_equal(result[20, 24, :3], rgb[20, 24])
        self.assertEqual(result[20, 24, 3], 255)

    def test_inline_ai_option_and_shared_refinement_controls_are_available(self):
        panel = VisualProperties()
        panel.set_layer(normalize_layers([dict(id='p', kind='image', path='source.png')])[0])
        panel.controls['key_method'].setCurrentIndex(panel.controls['key_method'].findData('ai'))
        self.assertEqual(panel.layer['key_method'], 'ai')
        self.assertTrue(panel.controls['key_protect_inside'].isEnabled())
        self.assertTrue(panel.controls['key_cleanup'].isEnabled())
        self.assertTrue(panel.matte_levels.isEnabled())
        self.assertFalse(panel.controls['key_similarity'].isEnabled())
        panel.deleteLater()

    def test_inline_ai_poster_and_stream_use_same_fill_cleanup_and_non_cumulative_levels(self):
        rgb, alpha, native = self.arrays()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/'person.png'; Image.fromarray(np.dstack([rgb, native])).save(source)
            layer = normalize_layers([dict(id='p', kind='image', path=str(source), key_method='ai', key_cleanup=True,
                key_cleanup_size=1, key_alpha_low=10, key_alpha_high=90)])[0]
            with patch.object(assets.Catalogue, 'lookup', return_value=None): bound = assets.apply_saved(layer)
            p = bound['_matting_binding']['parameters']
            self.assertTrue(p['fill_holes']); self.assertTrue(p['cleanup'])
            again = assets.apply_saved(bound)
            self.assertEqual(again['_matting_binding']['parameters'], p)
            base = dict(rgb=rgb, alpha=alpha, source=np.dstack([rgb, native]))
            with patch('app_plugins.builtin.batch_text_video.matting_render.infer', return_value=base) as infer:
                poster = live.poster(bound['_matting_binding'], QtCore.QSize(64, 48))
                alternate = copy.deepcopy(bound['_matting_binding']); alternate['parameters']['fill_holes'] = False
                live.poster(alternate, QtCore.QSize(64, 48))
                self.assertEqual(infer.call_count, 1, 'Changing refinement reuses the raw AI mask')
                spec = live.StreamInput(bound['_matting_binding'], .1, True, 1, os.environ.get('NODE_FFMPEG', ''))
                feed = live.Feed(spec)
                frames = list(feed.frames()); feed.close()
            raw = np.frombuffer(frames[0], np.uint8).reshape(48, 64, 4)
            self.assertEqual(raw[20, 24, 3], 255); self.assertEqual(raw[21, 25, 3], 0)
            self.assertEqual(poster.pixelColor(24, 20).alpha(), 255)
            self.assertEqual(poster.pixelColor(25, 21).alpha(), 0)

    def test_saved_static_ai_result_can_use_enhanced_refinement_without_color_rekey(self):
        rgb, alpha, native = self.arrays()
        with tempfile.TemporaryDirectory() as root:
            source, result = Path(root)/'source.png', Path(root)/'result.png'
            Image.fromarray(np.dstack([rgb, native])).save(source)
            Image.fromarray(refine(rgb, alpha, native, {})).save(result)
            record = dict(id='saved', source=assets.fingerprint(source), result=assets.fingerprint(result), parameters=normalize())
            before = copy.deepcopy(record)
            layer = normalize_layers([dict(id='p', kind='image', path=str(source), key_method='enhanced', key_mode='green')])[0]
            class Catalogue:
                def lookup(self, _): return record
            bound = assets.apply_saved(layer, Catalogue())
            self.assertEqual(bound['_matting_binding']['mode'], 'dynamic')
            self.assertTrue(bound['_matting_binding']['parameters']['fill_holes'])
            self.assertEqual(bound['key_mode'], 'none'); self.assertEqual(record, before)
