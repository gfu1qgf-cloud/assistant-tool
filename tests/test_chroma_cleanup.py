"""Reconstruction removes floating fragments while preserving attached detail."""
import os
from pathlib import Path
import tempfile
import unittest
import numpy as np
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.chroma_cleanup import clean_alpha
from app_plugins.builtin.batch_text_video.visual_media import normalize_visual, keyed_image, VisualProperties
import test_chroma_edge_despill as helper

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def matte():
    alpha = np.zeros((100, 160), np.uint8)
    alpha[30:90, 40:90] = 255
    alpha[48:60, 52:66] = 0  # keep a real hole
    alpha[44:80, 115:142] = 160  # second, disconnected subject
    alpha[18:31, 55] = 96  # attached fine hair
    for step in range(21): alpha[30-step, 40-step] = 255  # diagonal attachment
    alpha[12:15, 95:98] = 255  # detached fragment
    alpha[22:24, 7:9] = 128
    alpha[20:32, 150] = 255  # long detached thin hair
    alpha[0:2, 0:2] = 255   # border fragment
    return alpha


class ChromaCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def layer(self, **values):
        layer = dict(key_mode='green', key_similarity=.01, key_softness=0, key_cleanup=True)
        layer.update(values); normalize_visual(layer)
        return layer

    def image(self, alpha):
        return helper.picture(np.dstack([np.full((*alpha.shape, 3), (200, 100, 50), np.uint8), alpha]))

    def test_default_off_validation_and_off_is_pixel_identical(self):
        old = {}; normalize_visual(old); self.assertFalse(old['key_cleanup'])
        alpha = matte()
        self.assertIs(clean_alpha(alpha, old), alpha)
        for key, values in {'key_cleanup': [1, None, 'on'], 'key_cleanup_size': [0, 13, True, 1.5]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError): self.layer(**{key: value})

    def test_islands_removed_multiple_subjects_holes_and_attached_hair_preserved(self):
        alpha = matte(); actual = clean_alpha(alpha, self.layer())
        self.assertEqual(actual[13, 96], 0)
        self.assertEqual(actual[22, 7], 0)
        self.assertEqual(actual[25, 150], 0)
        self.assertEqual(actual[0, 0], 0)
        np.testing.assert_array_equal(actual[30:90, 40:90], alpha[30:90, 40:90])
        np.testing.assert_array_equal(actual[44:80, 115:142], alpha[44:80, 115:142])
        np.testing.assert_array_equal(actual[18:31, 55], alpha[18:31, 55])
        for step in range(21): self.assertEqual(actual[30-step, 40-step], 255)
        self.assertTrue(np.all(actual <= alpha))

    def test_size_controls_which_components_survive_and_white_protects_small_prop(self):
        alpha = matte()
        low = clean_alpha(alpha, self.layer(key_cleanup_size=1))
        high = clean_alpha(alpha, self.layer(key_cleanup_size=2))
        self.assertEqual(low[13, 96], 255); self.assertEqual(high[13, 96], 0)
        protected = np.zeros_like(alpha); protected[13, 96] = 255
        result = clean_alpha(alpha, self.layer(), protected)
        np.testing.assert_array_equal(result[12:15, 95:98], alpha[12:15, 95:98])
        self.assertEqual(result[22, 7], 0)

    def test_preview_cleanup_changes_only_alpha_and_disabled_key_is_unchanged(self):
        image = self.image(matte()); source = helper.pixels(image)
        clean = helper.pixels(keyed_image(image, self.layer()))
        np.testing.assert_array_equal(clean[:, :, :3], source[:, :, :3])
        self.assertEqual(clean[13, 96, 3], 0)
        self.assertEqual(clean[20, 55, 3], 96)
        self.assertIs(keyed_image(image, self.layer(key_mode='none')), image)

    def test_inspector_load_does_not_emit_and_switch_controls_size(self):
        panel = VisualProperties(); self.addCleanup(panel.deleteLater)
        changes = []; panel.changed.connect(changes.append)
        panel.set_layer(dict(self.layer(key_cleanup=False), id='cleanup'))
        self.assertFalse(changes); self.assertFalse(panel.controls['key_cleanup_size'].isEnabled())
        panel.controls['key_cleanup'].setChecked(True)
        panel.controls['key_cleanup_size'].setValue(4)
        self.assertTrue(changes[-1]['key_cleanup']); self.assertEqual(changes[-1]['key_cleanup_size'], 4)
        panel.set_layer(dict(self.layer(key_mode='none'), id='off'))
        self.assertFalse(panel.controls['key_cleanup'].isEnabled())

    def test_cleanup_settings_survive_store_and_component_conversion(self):
        from app_plugins.builtin.batch_text_video.store import fresh_state, Store, DEFAULTS
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'subject.png'; self.image(matte()).save(str(path))
            layer = normalize_layers([{**self.layer(key_cleanup_size=4), 'id': 'cleanup',
                                       'kind': 'image', 'path': str(path)}])[0]
            state = fresh_state(); state['settings']['layers'] = [layer]
            store = Store(Path(folder)/'private'); store.save(state, 0)
            loaded = store.load()['settings']['layers'][0]
            self.assertTrue(loaded['key_cleanup']); self.assertEqual(loaded['key_cleanup_size'], 4)
            graph, _ = component_graph(loaded, DEFAULTS, fallback_registry())
            node = next(node for node in graph['nodes'] if node['kind'] == 'batch_text_video.visual_layer')
            self.assertTrue(node['params']['layer']['key_cleanup'])
            self.assertEqual(node['params']['layer']['key_cleanup_size'], 4)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_native_matches_reconstruction_and_keeps_retained_alpha_exactly(self):
        alpha = matte(); image = self.image(alpha)
        native = helper.EdgeDespillTests().native
        for method in ('normal', 'enhanced'):
            for mode, despill in (('basic', False), ('basic', True), ('edge', True)):
                with self.subTest(method=method, mode=mode, despill=despill):
                    layer = self.layer(key_method=method, key_despill_mode=mode, key_despill=despill)
                    original = native(image, {**layer, 'key_cleanup': False})
                    actual = native(image, layer)
                    expected = clean_alpha(original[:, :, 3], layer)
                    np.testing.assert_array_equal(actual[:, :, 3], expected)
                    preview = helper.pixels(keyed_image(image, layer))
                    self.assertLessEqual(int(abs(actual.astype(int)-preview.astype(int)).max()), 2)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_native_trimap_preserves_small_fragment_and_never_revives_source_alpha(self):
        native = helper.EdgeDespillTests().native
        alpha = matte(); alpha[16, 96] = 0; image = self.image(alpha)
        with tempfile.TemporaryDirectory() as folder:
            trimap = np.full((100, 160, 4), (128, 128, 128, 255), np.uint8)
            trimap[11:17, 94:99] = (255, 255, 255, 255)
            path = Path(folder)/'keep.png'; helper.picture(trimap).save(str(path))
            for mode, despill in (('basic', False), ('basic', True), ('edge', True)):
                layer = self.layer(key_method='enhanced', key_mask_path=str(path), key_despill=despill, key_despill_mode=mode)
                actual = native(image, layer)
                self.assertEqual(actual[13, 96, 3], 255)
                self.assertEqual(actual[16, 96, 3], 0)
                self.assertEqual(actual[22, 7, 3], 0)
                preview = helper.pixels(keyed_image(image, layer))
                self.assertLessEqual(int(abs(actual.astype(int)-preview.astype(int)).max()), 2)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_subpixel_key_alpha_bridge_has_same_connectivity_in_poster_and_native(self):
        image = np.full((25, 40, 4), (0, 255, 0, 255), np.uint8)
        image[5:20, 4:20] = (200, 80, 30, 255)
        image[10:13, 21:24] = (200, 80, 30, 255)
        image[11, 20] = (20, 230, 10, 255)
        distance = float(np.sqrt((20**2+25**2+10**2)/(255**2*3)))
        for method in ('normal', 'enhanced'):
            layer = self.layer(key_method=method, key_similarity=distance-.1*.6/255, key_softness=1)
            actual = helper.EdgeDespillTests().native(helper.picture(image), layer)
            preview = helper.pixels(keyed_image(helper.picture(image), layer))
            self.assertEqual(actual[11, 22, 3], 0)
            np.testing.assert_array_equal(actual[:, :, 3], preview[:, :, 3])

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_random_components_border_and_empty_frames_match_native(self):
        random = np.random.default_rng(243)
        cases = [np.zeros((1, 1), np.uint8), np.full((3, 5), 160, np.uint8)]
        for _ in range(3):
            alpha = (random.random((50, 80)) > .75).astype(np.uint8)*160
            alpha[10:30, 20:45] = 255; cases.append(alpha)
        for alpha in cases:
            for size in (1, 3, 12):
                with self.subTest(shape=alpha.shape, size=size):
                    layer = self.layer(key_cleanup_size=size)
                    actual = helper.EdgeDespillTests().native(self.image(alpha), layer)
                    expected = clean_alpha(alpha, layer)
                    self.assertLessEqual(int(abs(actual[:, :, 3].astype(int)-expected.astype(int)).max()), 1)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_real_green_key_moving_video_and_gif_remove_fragments_on_each_frame(self):
        from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source
        from app_plugins.builtin.batch_text_video.visual_media import is_animated
        processor = helper.EdgeDespillTests()
        alpha = matte()
        source = np.full((100, 160, 4), (0, 255, 0, 255), np.uint8)
        source[alpha > 0] = (60, 50, 40, 255)
        frames = [source, np.roll(source, 5, axis=1)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, data in enumerate(frames): helper.picture(data).save(str(root/f'frame{index}.png'))
            for suffix, codec in (('mov', ['-c:v', 'png', '-pix_fmt', 'rgba']), ('gif', [])):
                path = root/('moving.'+suffix)
                processor.ffmpeg(['-framerate', '10', '-i', str(root/'frame%d.png'), '-frames:v', '2', *codec, str(path)])
                filtered = []
                for enabled in (False, True):
                    args, filters = ['-i', str(path)], []
                    layer = self.layer(key_cleanup=enabled)
                    pad, _ = add_keyed_source(args, filters, layer, 0, 'moving', 10, is_animated, duration=.2)
                    raw = processor.ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '['+pad+']',
                        '-frames:v', '2', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'])
                    filtered.append(np.frombuffer(raw, np.uint8).reshape(2, 100, 160, 4))
                for index in range(2):
                    expected = clean_alpha(filtered[0][index, :, :, 3], self.layer())
                    np.testing.assert_array_equal(filtered[1][index, :, :, 3], expected)
                    self.assertEqual(filtered[1][index, 13, 96+index*5, 3], 0)
                    self.assertEqual(filtered[1][index, 20, 55+index*5, 3], 255)
