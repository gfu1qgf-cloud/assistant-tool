"""Check RGB, not only matte alpha: exported skin/white must match the poster."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source
from app_plugins.builtin.batch_text_video.visual_media import is_animated, keyed_image, normalize_visual
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.engine import render_static_text

FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
class ChromaColorParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def native(self, image, layer):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'colors.png'
            self.assertTrue(image.save(str(path)))
            args, filters = ['-loop', '1', '-i', str(path)], []
            pad, count = add_keyed_source(args, filters, layer, 0, 'keytest', 10, is_animated, duration=.1)
            self.assertEqual(count, 1)
            result = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', *args,
                '-filter_complex', ';'.join(filters), '-map', '[' + pad + ']', '-frames:v', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
            return np.frombuffer(result.stdout, np.uint8).reshape(image.height(), image.width(), 4)

    def patches(self):
        samples = [(220, 190, 115), (245, 235, 195), (128, 128, 128), (70, 100, 230),
            (200, 80, 30), (50, 220, 70), (80, 190, 220), (230, 120, 200)]
        image = QtGui.QImage(160, 20, QtGui.QImage.Format.Format_RGBA8888)
        painter = QtGui.QPainter(image)
        for index, value in enumerate(samples):
            painter.fillRect(index * 20, 0, 20, 20, QtGui.QColor(*value))
        painter.end()
        return image

    def compare(self, image, layer):
        normalize_visual(layer)
        poster = keyed_image(image, layer).convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        expected = np.frombuffer(poster.constBits().asstring(poster.sizeInBytes()), np.uint8).reshape(
            poster.height(), poster.bytesPerLine())[:, :poster.width() * 4].reshape(poster.height(), poster.width(), 4)
        actual = self.native(image, layer)
        # Ignore fully transparent RGB. Alpha-zero pixels do not reach the video.
        visible = (actual[:, :, 3] > 250) & (expected[:, :, 3] > 250)
        self.assertTrue(visible.any())
        delta = abs(actual[:, :, :3].astype(int) - expected[:, :, :3].astype(int))[visible]
        self.assertLessEqual(int(delta.max()), 2, f'RGB max delta={delta.max()}, mean={delta.mean():.2f}')
        return actual, expected

    def test_skin_white_and_color_patches_match_in_normal_and_enhanced_modes(self):
        for mode in ('green', 'blue'):
            for method in ('normal', 'enhanced'):
                with self.subTest(mode=mode, method=method):
                    self.compare(self.patches(), dict(key_mode=mode, key_method=method,
                        key_similarity=.1, key_softness=.3, key_despill=True,
                        key_alpha_low=8.7, key_alpha_high=90.4))

    def test_disabled_despill_and_black_key_do_not_recolor(self):
        for mode, spill in (('green', False), ('black', True)):
            with self.subTest(mode=mode):
                self.compare(self.patches(), dict(key_mode=mode, key_method='enhanced',
                    key_similarity=.01, key_softness=.01, key_despill=spill))

    def test_encoded_mp4_and_playback_preserve_skin_and_ivory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = QtGui.QImage(320, 180, QtGui.QImage.Format.Format_RGBA8888)
            painter = QtGui.QPainter(image)
            samples = [(220, 190, 115), (245, 235, 195), (128, 128, 128), (70, 100, 230),
                       (200, 80, 30), (50, 220, 70), (80, 190, 220), (230, 120, 200)]
            for index, value in enumerate(samples):
                painter.fillRect(index % 4 * 80, index // 4 * 90, 80, 90, QtGui.QColor(*value))
            painter.end()
            image_path = root / 'patches.png'
            self.assertTrue(image.save(str(image_path)))
            source = root / 'source.mp4'
            subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'color=c=gray:s=320x180:r=10:d=0.4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
                check=True, capture_output=True, timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for method in ('normal', 'enhanced'):
                for preview in (False, True):
                    with self.subTest(method=method, playback=preview):
                        state = fresh_state()
                        state['settings'].update(aspect='source', darkness=0, music_enabled=False,
                            ffmpeg_path=FFMPEG, output_dir=str(root), overwrite=True)
                        layer = dict(id='picture', kind='image', source='fixed', path=str(image_path),
                            x=50, y=50, width=100, height=100, key_mode='green', key_method=method,
                            key_despill=True, key_similarity=.1, key_softness=.3)
                        state['settings']['layers'] = normalize_layers([layer])
                        output, _ = render_static_text(dict(path=str(source), name=f'{method}-{preview}',
                            title='', body=''), state, root / 'private', preview=preview)
                        decoded = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error',
                            '-i', str(output), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
                            check=True, capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        frame = np.frombuffer(decoded.stdout, np.uint8).reshape(180, 320, 4)
                        for index in range(4):
                            actual = frame[45, index * 80 + 40, :3].astype(int)
                            self.assertLessEqual(int(abs(actual - samples[index]).max()), 5,
                                f'{method}/{preview} sample {samples[index]} became {actual.tolist()}')
