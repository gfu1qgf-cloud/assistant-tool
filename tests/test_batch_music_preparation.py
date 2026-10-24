"""Music read-ahead must not serialize independent audio or consume playheads."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.store import file_identity, fresh_state, Store


class MusicSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'original.mp3'
        self.path.write_bytes(b'original music, not a generated cache')
        self.cache = self.root / 'cache'
        self.settings = fresh_state()['settings']

    def test_unprocessed_music_uses_original_without_ffmpeg(self):
        self.settings['skip_silence'] = False
        before = self.path.read_bytes()
        with patch.object(engine, 'probe', return_value={'duration': 12., 'audio': True}) as probe, \
                patch.object(engine, '_run', side_effect=AssertionError('unnecessary full-track conversion')):
            path, duration, _key = engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        self.assertEqual(path, self.path)
        self.assertEqual(duration, 12.)
        probe.assert_called_once_with(self.path, 'ffprobe', audio_only=True)
        self.assertFalse(list(self.cache.glob('*.flac')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_silence_detection_without_cuts_does_not_transcode_and_is_cached(self):
        self.settings['skip_silence'] = True
        with patch.object(engine, 'probe', return_value={'duration': 12., 'audio': True}), \
                patch.object(engine, '_run', return_value='') as run:
            first = engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        self.assertEqual(first[0], self.path)
        self.assertEqual(run.call_count, 1)
        self.assertIn('silencedetect', ' '.join(run.call_args.args[0]))
        self.assertFalse(list(self.cache.glob('*.flac')))
        with patch.object(engine, 'probe', side_effect=AssertionError('must reuse metadata')), \
                patch.object(engine, '_run', side_effect=AssertionError('must not rescan')):
            second = engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        self.assertEqual(second, first)

    def test_real_cuts_keep_a_small_index_and_preserve_original(self):
        self.settings['skip_silence'] = True
        before = self.path.read_bytes()
        commands = []
        def run(command, *_args, **_kwargs):
            commands.append(command)
            if 'silencedetect' in ' '.join(command):
                return 'silence_start: 0\nsilence_end: 1\n'
            Path(command[-1]).write_bytes(b'generated flac')
            return ''
        def probe(path, *_args, **_kwargs):
            return {'duration': 12. if Path(path) == self.path else 11.04, 'audio': True}
        with patch.object(engine, 'probe', side_effect=probe), patch.object(engine, '_run', side_effect=run), \
                patch.object(engine, 'filter_file_option', return_value='-filter_complex_script'):
            first = engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        self.assertEqual(first[0], self.path)
        self.assertEqual(first[1], 11.04)
        self.assertEqual(len(commands), 1)
        self.assertEqual(first.ranges, ((.96, 12.),))
        self.assertEqual(len(list(self.cache.glob('*.index.json'))), 1)
        self.assertFalse(list(self.cache.glob('*.flac')))
        with patch.object(engine, '_run', side_effect=AssertionError('must reuse processed cache')):
            self.assertEqual(engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe'), first)
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_key_and_playhead_survive_direct_source_optimization(self):
        self.settings['skip_silence'] = False
        info = self.path.stat()
        key = hashlib.sha256(json.dumps([file_identity(self.path), info.st_size, info.st_mtime_ns,
            False, self.settings['silence_db'], self.settings['silence_seconds']]).encode('utf-8')).hexdigest()
        self.cache.mkdir()
        (self.cache / (key + '.flac')).write_bytes(b'old generated cache')
        (self.cache / (key + '.json')).write_text(json.dumps({'duration': 12.}), encoding='utf-8')
        state = fresh_state()
        state['settings'].update(self.settings)
        state['music'] = [dict(id='music', path=str(self.path), cursor=7.5, audio_key=key)]
        before = copy.deepcopy(state)
        with patch.object(engine, 'probe', return_value={'duration': 12., 'audio': True}), \
                patch.object(engine, '_run', side_effect=AssertionError('must not transcode')):
            plan = engine.choose_music(state, 3., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
        self.assertEqual(plan['start'], 7.5)
        self.assertEqual(plan['cursor'], 10.5)
        self.assertEqual(plan['audio_key'], key)
        self.assertFalse(plan['generated'])
        self.assertEqual(plan['path'], str(self.path))
        self.assertEqual(state, before)
        self.assertEqual((self.cache / (key + '.flac')).read_bytes(), b'old generated cache')

    def test_changed_source_rebuilds_metadata(self):
        self.settings['skip_silence'] = False
        with patch.object(engine, 'probe', return_value={'duration': 12., 'audio': True}):
            first = engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        self.path.write_bytes(b'changed and longer original music')
        with patch.object(engine, 'probe', return_value={'duration': 20., 'audio': True}):
            second = engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        self.assertNotEqual(first[2], second[2])
        self.assertEqual(second[1], 20.)

    def test_no_audio_and_all_silence_are_reported_not_mixed_as_valid_music(self):
        with patch.object(engine, 'probe', return_value={'duration': 12., 'audio': False}):
            with self.assertRaisesRegex(ValueError, '没有音轨'):
                engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')
        with patch.object(engine, 'probe', return_value={'duration': 12., 'audio': True}), \
                patch.object(engine, '_run', return_value='silence_start: 0'):
            with self.assertRaisesRegex(ValueError, '非静音'):
                engine.clean_music(self.path, self.settings, self.cache, 'ffmpeg', 'ffprobe')


class MusicReadAheadTests(unittest.TestCase):
    setUp = MusicSourceTests.setUp

    def playlist(self, count):
        state = fresh_state()
        state['settings']['export_workers'] = 4
        for i in range(count):
            path = self.root / f'music-{i}.wav'
            path.write_bytes(str(i).encode())
            state['music'].append(dict(id=str(i), path=str(path), cursor=0., audio_key=''))
        return state

    def test_read_ahead_overlaps_independent_tracks_and_only_reads_a_bounded_window(self):
        from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession, music_key, music_session
        state = self.playlist(20)
        before = copy.deepcopy(state)
        barrier = threading.Barrier(3)
        visited, threads = [], set()
        lock = threading.Lock()
        def prepare(path, settings, _cache, *_args):
            with lock:
                visited.append(Path(path).name)
                threads.add(threading.get_ident())
            barrier.wait(3)
            return Path(path), 12., music_key(path, settings)
        with patch.object(engine, 'clean_music', side_effect=prepare):
            with MusicPreparationSession(threading.Event(), 3) as session, music_session(session):
                session.prime(state, self.cache, 'ffmpeg', 'ffprobe')
                plan = engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
                for future in list(session.entries.values()):
                    future.result(3)
        self.assertEqual(len(threads), 3)
        self.assertEqual(set(visited), {'music-0.wav', 'music-1.wav', 'music-2.wav'})
        self.assertEqual(plan['id'], '0')
        self.assertEqual(plan['cursor'], 2.)
        self.assertEqual(state, before)

    def test_repeated_music_is_built_once_and_cache_stays_leased_until_session_ends(self):
        from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession, music_key, music_session
        from model.MediaCacheLeases import media_cache_in_use
        state = self.playlist(1)
        key = music_key(state['music'][0]['path'], state['settings'])
        output = self.cache / (key + '.flac')
        self.cache.mkdir()
        def prepare(*_args):
            self.assertTrue(media_cache_in_use(output))
            output.write_bytes(b'generated')
            return output, 10., key
        with patch.object(engine, 'clean_music', side_effect=prepare) as clean:
            with MusicPreparationSession(threading.Event(), 3) as session, music_session(session):
                session.prime(state, self.cache, 'ffmpeg', 'ffprobe')
                first = engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
                state['music'][0].update(cursor=first['cursor'], audio_key=key)
                second = engine.choose_music(state, 2., self.root/'another-task-cache', 'ffmpeg', 'ffprobe', None, lambda _: None)
                self.assertTrue(media_cache_in_use(output))
                self.assertEqual(clean.call_count, 1)
                self.assertEqual(second['start'], 2.)
        self.assertFalse(media_cache_in_use(output))

    def test_disabled_music_does_not_submit_work(self):
        from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession
        state = self.playlist(2)
        state['settings']['music_enabled'] = False
        with patch.object(engine, 'clean_music') as prepare:
            with MusicPreparationSession(threading.Event(), 3) as session:
                session.prime(state, self.cache, 'ffmpeg', 'ffprobe')
        prepare.assert_not_called()

    def test_temporary_preparation_failure_is_not_cached_forever(self):
        from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession, music_key, music_session
        state = self.playlist(1)
        path = state['music'][0]['path']
        ready = (Path(path), 10., music_key(path, state['settings']))
        with patch.object(engine, 'clean_music', side_effect=[RuntimeError('temporary read failure'), ready]) as clean:
            with MusicPreparationSession(threading.Event(), 2) as session, music_session(session):
                session.prime(state, self.cache, 'ffmpeg', 'ffprobe')
                with self.assertRaisesRegex(ValueError, '没有可用'):
                    engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
                plan = engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
                self.assertEqual(plan['id'], '0')
                self.assertEqual(clean.call_count, 2)

    def test_export_lease_handoff_allows_pruning_then_rebuilds_an_evicted_result(self):
        from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession, music_key, music_session
        from model.MediaCacheLeases import media_cache_in_use, media_cache_lease, trim_generated_cache
        state = self.playlist(1)
        key = music_key(state['music'][0]['path'], state['settings'])
        output = self.cache / (key + '.flac')
        self.cache.mkdir()
        def prepare(*_args):
            output.write_bytes(b'generated music')
            return output, 10., key
        with patch.object(engine, 'clean_music', side_effect=prepare) as clean:
            with MusicPreparationSession(threading.Event(), 2) as session, music_session(session):
                session.prime(state, self.cache, 'ffmpeg', 'ffprobe')
                engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
                with media_cache_lease(output):
                    session.release(output)
                    self.assertTrue(media_cache_in_use(output))
                    trim_generated_cache(self.cache, '.flac', 1)
                    self.assertTrue(output.exists())
                self.assertFalse(media_cache_in_use(output))
                trim_generated_cache(self.cache, '.flac', 1)
                self.assertFalse(output.exists())
                engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', None, lambda _: None)
                self.assertEqual(clean.call_count, 2)
                self.assertTrue(output.exists())

    def test_canceling_read_ahead_stops_workers_and_does_not_change_playheads(self):
        from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession, music_session
        state = self.playlist(3)
        before = copy.deepcopy(state)
        started = threading.Event()
        parent = threading.Event()
        def prepare(_path, _settings, _cache, _ffmpeg, _ffprobe, cancel):
            started.set()
            deadline = time.monotonic() + 3
            while not cancel.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            raise engine.Cancelled('synthetic cancel')
        with patch.object(engine, 'clean_music', side_effect=prepare):
            with MusicPreparationSession(parent, 2) as session, music_session(session):
                session.prime(state, self.cache, 'ffmpeg', 'ffprobe')
                self.assertTrue(started.wait(3))
                parent.set()
                with self.assertRaises(engine.Cancelled):
                    engine.choose_music(state, 2., self.cache, 'ffmpeg', 'ffprobe', parent, lambda _: None)
        self.assertEqual(state, before)

    def test_global_processing_budget_is_bounded_and_wait_can_cancel(self):
        from app_plugins.builtin.batch_text_video.music_preparation import processing_slot
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        active = peak = 0
        def work(cancel):
            nonlocal active, peak
            with processing_slot(cancel):
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 4:
                        entered.set()
                try:
                    if not release.wait(3):
                        raise TimeoutError('test audio processing not released')
                finally:
                    with lock:
                        active -= 1
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(work, threading.Event()) for _ in range(4)]
            self.assertTrue(entered.wait(3))
            cancel = threading.Event()
            waiting = pool.submit(work, cancel)
            cancel.set()
            try:
                with self.assertRaises(engine.Cancelled):
                    waiting.result(3)
            finally:
                release.set()
            for future in futures:
                future.result(3)
        self.assertEqual(peak, 4)


FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'requires explicit test FFmpeg')
class RealMusicPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))

    def ffmpeg(self, *args):
        subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def state(self):
        source = self.root/'video.mp4'
        self.ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=160x240:r=10:d=0.4',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source))
        state = fresh_state()
        state['settings'].update(layers=[], aspect='source', use_voice=False, export_workers=2,
            ffmpeg_path=FFMPEG, auto_crop_black_bars=False, unvoiced_duration_mode='source',
            output_dir=str(self.root/'out'), music_enabled=True, skip_silence=True)
        for i in range(2):
            song = self.root/f'song-{i}.mp3'
            self.ffmpeg('-f', 'lavfi', '-i', f'sine=frequency={440+i*220}:duration=3:sample_rate=48000',
                        '-c:a', 'libmp3lame', str(song))
            state['music'].append(dict(id=f'm-{i}', path=str(song), cursor=0., audio_key=''))
        state['jobs'] = [dict(id=f'j-{i}', path=str(source), name=f'result-{i}',
            title=f'Title {i}', body=f'Body {i}', status='待生成', output='', error='') for i in range(3)]
        store = Store(self.root/'private')
        return store, store.save(state, 0)

    def test_music_scans_overlap_and_original_mp3_tracks_render_without_flac_copies(self):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        store, state = self.state()
        before = {item['path']: Path(item['path']).read_bytes() for item in state['music']}
        original = engine._run
        scans, threads = [], set()
        barrier = threading.Barrier(2)
        def run(args, *a, **kw):
            if 'silencedetect' in ' '.join(args):
                scans.append(args[args.index('-i')+1])
                threads.add(threading.get_ident())
                barrier.wait(10)
            return original(args, *a, **kw)
        messages = []
        with patch.object(engine, '_run', side_effect=run):
            result, saved = execute_batch(store, state, [job['id'] for job in state['jobs']],
                threading.Event(), lambda *_: None, messages.append, lambda *_: None)
        self.assertEqual((result['completed'], result['failed']), (3, 0), (result, messages))
        self.assertEqual(len(threads), 2)
        self.assertEqual(len(scans), 2)
        self.assertAlmostEqual(saved['music'][0]['cursor'], .8, places=3)
        self.assertAlmostEqual(saved['music'][1]['cursor'], .4, places=3)
        self.assertFalse(list(self.root.rglob('*.flac')))
        for output in result['paths']:
            info = engine.probe(output, self.ffprobe)
            self.assertTrue(info['audio'])
            self.assertAlmostEqual(info['duration'], .4, delta=.05)
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)

    def test_direct_music_directory_is_never_pruned_as_a_generated_cache(self):
        store, state = self.state()
        state['settings']['skip_silence'] = False
        sentinel = self.root / ('f'*64 + '.flac')
        sentinel.write_bytes(b'user-owned file')
        calls = []
        from model.MediaCacheLeases import trim_generated_cache
        def trim(directory, *args):
            calls.append(Path(directory))
            return trim_generated_cache(directory, *args)
        with patch('model.MediaCacheLeases.trim_generated_cache', side_effect=trim):
            engine.render_static_text(state['jobs'][0], state, store.directory, threading.Event())
        self.assertNotIn(self.root, calls)
        self.assertEqual(sentinel.read_bytes(), b'user-owned file')

    def test_music_processing_overlaps_text_layout_even_with_one_encoder(self):
        store, state = self.state()
        state['settings']['export_workers'] = 1
        state['music'] = state['music'][:1]
        scan_started, layout_started = threading.Event(), threading.Event()
        run, overlay = engine._run, engine.render_overlay
        def audio_work(args, *a, **kw):
            if 'silencedetect' in ' '.join(args):
                scan_started.set()
                if not layout_started.wait(5):
                    raise TimeoutError('music work blocked text layout')
            return run(args, *a, **kw)
        def layout(*args, **kwargs):
            layout_started.set()
            self.assertTrue(scan_started.wait(5), 'text layout blocked music preparation')
            return overlay(*args, **kwargs)
        with patch.object(engine, '_run', side_effect=audio_work), patch.object(engine, 'render_overlay', side_effect=layout):
            output, _music = engine.render_static_text(state['jobs'][0], state, store.directory, threading.Event())
        self.assertTrue(output.is_file())
        self.assertTrue(scan_started.is_set())

    def test_real_silence_removal_reuses_index_without_whole_track_conversion(self):
        song = self.root/'with-silence.wav'
        self.ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=1:sample_rate=48000',
                    '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=1',
                    '-f', 'lavfi', '-i', 'sine=frequency=660:duration=1:sample_rate=48000',
                    '-filter_complex', '[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]',
                    '-map', '[out]', '-c:a', 'pcm_s16le', str(song))
        before = song.read_bytes()
        settings = fresh_state()['settings']
        first = engine.clean_music(song, settings, self.root/'cache', FFMPEG, self.ffprobe)
        self.assertEqual(first[0], song)
        self.assertEqual(len(first.ranges), 2)
        self.assertFalse(list((self.root/'cache').glob('*.flac')))
        self.assertGreater(first[1], 2.)
        self.assertLess(first[1], 2.2)
        self.assertTrue(engine.probe(first[0], self.ffprobe)['audio'])
        with patch.object(engine, '_run', side_effect=AssertionError('must reuse already cut audio')):
            second = engine.clean_music(song, settings, self.root/'cache', FFMPEG, self.ffprobe)
        self.assertEqual(second, first)
        self.assertEqual(song.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
