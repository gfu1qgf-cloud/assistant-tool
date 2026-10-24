"""Opt-in native stress fixture: real outputs past the reported 55-item stop."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
from app_plugins.builtin.batch_text_video.music_inputs import append_inputs
from app_plugins.builtin.batch_text_video.music_preparation import current_music_session
from app_plugins.builtin.batch_text_video.store import ProfileStore, Store, fresh_state
from model.AppDataStorage import data_file

FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(os.environ.get('TEST_LONG_BATCH') == '1' and Path(FFMPEG).is_file(),
                     'opt-in generated-media long batch')
class LongMusicBatchTests(unittest.TestCase):
    def test_72_real_exports_eight_encoders_keep_playheads_and_order(self):
        import numpy as np
        import wave
        ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            song = root/'song.wav'
            tone = (np.sin(np.arange(48000)*2*np.pi*440/48000)*9000).astype('<i2').tobytes()
            with wave.open(str(song), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
                for _ in range(8): stream.writeframesraw(tone+bytes(96000))
            before = song.read_bytes()
            store = Store(root/'private')
            state = fresh_state()
            state['settings'].update(export_workers=8, music_enabled=True, layers=[])
            state['music'] = [dict(id='music', path=str(song), cursor=0., audio_key='')]
            state['jobs'] = [dict(id=str(i), path='', name=f'video-{i}', title='', body='',
                                   status='待生成', output='', error='') for i in range(72)]
            state = store.save(state, 0)
            profile = ProfileStore(store.directory, 'default')
            state = profile.load()
            active, peak, lock = 0, 0, threading.Lock()
            @contextmanager
            def prepare(job, snapshot, directory, token, progress, report):
                nonlocal active, peak
                started = time.monotonic()
                cache = root/'cache'
                session = current_music_session()
                session.prime(snapshot, cache, FFMPEG, ffprobe)
                music = engine.choose_music(snapshot, .2, cache, FFMPEG, ffprobe, token, report)
                with tempfile.TemporaryDirectory(dir=root) as temporary, ExitStack() as leases:
                    temporary = Path(temporary)
                    args = [FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
                            '-filter_complex_threads', '1', '-f', 'lavfi', '-i', 'color=s=64x64:r=10:d=0.2']
                    graph = ['[0:v]format=yuv420p[v]']
                    _n, label, offset = append_inputs(args, graph, music, 1, .2, cache,
                                                      FFMPEG, ffprobe, token, report, leases)
                    graph.append(f'{label}atrim=start={offset}:duration=0.2,asetpts=PTS-STARTPTS[a]')
                    script = temporary/'audio.filter'
                    data_file(script).write_text(';\n'.join(graph), 'utf8')
                    target, output = temporary/'result.mp4', root/(job['name']+'.mp4')
                    args += [engine.filter_file_option(FFMPEG), str(script), '-map', '[v]', '-map', '[a]',
                             '-t', '0.2', '-threads', '1', '-c:v', 'libx264', '-preset', 'ultrafast',
                             '-c:a', 'aac', str(target)]
                    item = PreparedExport(args, target, output, ffprobe, .2, True, music, [], token,
                        progress, report, False, 'cpu', False, False, 8, render_scope=str(store.path),
                        source_paths=(str(song),), timings={'prepare_seconds':time.monotonic()-started})
                    original = item.encode
                    def measured():
                        nonlocal active, peak
                        with lock: active += 1; peak = max(peak, active)
                        try: return original()
                        finally:
                            with lock: active -= 1
                    item.encode = measured
                    yield item
            def renderer(*a, **kw): raise AssertionError('prepared pipeline required')
            renderer.prepare_export = prepare
            messages = []
            result, saved = execute_batch(profile, state, [job['id'] for job in state['jobs']],
                threading.Event(), lambda *_: None, messages.append, lambda *_: None,
                {'static_text': renderer})
            self.assertEqual((result['completed'], result['failed']), (72, 0), (result, messages[-4:]))
            self.assertNotIn('error', result)
            self.assertGreaterEqual(peak, 4)
            self.assertEqual(profile.current_revision(), state['revision']+72)
            self.assertEqual([Path(p).stem for p in result['paths']], [f'video-{i}' for i in range(72)])
            analysis = engine.clean_music(song, saved['settings'], root/'cache', FFMPEG, ffprobe)
            self.assertAlmostEqual(saved['music'][0]['cursor'], (72*.2)%analysis[1], places=6)
            self.assertFalse(list((root/'cache').glob('*.flac')))
            self.assertEqual(song.read_bytes(), before)
            print(f'LONG_BATCH_RESULT: completed=72 peak_encoders={peak} revision={profile.current_revision()} '
                  f'cursor={saved["music"][0]["cursor"]:.6f} cache_bytes='
                  f'{sum(p.stat().st_size for p in (root/"cache").glob("*") if p.is_file())}')
