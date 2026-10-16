import copy
import gc
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.playback import PlaybackWorker, preview_key
from app_plugins.builtin.batch_text_video.engine import render_static_text, probe
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.engine import audio_graph

PREFIX = 'app_plugins.builtin.batch_text_video.'
FFMPEG = os.environ.get('NODE_FFMPEG', '')


class QuickPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        gc.collect()

    def test_cache_distinguishes_full_and_quick_without_mutating_settings(self):
        state = fresh_state()
        job = {'path': '', 'title': '', 'body': ''}
        before = copy.deepcopy(state)
        self.assertNotEqual(preview_key(job, state), preview_key(job, state, preview_limit=10))
        self.assertEqual(preview_key(job, state, preview_limit=10), preview_key(job, state, preview_limit=10))
        self.assertEqual(state, before)

    def test_button_is_visible_and_requests_ten_second_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.mp4'
            source.touch()
            with patch.object(BatchTextVideoDialog, 'request_background'), patch(PREFIX + 'playback.PlaybackWorker.start'):
                dialog = BatchTextVideoDialog(store=Store(folder))
                dialog.active_entry = lambda: {'path': str(source), 'name': 'test', 'title': '', 'body': ''}
                self.assertIn('10秒', dialog.quick_preview_button.text())
                dialog.quick_preview_button.click()
                worker = dialog.playback_worker
                self.assertIsNotNone(worker)
                self.assertEqual(worker.preview_limit, 10)
                self.assertEqual(dialog.quick_preview_button.text(), '取消准备')
                dialog.quick_preview_button.click()
                self.assertTrue(worker.cancel.is_set())
                dialog._playback_finished()
                self.assertIn('10秒', dialog.quick_preview_button.text())
                dialog.close()

    def test_worker_limits_only_proxy_does_not_save_or_advance_pools(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp4'
            source.touch()
            state = fresh_state()
            job = {'path': str(source), 'name': 'test', 'title': '', 'body': ''}
            before = copy.deepcopy((state, job))
            results = []
            worker = PlaybackWorker(job, state, root, preview_key(job, state, preview_limit=10), preview_limit=10)
            worker.ready.connect(results.append)
            def renderer(job, state, *_args, **kwargs):
                self.assertEqual(kwargs['preview_limit'], 10)
                self.assertTrue(kwargs['preview'])
                output = Path(job['task_dir']) / (job['name'] + '.mp4')
                output.write_bytes(b'synthetic proxy')
                return output, {'cursor': 999}
            with patch(PREFIX + 'playback.resolve_tools', return_value=('ffmpeg', 'ffprobe')), \
                 patch(PREFIX + 'playback.probe', return_value={'duration': 10}), \
                 patch(PREFIX + 'playback.render_static_text', side_effect=renderer), \
                 patch.object(Store, 'save', side_effect=AssertionError('must not save')):
                worker.run()
            self.assertNotIn('error', results[0])
            self.assertEqual(results[0]['duration'], 10)
            self.assertEqual((state, job), before)

    def test_limit_cannot_truncate_formal_export_or_accept_invalid_values(self):
        for preview, value in ((False, 10), (True, 0), (True, -1), (True, True),
                               (True, float('nan')), (True, float('inf')), (True, '10')):
            with self.subTest(value=value, preview=preview), self.assertRaises(ValueError):
                render_static_text({}, {}, '.', preview=preview, preview_limit=value)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
    def test_native_quick_ten_seconds_short_source_and_formal_full_length(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp4'
            subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'color=c=teal:s=320x180:r=10:d=12', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
                check=True, capture_output=True, timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            state = fresh_state()
            badge = root / 'badge.png'
            image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('#f7e2da'))
            self.assertTrue(image.save(str(badge)))
            layers = normalize_layers([dict(id='badge', kind='image', path=str(badge), source='fixed',
                x=50, y=50, width=10, height=10)])
            state['settings'].update(aspect='source', layers=layers, darkness=0, music_enabled=False,
                ffmpeg_path=FFMPEG, output_dir=str(root), overwrite=True)
            job = {'path': str(source), 'name': 'quick', 'title': '', 'body': ''}
            ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))
            before = copy.deepcopy(state)
            with patch(PREFIX + 'engine.audio_graph', wraps=audio_graph) as audio:
                output, _ = render_static_text(job, state, root / 'private', preview=True, preview_limit=10)
            self.assertEqual(audio.call_args.args[1], 12)
            self.assertAlmostEqual(probe(output, ffprobe)['duration'], 10, delta=.12)
            full, _ = render_static_text({**job, 'name': 'full'}, state, root / 'private')
            self.assertAlmostEqual(probe(full, ffprobe)['duration'], 12, delta=.12)
            self.assertEqual(state, before)
            state['settings'].update(unvoiced_duration_mode='fixed', unvoiced_duration_seconds=2)
            short, _ = render_static_text({**job, 'name': 'short'}, state, root / 'private', preview=True, preview_limit=10)
            self.assertAlmostEqual(probe(short, ffprobe)['duration'], 2, delta=.12)
