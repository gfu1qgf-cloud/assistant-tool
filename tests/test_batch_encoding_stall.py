"""Stale heartbeats, bounded retries and cancellation must not publish partial files."""
from pathlib import Path
import unittest
import subprocess
import sys
from unittest.mock import Mock, patch

from app_plugins.builtin.batch_text_video.encoding_watchdog import EncodingWatchdog, EncodingStalled, serial_filters
from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
from app_plugins.builtin.batch_text_video.engine import Cancelled
from model.MediaEncoding import video_encoding_args


class EncodingStallTests(unittest.TestCase):
    def prepared(self, encoder='qsv', fallback=True):
        progress = Mock(); progress.stage = Mock()
        return PreparedExport(['ffmpeg', '-y', '-filter_threads', '4', '-filter_complex_threads', '4',
            '-i', 'voice.wav', '-filter_complex', 'keep-all-effects',
            *video_encoding_args(encoder, workers=8), '-pix_fmt', 'nv12' if encoder == 'qsv' else 'yuv420p',
            'temporary.mp4'], Path('temporary.mp4'), Path('final.mp4'), 'ffprobe', 7.2,
            True, None, [], None, progress, Mock(), False, encoder, fallback, False, 8)

    def test_repeated_heartbeat_does_not_extend_deadline(self):
        watch = EncodingWatchdog(90)
        heartbeat = 'frame=210\r\nout_time_us=7125333\ntotal_size=5242928\nprogress=continue\n'
        watch.observe(heartbeat, 10)
        for now in (20, 40, 70, 99):
            self.assertEqual(watch.observe(heartbeat, now), 7125333)
        with self.assertRaisesRegex(EncodingStalled, '7.125'):
            watch.observe(heartbeat, 100)

    def test_partial_lines_and_real_progress_reset_idle_timer(self):
        watch = EncodingWatchdog(90)
        self.assertEqual(watch.observe('frame=1\nout_time_us=7', 1), 0)
        self.assertEqual(watch.observe('125333\ntotal_size=N/A\n', 85), 7125333)
        watch.observe('', 170)
        watch.observe('frame=2\nout_time_us=7125333\ntotal_size=100\n', 174)
        watch.observe('', 260)
        with self.assertRaises(EncodingStalled): watch.observe('', 264)

    def test_initialisation_without_frames_keeps_full_timeout(self):
        watch = EncodingWatchdog(90)
        watch.observe('frame=0\nout_time_us=0\ntotal_size=48\n', 1)
        watch.observe('progress=continue\n', 400)
        self.assertIsNone(watch.last_advance)

    def test_stall_retries_once_with_same_effects_and_serial_cpu(self):
        prepared = self.prepared()
        with patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=[EncodingStalled('stalled'), '']) as run, \
                patch('app_plugins.builtin.batch_text_video.engine.probe', return_value={'duration': 7.2, 'audio': True}):
            prepared._encode()
        self.assertEqual(run.call_count, 2)
        args = run.call_args.args[0]
        self.assertIn('libx264', args); self.assertNotIn('h264_qsv', args)
        self.assertEqual(args[args.index('-filter_complex_threads')+1], '1')
        self.assertEqual(args[args.index('-pix_fmt')+1], 'yuv420p')
        self.assertIn('keep-all-effects', args); self.assertEqual(args[-1], 'temporary.mp4')
        prepared.progress.stage.assert_any_call('停滞恢复：CPU重试')
        self.assertTrue(prepared.encoded)

    def test_second_stall_stops_and_never_marks_encoded(self):
        prepared = self.prepared()
        with patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=EncodingStalled('stalled')) as run:
            with self.assertRaises(EncodingStalled): prepared._encode()
        self.assertEqual(run.call_count, 2); self.assertFalse(prepared.encoded)
        with self.assertRaisesRegex(RuntimeError, '尚未编码'): prepared.publish()

    def test_cpu_stall_can_retry_serial_filters_but_other_errors_cannot(self):
        prepared = self.prepared('cpu')
        with patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=[EncodingStalled('stalled'), '']) as run, \
                patch('app_plugins.builtin.batch_text_video.engine.probe', return_value={'duration': 7.2, 'audio': True}):
            prepared._encode()
        self.assertEqual(run.call_count, 2)
        for error, fallback in ((Cancelled('stop'), True), (RuntimeError('bad file'), True),
                                (EncodingStalled('stall'), False), (TimeoutError('timeout'), True)):
            with self.subTest(error=error):
                prepared = self.prepared(fallback=fallback)
                with patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=error) as run:
                    with self.assertRaises(type(error)): prepared._encode()
                self.assertEqual(run.call_count, 1); self.assertFalse(prepared.encoded)

    def test_retry_still_validates_duration_and_audio(self):
        for result in ({'duration': 7., 'audio': True}, {'duration': 7.2, 'audio': False}):
            prepared = self.prepared()
            with patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=[EncodingStalled('stalled'), '']), \
                    patch('app_plugins.builtin.batch_text_video.engine.probe', return_value=result):
                with self.assertRaises(RuntimeError): prepared._encode()
            self.assertFalse(prepared.encoded)

    def test_real_child_repeating_stale_progress_is_reaped_without_a_callback(self):
        from app_plugins.builtin.batch_text_video.engine import _run
        module = 'app_plugins.builtin.batch_text_video.encoding_watchdog.EncodingWatchdog'
        # The child is a generated fixture, never a user encoder. Use a short
        # injectable deadline to verify the complete reader/kill/wait path.
        script = ("import time\nwhile True:\n print('frame=210\\nout_time_us=7125333\\n"
                  "total_size=5242928\\nprogress=continue',flush=True)\n time.sleep(.03)")
        children = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs); children.append(child); return child
        with patch(module, return_value=EncodingWatchdog(.3)), \
                patch('app_plugins.builtin.batch_text_video.engine.subprocess.Popen', side_effect=launch):
            with self.assertRaisesRegex(EncodingStalled, '7.125'):
                _run([getattr(sys, '_base_executable', sys.executable), '-u', '-c', script,
                      '-progress', 'pipe:1'], duration=7.2, timeout=5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
