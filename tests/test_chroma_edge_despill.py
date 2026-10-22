"""Edge spill reduction, core/protection safety and real native RGB parity."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.visual_media import keyed_image, normalize_visual, VisualProperties, is_animated
from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source
from app_plugins.builtin.batch_text_video.chroma_despill import DEFAULTS as DESPILL, edge_mask

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def pixels(image):
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4).copy()


def picture(data):
    h, w = data.shape[:2]
    return QtGui.QImage(data.tobytes(), w, h, w*4, QtGui.QImage.Format.Format_RGBA8888).copy()


def sample(blue=False):
    data = np.full((80, 120, 4), (0, 255, 0, 255), np.uint8)
    data[12:68, 12:108] = (100, 185, 40, 255)  # yellow-green opaque fringe
    data[16:64, 16:104] = (180, 170, 100, 255)  # warm subject
    data[28:52, 36:60] = (80, 150, 60, 255)   # legitimate green in the core
    data[22:58, 80:90] = (0, 255, 0, 255)    # enclosed green prop
    data[30:50, 10:12] = (20, 225, 10, 255)  # semi-transparent keyed strand
    if blue:
        data[:, :, [1, 2]] = data[:, :, [2, 1]]
    return picture(data)


class EdgeDespillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def layer(self, **extra):
        layer = dict(key_mode='green', key_method='normal', key_similarity=.05, key_softness=.6,
                     key_despill=True, key_despill_mode='edge')
        layer.update(extra)
        normalize_visual(layer)
        return layer

    def test_old_projects_keep_basic_and_invalid_values_are_rejected(self):
        old = dict(key_mode='green', key_despill=True)
        normalize_visual(old)
        self.assertEqual(old['key_despill_mode'], 'basic')
        for field, values in {'key_despill_mode': ['unknown'], 'key_despill_width': [0, 13, True, 2.5],
                              'key_despill_amount': [-1, 101, True, float('nan')],
                              'key_despill_luma': [-1, 101, float('inf')]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.layer(**{field: value})

    def test_reduces_fringe_more_than_basic_and_keeps_core_rgb_and_alpha(self):
        image = sample()
        layer = self.layer()
        clean = pixels(keyed_image(image, layer))
        base = pixels(keyed_image(image, {**layer, 'key_despill_mode': 'basic'}))
        original = pixels(keyed_image(image, {**layer, 'key_despill': False}))
        excess = lambda rgb: float(rgb[1])-(float(rgb[0])+float(rgb[2]))/2
        self.assertLess(excess(clean[35, 12, :3]), excess(base[35, 12, :3])*.45)
        np.testing.assert_array_equal(clean[35:45, 42:50], original[35:45, 42:50])
        np.testing.assert_array_equal(clean[:, :, 3], original[:, :, 3])

    def test_strength_zero_identity_and_luma_compensation_avoids_dark_edge(self):
        image = sample()
        layer = self.layer()
        original = pixels(keyed_image(image, {**layer, 'key_despill': False}))
        zero = pixels(keyed_image(image, {**layer, 'key_despill_amount': 0}))
        np.testing.assert_array_equal(original, zero)
        dark = pixels(keyed_image(image, {**layer, 'key_despill_luma': 0}))[35, 12, :3]
        light = pixels(keyed_image(image, {**layer, 'key_despill_luma': 100}))[35, 12, :3]
        weights = np.array([.299, .587, .114])
        self.assertGreater(light@weights, dark@weights + 20)
        self.assertLess(abs(light@weights-original[35, 12, :3]@weights), 2)

    def test_soft_edge_band_and_width_control_do_not_reach_deep_core(self):
        alpha = np.zeros((60, 100), np.uint8); alpha[10:50, 10:90] = 255
        narrow = edge_mask(alpha, self.layer(key_despill_width=1))
        wide = edge_mask(alpha, self.layer(key_despill_width=8))
        self.assertEqual(narrow[30, 16], 0)
        self.assertGreater(wide[30, 16], 0)
        self.assertEqual(wide[30, 50], 0)
        self.assertTrue(np.any((wide > 0) & (wide < 255)))

    def test_ui_save_reload_and_component_node_keep_all_controls(self):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.store import Store, fresh_state, DEFAULTS
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'sample.png'; sample().save(str(path))
            layer = normalize_layers([{**self.layer(), 'id': 'edge', 'kind': 'image', 'path': str(path)}])[0]
            before = copy.deepcopy(layer)
            panel = VisualProperties(); self.addCleanup(panel.deleteLater)
            changed = []; panel.changed.connect(changed.append); panel.set_layer(layer)
            self.assertFalse(changed)
            self.assertTrue(panel.controls['key_despill_width'].isEnabled())
            panel.controls['key_despill_width'].setValue(7)
            panel.controls['key_despill_luma'].setValue(65)
            updated = changed[-1]
            state = fresh_state(); state['settings']['layers'] = [updated]
            store = Store(Path(folder)/'private'); store.save(state, 0)
            loaded = store.load()['settings']['layers'][0]
            graph, _ = component_graph(loaded, DEFAULTS, fallback_registry())
            node = next(n for n in graph['nodes'] if n['kind'] == 'batch_text_video.visual_layer')
            for key in DESPILL:
                self.assertEqual(loaded[key], updated[key])
                self.assertEqual(node['params']['layer'][key], updated[key])
            self.assertEqual(layer, before)
            panel.controls['key_despill'].setChecked(False)
            self.assertFalse(panel.controls['key_despill_width'].isEnabled())
            panel.set_layer({**layer, 'key_mode': 'black'})
            self.assertFalse(panel.controls['key_despill_mode'].isEnabled())

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_native_normal_enhanced_green_blue_custom_match_and_preserve_alpha(self):
        for mode in ('green', 'blue', 'custom'):
            image = sample(blue=mode == 'blue')
            for method in ('normal', 'enhanced'):
                with self.subTest(mode=mode, method=method):
                    layer = self.layer(key_mode=mode, key_method=method)
                    self.compare(image, layer)
                    unclean = self.native(image, {**layer, 'key_despill': False})
                    clean = self.native(image, layer)
                    np.testing.assert_array_equal(clean[:, :, 3], unclean[:, :, 3])
                    if method == 'enhanced':
                        np.testing.assert_array_equal(clean[30, 80, :3], pixels(image)[30, 80, :3])

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_native_soft_native_alpha_levels_trimap_and_zero_strength(self):
        image = pixels(sample()); image[25:40, 12:22, 3] = 128; image[:4, :, 3] = 0
        image = picture(image)
        for amount in (0, 35.5, 100):
            layer = self.layer(key_despill_amount=amount, key_alpha_low=4, key_alpha_high=94,
                               key_despill_width=2, key_despill_luma=100)
            self.compare(image, layer)
        with tempfile.TemporaryDirectory() as folder:
            trimap = np.full((80, 120, 4), (128, 128, 128, 255), np.uint8)
            trimap[22:48, 8:18] = (255, 255, 255, 255)
            trimap[48:60, 12:22] = (0, 0, 0, 255)
            path = Path(folder)/'mask.png'; picture(trimap).save(str(path))
            layer = self.layer(key_method='enhanced', key_mask_path=str(path))
            actual = self.compare(image, layer)
            np.testing.assert_array_equal(actual[42, 12, :3], pixels(image)[42, 12, :3])
            self.assertEqual(actual[52, 16, 3], 0)

    def compare(self, image, layer):
        expected = pixels(keyed_image(image, layer))
        actual = self.native(image, layer)
        visible = (expected[:, :, 3] > 10) & (actual[:, :, 3] > 10)
        delta = abs(expected.astype(int)-actual.astype(int))
        self.assertLessEqual(int(delta[:, :, 3].max()), 2)
        self.assertLessEqual(int(delta[:, :, :3][visible].max()), 3)
        return actual

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_tiny_border_and_wide_masks_match_without_new_transparency(self):
        for shape, radius in (((1, 1), 12), ((3, 5), 4), ((80, 120), 12)):
            with self.subTest(shape=shape, radius=radius):
                image = pixels(sample()) if shape == (80, 120) else np.full((*shape, 4), (80, 160, 40, 128), np.uint8)
                self.compare(picture(image), self.layer(key_despill_width=radius))

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_video_and_gif_recompute_edge_mask_on_every_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            frames = [pixels(sample()), np.roll(pixels(sample()), 9, axis=1)]
            for index, data in enumerate(frames):
                self.assertTrue(picture(data).save(str(root/f'frame{index}.png')))
            for suffix, codec in (('mov', ['-c:v', 'png', '-pix_fmt', 'rgba']), ('gif', [])):
                path = root/('moving.'+suffix)
                self.ffmpeg(['-framerate', '10', '-i', str(root/'frame%d.png'), '-frames:v', '2', *codec, str(path)])
                raw = self.ffmpeg(['-i', str(path), '-vf', 'fps=10', '-frames:v', '2', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'])
                decoded = np.frombuffer(raw, np.uint8).reshape(2, 80, 120, 4)
                layer = self.layer(key_method='enhanced')
                filters = []; args = ['-i', str(path)]
                pad, _ = add_keyed_source(args, filters, layer, 0, 'moving', 10, is_animated, duration=.2)
                raw = self.ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '['+pad+']',
                                  '-frames:v', '2', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'])
                actual = np.frombuffer(raw, np.uint8).reshape(2, 80, 120, 4)
                for index in range(2):
                    expected = pixels(keyed_image(picture(decoded[index]), layer))
                    visible = (expected[:, :, 3] > 10) & (actual[index, :, :, 3] > 10)
                    delta = abs(expected.astype(int)-actual[index].astype(int))
                    self.assertLessEqual(int(delta[visible].max()), 3,
                        f'{suffix}/{index}: max at {np.unravel_index(delta.argmax(), delta.shape)}, '
                        f'input {decoded[index, 35, 12].tolist()}, expected {expected[35, 12].tolist()}, actual {actual[index, 35, 12].tolist()}')
                self.assertFalse(np.array_equal(actual[0], actual[1]))

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_encoded_export_playback_and_converted_node_share_edge_correction(self):
        import hashlib
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.store import fresh_state, DEFAULTS
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry, new_effect, prepare_bundle, EffectRuntime
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        from model.node_workflow.library import WorkflowLibrary
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/'base.mp4'; overlay = root/'source.png'
            sample().save(str(overlay))
            self.ffmpeg(['-f', 'lavfi', '-i', 'color=c=gray:s=240x160:r=10:d=0.4',
                         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)])
            checksums = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, overlay)}
            layer = normalize_layers([{**self.layer(key_method='enhanced'), 'id': 'edge', 'kind': 'image',
                'path': str(overlay), 'source': 'fixed', 'width': 100, 'height': 100, 'x': 50, 'y': 50}])[0]
            registry = fallback_registry(); graph, _ = component_graph(layer, DEFAULTS, registry)
            library = WorkflowLibrary(root/'workflows'); stored = library.save(graph, '边缘去色溢', registry)
            effect = new_effect(); effect['workflow_id'] = stored['workflow_id']
            services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
            outputs = []
            for variant in ('export', 'playback', 'node', 'basic'):
                state = fresh_state()
                selected = effect if variant == 'node' else dict(layer, key_despill_mode='basic' if variant == 'basic' else 'edge')
                state['settings'].update(aspect='source', darkness=0, music_enabled=False, use_voice=False,
                    ffmpeg_path=FFMPEG, output_dir=str(root/'out'), layers=[selected])
                job = dict(id='job', path=str(source), name=variant, title='', body='', task_dir='')
                state['jobs'] = [job]; runtime = None
                if variant == 'node':
                    bundle, registry, _ = prepare_bundle(state, ['job'], services)
                    runtime = EffectRuntime(bundle, registry)
                output, _ = render_static_text(job, state, root/'private', node_effects=runtime, preview=variant == 'playback')
                raw = self.ffmpeg(['-i', str(output), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
                outputs.append(np.frombuffer(raw, np.uint8).reshape(160, 240, 3))
            for other in outputs[1:3]:
                self.assertLess(float(abs(outputs[0].astype(float)-other).mean()), 1.5)
            # The improved edge must survive real YUV420 encoding as well.
            excess = lambda p: float(p[1])-(float(p[0])+float(p[2]))/2
            self.assertLess(excess(outputs[0][70, 25]), excess(outputs[3][70, 25])-8)
            self.assertEqual(checksums, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in checksums})

    def ffmpeg(self, args):
        result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
            capture_output=True, timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
        return result.stdout

    def native(self, image, layer):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'input.png'; self.assertTrue(image.save(str(path)))
            args, filters = ['-loop', '1', '-i', str(path)], []
            pad, _ = add_keyed_source(args, filters, layer, 0, 'edge', 10, is_animated, duration=.1)
            result = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', *args,
                '-filter_complex', ';'.join(filters), '-map', '['+pad+']', '-frames:v', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
            return np.frombuffer(result.stdout, np.uint8).reshape(image.height(), image.width(), 4)
