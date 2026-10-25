import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import numpy as np
from PIL import Image
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers, compose_layers
from app_plugins.builtin.batch_text_video.visual_media import add_plane
from app_plugins.builtin.batch_text_video.overlay_timing import resolve_window, DEFAULTS


class AnimationClippingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Explicit real FFmpeg required')
    def test_falling_subject_keeps_feet_until_they_reach_the_canvas_edge(self):
        ffmpeg = os.environ['NODE_FFMPEG']
        with tempfile.TemporaryDirectory() as root:
            pixels = np.full((160, 80, 4), [255, 0, 0, 255], np.uint8)
            pixels[50:120, :, :3] = [0, 255, 0]; pixels[120:, :, :3] = [0, 0, 255]
            path = Path(root)/'person.png'; Image.fromarray(pixels).save(path)
            window = resolve_window({**DEFAULTS, 'enter': 'up', 'enter_duration': 1, 'distance': 80}, 2)
            layer = normalize_layers([dict(id='person', kind='image', path=str(path), x=50, y=90, width=50, height=80)])[0]
            args = [ffmpeg, '-y', '-v', 'error', '-filter_complex_threads', '1', '-f', 'lavfi', '-i', 'color=c=black:s=160x240:r=10:d=2']
            filters = ['[0:v]format=rgba[base]']
            add_plane(args, filters, layer, 1, 'base', 'result', 160, 240, 2, 10, timing=window)
            args += ['-filter_complex', ';'.join(filters), '-map', '[result]', '-t', '2', '-an', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1']
            result = subprocess.run(args, capture_output=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, 240, 160, 4)
            blue = lambda frame: (frame[:, :, 2] > 200) & (frame[:, :, 0] < 30) & (frame[:, :, 1] < 30)
            self.assertGreater(blue(frames[2]).sum(), 2000, 'Feet must exist while falling through the middle')
            self.assertEqual(blue(frames[12]).sum(), 0, 'Feet are outside the canvas only after landing')
            layer.update(visual_timing_enabled=True, visual_timing={**DEFAULTS, 'enter': 'up', 'enter_duration': 1, 'distance': 80})
            preview = compose_layers({}, [layer], 160, 240, 0, editor_timing=dict(duration=2, time=.2))
            expected = sum(preview.pixelColor(x, y).blue() > 200 and preview.pixelColor(x, y).red() < 30
                for y in range(240) for x in range(160))
            self.assertAlmostEqual(blue(frames[2]).sum(), expected, delta=200)
