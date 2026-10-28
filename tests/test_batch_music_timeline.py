"""Music index mapping, accurate seeks and persistent playhead compatibility."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.music_analysis import timeline_ranges, from_index
from app_plugins.builtin.batch_text_video.music_inputs import append_inputs
from app_plugins.builtin.batch_text_video.music_preparation import music_key
from app_plugins.builtin.batch_text_video.store import fresh_state, Store, ProfileStore


class MusicTimelineTests(unittest.TestCase):
    def test_maps_cursor_across_cuts_and_wrap_without_gaps(self):
        self.assertEqual(timeline_ranges(((1., 3.), (4., 7.)), 4., 4.),
                         [(6., 7.), (1., 3.), (4., 5.)])
        self.assertEqual(timeline_ranges(((1., 2.),), 1., 2.), [(1., 2.), (1., 2.)])
        self.assertIsNone(timeline_ranges(((1., 2.),), 0., 1000.))

    def test_corrupt_ranges_are_rejected(self):
        for ranges in ([], [[1, 0]], [[0, 2], [1, 3]], [[0, float('nan')]], [[0, 4]]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                from_index('song.wav', 'key', dict(version=1, source_duration=3., ranges=ranges))

    def test_late_cursor_is_an_input_seek_and_does_not_mutate_plan(self):
        music = dict(path='song.wav', duration=600., start=540., cursor=543., audio_key='key')
        before = copy.deepcopy(music)
        args, filters = [], []
        with ExitStack() as leases:
            count, label, offset = append_inputs(args, filters, music, 3, 3., '.', 'ffmpeg', 'ffprobe',
                                                 None, lambda _: None, leases)
        self.assertEqual((count, label, offset), (1, '[music_source]', 0.))
        self.assertLess(args.index('-ss'), args.index('-i'))
        self.assertIn('540.000000000', args)
        self.assertEqual(music, before)

    def test_index_survives_evicted_flac_and_threshold_changes_invalidate_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cache = Path(tmp)/'song.wav', Path(tmp)/'cache'
            path.write_bytes(b'not overwritten')
            settings = fresh_state()['settings']
            with patch.object(engine, 'probe', return_value=dict(duration=10., audio=True)), \
                    patch.object(engine, '_run', return_value='silence_start: 3\nsilence_end: 6'):
                first = engine.clean_music(path, settings, cache, 'ffmpeg', 'ffprobe')
            with patch.object(engine, 'probe', side_effect=AssertionError('rescan')), \
                    patch.object(engine, '_run', side_effect=AssertionError('transcode')):
                self.assertEqual(engine.clean_music(path, settings, cache, 'ffmpeg', 'ffprobe'), first)
            self.assertLess(sum(p.stat().st_size for p in cache.iterdir()), 1000)
            settings['silence_db'] -= 1
            self.assertNotEqual(music_key(path, settings), first.key)

    def test_cancelled_cycle_build_leaves_index_and_source_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)/'cache'
            cache.mkdir()
            song = Path(tmp)/'song.wav'
            song.write_bytes(b'original')
            music = dict(path=str(song), duration=1., start=.1, audio_key='a'*64,
                         source_ranges=((1., 2.),))
            with self.assertRaises(engine.Cancelled), ExitStack() as leases, \
                    patch.object(engine, '_run', side_effect=engine.Cancelled('cancelled')), \
                    patch.object(engine, 'filter_file_option', return_value='-filter_complex_script'):
                append_inputs([], [], music, 0, 100., cache, 'ffmpeg', 'ffprobe',
                              threading.Event(), lambda _: None, leases)
            self.assertEqual(list(cache.iterdir()), [])
            self.assertEqual(song.read_bytes(), b'original')
            from model.MediaCacheLeases import media_cache_in_use
            self.assertFalse(media_cache_in_use(cache/('a'*64+'.flac')))

    def test_revision_guard_reads_token_without_normalizing_all_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            state = fresh_state()
            state['jobs'] = [dict(id=str(i), path='', name=str(i)) for i in range(837)]
            state = store.save(state, 0)
            with patch('app_plugins.builtin.batch_text_video.store.normalize_layers',
                       side_effect=AssertionError('must not rebuild all resources')):
                self.assertEqual(store.current_revision(), state['revision'])
                profile = ProfileStore(tmp, 'default')
                self.assertEqual(profile.current_revision(), state['profiles'][0]['revision'])
            self.assertEqual(store.load()['jobs'], state['jobs'])


FFMPEG = os.environ.get('NODE_FFMPEG', '')
FFPROBE = str(Path(FFMPEG).with_name('ffprobe.exe'))


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), 'requires explicit test FFmpeg')
class RealMusicTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_ffmpeg(self, args):
        result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                                capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
        return result.stdout

    def source(self, codec='pcm_s16le', suffix='.wav'):
        path = self.root/('song'+suffix)
        self.run_ffmpeg(['-f', 'lavfi', '-i',
            r'aevalsrc=if(lt(t\,1)\,sin(2*PI*440*t)\,if(lt(t\,2)\,0\,sin(2*PI*660*t))):s=48000:d=3',
            '-c:a', codec, str(path)])
        return path

    def render(self, music, duration, cache=None):
        args, graph = [], []
        with ExitStack() as leases:
            count, label, offset = append_inputs(args, graph, music, 0, duration, cache or self.root/'cache',
                FFMPEG, FFPROBE, threading.Event(), lambda _: None, leases)
            graph.append(f'{label}atrim=start={offset:.9f}:duration={duration:.9f},asetpts=PTS-STARTPTS[out]')
            script = self.root/'render.filter'
            script.write_text(';\n'.join(graph), encoding='utf8')
            raw = self.run_ffmpeg(args + [engine.filter_file_option(FFMPEG), str(script), '-map', '[out]',
                                         '-ar', '48000', '-ac', '2', '-t', str(duration), '-f', 's16le', '-'])
        return np.frombuffer(raw, '<i2').reshape(-1, 2), count

    def test_cut_and_wrapped_audio_matches_reference_samples(self):
        song = self.source()
        before = song.read_bytes()
        analysis = engine.clean_music(song, fresh_state()['settings'], self.root/'cache', FFMPEG, FFPROBE)
        # Independent reference: decode once, concatenate at the original
        # sample boundaries, then apply the cleaned cursor and repetition.
        pcm = np.frombuffer(self.run_ffmpeg(['-i', str(song), '-ar', '48000', '-ac', '2', '-f', 's16le', '-']), '<i2').reshape(-1, 2)
        cycle = np.concatenate([pcm[round(a*48000):round(b*48000)] for a,b in analysis.ranges])
        cursor = len(cycle)/48000-.25
        music = dict(path=str(song), duration=analysis.duration, start=cursor, audio_key=analysis.key,
                     source_ranges=analysis.ranges)
        actual, _count = self.render(music, 3.25)
        start = round(cursor*48000)
        expected = np.concatenate([cycle]*4)[start:start+round(3.25*48000)]
        self.assertEqual(actual.shape, expected.shape)
        self.assertLessEqual(np.abs(actual.astype('i4')-expected.astype('i4')).max(), 3)
        self.assertFalse(list((self.root/'cache').glob('*.flac')))
        self.assertEqual(song.read_bytes(), before)

    def test_compressed_input_seek_and_very_short_repeated_cycle(self):
        song = self.source('libmp3lame', '.mp3')
        music = dict(path=str(song), duration=engine.probe(song, FFPROBE, True)['duration'],
                     start=2.5, audio_key='unused')
        actual, count = self.render(music, .2)
        self.assertEqual(len(actual), 9600)
        self.assertEqual(count, 1)
        self.assertGreater(np.sqrt(np.mean(actual.astype(float)**2)), 1000)
        # Exercise bounded fallback with a one-second cycle used 100 times.
        short = self.root/'short.wav'
        self.run_ffmpeg(['-f','lavfi','-i','sine=duration=1:sample_rate=48000','-c:a','pcm_s16le',str(short)])
        repeated, count = self.render(dict(path=str(short), duration=1., start=.75, audio_key='short'), 70.)
        self.assertEqual(count, 1)
        self.assertEqual(len(repeated), 70*48000)

    def test_dense_cut_fallback_is_bounded_reused_and_not_built_for_preview(self):
        song = self.source()
        cache = self.root/'cache'
        analysis = engine.clean_music(song, fresh_state()['settings'], cache, FFMPEG, FFPROBE)
        music = dict(path=str(song), duration=analysis.duration, start=.3, audio_key=analysis.key,
                     source_ranges=analysis.ranges)
        preview, count = self.render(music, .2)
        self.assertFalse(list(cache.glob('*.flac')))
        actual, count = self.render(music, 75.)
        self.assertEqual(count, 1)
        self.assertEqual(len(actual), 75*48000)
        self.assertEqual(len(list(cache.glob('*.flac'))), 1)
        from model.MediaCacheLeases import media_cache_in_use
        self.assertFalse(media_cache_in_use(next(cache.glob('*.flac'))))
        with patch.object(engine, '_materialize_music_cycle', side_effect=AssertionError('must reuse')):
            self.render(music, 75.)

    def test_stereo_antiphase_is_not_mistaken_for_silence(self):
        song = self.root/'antiphase.wav'
        self.run_ffmpeg(['-f', 'lavfi', '-i', 'aevalsrc=sin(2*PI*440*t)|-sin(2*PI*440*t):s=48000:d=2',
                         '-c:a', 'pcm_s16le', str(song)])
        analysis = engine.clean_music(song, fresh_state()['settings'], self.root/'cache', FFMPEG, FFPROBE)
        self.assertEqual(analysis[1], 2.)
        self.assertEqual(analysis[0], song)

    def test_dense_cycle_build_limits_each_decoder_group_to_32_inputs(self):
        song = self.root/'dense.wav'
        self.run_ffmpeg(['-f', 'lavfi', '-i',
            r'aevalsrc=if(lt(mod(t\,2)\,1)\,sin(2*PI*440*t)\,0):s=48000:d=68',
            '-c:a', 'pcm_s16le', str(song)])
        analysis = engine.clean_music(song, fresh_state()['settings'], self.root/'cache', FFMPEG, FFPROBE)
        music = dict(path=str(song), duration=analysis.duration, start=0., audio_key=analysis.key,
                     source_ranges=analysis.ranges)
        counts = []
        original = engine._run
        def capture(args, *a, **kw):
            counts.append(args.count('-i'))
            return original(args, *a, **kw)
        with patch.object(engine, '_run', side_effect=capture):
            actual, count = self.render(music, 75.)
        self.assertEqual(len(actual), 75*48000)
        self.assertEqual(count, 1)
        self.assertEqual(len(counts), 3)  # Two bounded groups, then one valid FLAC cycle.
        self.assertLessEqual(max(counts), 32)
