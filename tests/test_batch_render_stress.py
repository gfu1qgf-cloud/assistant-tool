"""Sustained prepared exports with the same multi-layer/audio topology as the reported stall."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.encoding_watchdog import EncodingStalled
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import Store, fresh_state

FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'requires explicit test FFmpeg')
class RenderStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_48_eight_worker_exports_pass_the_reported_twenty_job_boundary(self):
        hardware = os.environ.get('TEST_BATCH_QSV_STRESS') == '1'
        size = (1080, 1920) if hardware else (216, 384)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def make(args):
                result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                    capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace')[-2000:])
            image = root/'picture.png'
            picture = QtGui.QImage(*size, QtGui.QImage.Format.Format_RGBA8888)
            picture.fill(QtGui.QColor('#905020')); self.assertTrue(picture.save(str(image)))
            clip = root/'clip.mp4'
            make(['-f', 'lavfi', '-i', f'testsrc2=s={size[0]}x{size[1]}:r=30:d=0.7',
                '-c:v', 'libx264', '-preset', 'ultrafast', str(clip)])
            voices = []
            for index, duration in enumerate((6.77, 7.17, 8.85)):
                voice = root/f'voice{index}.mp3'; voices.append(voice)
                make(['-f', 'lavfi', '-i', f'sine=sample_rate=44100:duration={duration}', '-c:a', 'libmp3lame', str(voice)])
            music = root/'music.mp3'
            make(['-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=44100:duration=19',
                  '-ac', '2', '-c:a', 'libmp3lame', str(music)])
            store = Store(root/'private'); state = fresh_state()
            state['settings'].update(ffmpeg_path=FFMPEG, output_dir=str(root/'out'), use_voice=True,
                export_workers=8, video_encoder='qsv' if hardware else 'cpu', encoder_fallback=True,
                export_quality='medium', skip_silence=False, auto_crop_black_bars=False, voice_fx_preset='church')
            common = dict(kind='image', path=str(image), source='fixed', enabled=True, opacity=100,
                          x=50, y=50, width=100, height=100)
            state['settings']['layers'] = normalize_layers([
                dict(common, id='base'),
                dict(common, id='breathing', visual_animation='breathe', animation_strength=8, animation_period=5),
                dict(common, id='screen', path=str(clip), blend_mode='screen', opacity=80),
                dict(common, id='foreground', width=10, height=10),
            ])
            state['music'] = [dict(id='music', path=str(music), cursor=0., audio_key='')]
            state['jobs'] = [dict(id=str(i), path='', name=f'video-{i}', title='', body='',
                voice_path=str(voices[i%3]), status='待生成', output='', error='') for i in range(48)]
            state = store.save(state, 0)
            calls = 0; forced = 0; real_stalls = 0; lock = threading.Lock(); messages = []; completed = []
            original_run = engine._run
            def counted(*args, **kwargs):
                nonlocal calls, forced, real_stalls
                with lock:
                    calls += 1
                    # Default regression deliberately stalls the 26th attempt;
                    # native QSV stress never injects a failure.
                    inject = calls == 26 and not hardware
                    if inject: forced += 1
                if inject: raise EncodingStalled('generated fault at attempt 26')
                try: return original_run(*args, **kwargs)
                except EncodingStalled:
                    with lock: real_stalls += 1
                    raise
            def done(identifier, status):
                completed.append((identifier, status))
                if len(completed) % 8 == 0:
                    print(f'stress {"QSV1080" if hardware else "CPU216"}: {len(completed)}/48', flush=True)
            with patch.object(engine, 'output_size', return_value=size), patch.object(engine, '_run', side_effect=counted):
                result, state = execute_batch(store, state, [job['id'] for job in state['jobs']],
                    threading.Event(), lambda *_: None, messages.append, done)
            print(f'stress attempts={calls} injected={forced} detected_stalls={real_stalls}', flush=True)
            self.assertEqual((result['completed'], result['failed']), (48, 0), (result, messages[-12:]))
            self.assertNotIn('error', result)
            self.assertEqual([identifier for identifier, _ in completed], list(map(str, range(48))))
            self.assertTrue(all(status == '已完成' for _, status in completed))
            self.assertTrue(all(Path(path).is_file() for path in result['paths']))
            expected_cursor = sum(engine.probe(voices[i%3], str(Path(FFMPEG).with_name('ffprobe.exe')), True)['duration']
                                  for i in range(3))*16
            music_length = engine.probe(music, str(Path(FFMPEG).with_name('ffprobe.exe')), True)['duration']
            self.assertAlmostEqual(state['music'][0]['cursor'], expected_cursor % music_length, places=3)
            self.assertFalse(list((root/'out').glob('.text-video-*')))
            if not hardware: self.assertEqual(forced, 1)
