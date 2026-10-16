"""SDR graphic pixels may not inherit a phone background's HLG/PQ tags."""
import os
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.engine import render_static_text, probe
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.ui import PreviewWorker
from model.MediaColor import sdr_input_filter

FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
class HdrCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def ffmpeg(self, *args):
        result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
            capture_output=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
        return result.stdout

    def test_hdr_background_does_not_tag_sdr_picture_as_hdr(self):
        ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'hlg.mp4'
            self.ffmpeg('-f', 'lavfi', '-i', 'color=c=0x8090a0:s=320x180:r=5:d=0.8',
                '-vf', 'setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc:range=limited',
                '-c:v', 'libx265', '-x265-params', 'pools=1:frame-threads=1:log-level=error',
                '-pix_fmt', 'yuv420p10le', '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67',
                '-colorspace', 'bt2020nc', '-color_range', 'tv', str(source))
            self.assertEqual(probe(source, ffprobe)['video']['color_transfer'], 'arib-std-b67')
            picture = root / 'sdr.png'
            image = QtGui.QImage(320, 180, QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor(245, 235, 195))
            painter = QtGui.QPainter(image)
            painter.fillRect(0, 0, 160, 180, QtGui.QColor(220, 190, 115))
            painter.end()
            self.assertTrue(image.save(str(picture)))
            state = fresh_state()
            state['settings'].update(aspect='source', darkness=0, music_enabled=False,
                ffmpeg_path=FFMPEG, output_dir=str(root), overwrite=True)
            state['settings']['layers'] = normalize_layers([dict(id='picture', kind='image',
                path=str(picture), source='fixed', width=100, height=100, x=50, y=50,
                key_mode='custom', key_color='#00ff00', key_method='normal', key_despill=True,
                key_similarity=.1, key_softness=.3, key_alpha_low=4.7, key_alpha_high=92.4)])
            for preview, seconds, transition in ((False, None, 'none'), (True, None, 'none'),
                                                 (False, 2, 'fade'), (True, 2, 'none')):
                with self.subTest(preview=preview, seconds=seconds, transition=transition):
                    selected = copy.deepcopy(state)
                    if seconds:
                        selected['settings'].update(unvoiced_duration_mode='fixed',
                            unvoiced_duration_seconds=seconds, loop_transition=transition,
                            transition_seconds=.2)
                    output, _ = render_static_text(dict(path=str(source), name=f'output-{preview}-{seconds}-{transition}',
                        title='', body=''), selected, root / 'private', preview=preview)
                    stream = probe(output, ffprobe)['video']
                    for key in ('color_primaries', 'color_transfer', 'color_space'):
                        self.assertEqual(stream.get(key), 'bt709', (key, stream.get(key)))
                    decoded = self.ffmpeg('-i', str(output), '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1')
                    frame = np.frombuffer(decoded, np.uint8).reshape(180, 320, 3)
                    for x, expected in ((80, (220, 190, 115)), (240, (245, 235, 195))):
                        self.assertLessEqual(int(abs(frame[90, x].astype(int) - expected).max()), 5)

            results = []
            worker = PreviewWorker(str(source), FFMPEG, frame_settings={**state['settings'], 'fit': 'contain'})
            worker.ready.connect(results.append)
            worker.run()
            self.assertNotIn('error', results[0])
            self.assertFalse(QtGui.QImage.fromData(results[0]['image']).isNull())

    def test_policy_is_explicit_for_hdr_and_unknown_wide_gamut(self):
        for transfer in ('arib-std-b67', 'smpte2084'):
            result = sdr_input_filter({'color_transfer': transfer, 'color_primaries': 'bt2020',
                'color_space': 'bt2020nc', 'color_range': 'tv'})
            self.assertIn('transfer=linear', result)
            self.assertIn('tonemap=', result)
            self.assertIn('primaries=bt709', result)
        self.assertEqual(sdr_input_filter({'color_transfer': 'bt709', 'color_primaries': 'bt709'}), '')
        self.assertNotIn('tonemap=', sdr_input_filter({'color_transfer': 'bt709', 'color_primaries': 'bt2020'}))
        with self.assertRaisesRegex(ValueError, '传递函数'):
            sdr_input_filter({'color_primaries': 'bt2020', 'color_transfer': 'unknown'})

    def test_pq_and_sdr_wide_gamut_native_filters_execute(self):
        for transfer in ('smpte2084', 'bt709'):
            with self.subTest(transfer=transfer):
                color = sdr_input_filter(dict(color_transfer=transfer, color_primaries='bt2020',
                    color_space='bt2020nc', color_range='tv'))
                data = self.ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=80x60:r=5:d=0.4',
                    '-vf', f'setparams=range=limited:color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc,' + color,
                    '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1')
                self.assertEqual(len(data), 80 * 60 * 3)
