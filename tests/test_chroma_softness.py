"""High edge-softness must not fade the foreground; Qt/native parity."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source, key_color
from app_plugins.builtin.batch_text_video.visual_media import keyed_image, is_animated, normalize_visual

FFMPEG = os.environ.get('NODE_FFMPEG', '')
SOFTNESS = (0, .08, .29, .3, .31, .6, 1)
COLORS = {'black': (75, 75, 75), 'green': (65, 210, 100),
          'blue': (100, 65, 210), 'custom': (100, 80, 180)}


def settings(mode='black', method='normal', softness=.31, **values):
    layer = dict(key_mode=mode, key_color='#ff00ff', key_method=method,
                 key_similarity=.15, key_softness=softness, key_despill=False, **values)
    normalize_visual(layer)
    return layer


def sample(mode='black', alpha=255):
    image = QtGui.QImage(160, 80, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(QtGui.QColor(*key_color(settings(mode))))
    painter = QtGui.QPainter(image)
    painter.setCompositionMode(painter.CompositionMode.CompositionMode_Source)
    painter.fillRect(20, 15, 120, 50, QtGui.QColor(*COLORS[mode], alpha))
    # Near-key transition pixels: should soften while the subject stays solid.
    if mode == 'black':
        painter.fillRect(16, 15, 4, 50, QtGui.QColor(40, 40, 40, alpha))
    painter.end()
    return image


def pixels(image):
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4).copy()


class SoftnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_foreground_stays_opaque_above_point_three_in_both_methods(self):
        for mode in COLORS:
            for method in ('normal', 'enhanced'):
                for softness in SOFTNESS:
                    with self.subTest(mode=mode, method=method, softness=softness):
                        result = keyed_image(sample(mode), settings(mode, method, softness))
                        self.assertEqual(result.pixelColor(80, 40).alpha(), 255)
                        self.assertEqual(result.pixelColor(0, 0).alpha(), 0)

    def test_softness_still_softens_near_key_transition_and_is_monotonic(self):
        alphas = []
        for softness in SOFTNESS:
            result = keyed_image(sample(), settings(softness=softness))
            alphas.append(result.pixelColor(18, 30).alpha())
            self.assertEqual(result.pixelColor(80, 40).alpha(), 255)
        self.assertEqual(alphas[0], 255)
        self.assertTrue(all(right <= left for left, right in zip(alphas, alphas[1:])), alphas)
        self.assertTrue(0 < alphas[-1] < alphas[1] < 255, alphas)

    def test_source_transparency_and_rgb_are_preserved_at_maximum_softness(self):
        for method in ('normal', 'enhanced'):
            with self.subTest(method=method):
                image = sample('black', alpha=128)
                image.setPixelColor(5, 5, QtGui.QColor(180, 100, 80, 0))
                before = pixels(image)
                result = keyed_image(image, settings(method=method, softness=1))
                self.assertEqual(result.pixelColor(80, 40).alpha(), 128)
                self.assertEqual(result.pixelColor(5, 5).alpha(), 0)
                np.testing.assert_array_equal(pixels(result)[:, :, :3], before[:, :, :3])
                np.testing.assert_array_equal(pixels(image), before)

    def test_alpha_levels_still_adjust_the_transition(self):
        image = sample()
        weak = keyed_image(image, settings(softness=1))
        recovered = keyed_image(image, settings(softness=1, key_alpha_high=5))
        removed = keyed_image(image, settings(softness=1, key_alpha_low=10))
        self.assertTrue(0 < weak.pixelColor(18, 30).alpha() < 255)
        self.assertEqual(recovered.pixelColor(18, 30).alpha(), 255)
        self.assertEqual(removed.pixelColor(18, 30).alpha(), 0)
        for result in (weak, recovered, removed):
            self.assertEqual(result.pixelColor(80, 40).alpha(), 255)

    def test_normalization_and_render_do_not_rewrite_saved_softness(self):
        for softness in SOFTNESS:
            layer = settings(softness=softness)
            before = copy.deepcopy(layer)
            keyed_image(sample(), layer)
            self.assertEqual(layer, before)
            self.assertEqual(layer['key_softness'], softness)


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'requires explicit test FFmpeg')
class NativeSoftnessTests(SoftnessTests):
    def native(self, image, layer):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'sample.png'
            self.assertTrue(image.save(str(source)))
            args, filters = ['-loop', '1', '-i', str(source)], []
            output, count = add_keyed_source(args, filters, layer, 0, 'softness', 10,
                                             is_animated, duration=.1)
            self.assertEqual(count, 1)
            result = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', *args,
                '-filter_complex', ';'.join(filters), '-map', '['+output+']', '-frames:v', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            return np.frombuffer(result.stdout, np.uint8).reshape(image.height(), image.width(), 4)

    def test_preview_and_native_alpha_agree_across_the_full_softness_range(self):
        for mode in COLORS:
            for method in ('normal', 'enhanced'):
                for softness in SOFTNESS:
                    with self.subTest(mode=mode, method=method, softness=softness):
                        image, layer = sample(mode), settings(mode, method, softness)
                        native = self.native(image, layer)
                        poster = pixels(keyed_image(image, layer))
                        self.assertEqual(int(native[40, 80, 3]), 255)
                        self.assertEqual(int(native[0, 0, 3]), 0)
                        self.assertLessEqual(int(abs(native.astype(int)-poster.astype(int)).max()), 2)

    def test_native_source_transparency_and_levels_at_maximum_softness(self):
        for method in ('normal', 'enhanced'):
            for low, high in ((0, 100), (0, 5), (10, 100)):
                with self.subTest(method=method, low=low, high=high):
                    image = sample(alpha=128)
                    image.setPixelColor(5, 5, QtGui.QColor(180, 100, 80, 0))
                    layer = settings(method=method, softness=1, key_alpha_low=low, key_alpha_high=high)
                    actual = self.native(image, layer)
                    expected = pixels(keyed_image(image, layer))
                    self.assertAlmostEqual(int(actual[40, 80, 3]), 128, delta=1)
                    self.assertEqual(int(actual[5, 5, 3]), 0)
                    self.assertLessEqual(int(abs(actual.astype(int)-expected.astype(int)).max()), 2)
