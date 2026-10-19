"""Source protection, publication conflicts and cache handoff, synthetic files only."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
from app_plugins.builtin.batch_text_video.store import Store, ProfileStore, fresh_state
from model.MediaCacheLeases import trim_generated_cache, media_cache_in_use

FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'requires explicit test FFmpeg')
class RealExportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.mp4'
        self.make_video(self.source, 'red')

    def make_video(self, path, color):
        subprocess.run([FFMPEG, '-y', '-nostdin', '-v', 'error', '-f', 'lavfi',
                        '-i', f'color=c={color}:s=320x240:r=25:d=0.8', '-an',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def make_state(self, name):
        state = fresh_state()
        layer = new_text_component()
        layer.update(text='TEST', font_min=12, font_max=18, width=80, height=70)
        state['settings'].update(ffmpeg_path=FFMPEG, aspect='source', layers=[layer],
                                 music_enabled=False, use_voice=False, skip_silence=False,
                                 auto_crop_black_bars=False, overwrite=True,
                                 output_dir=str(self.root), export_workers=1)
        state['jobs'] = [dict(id='job', path=str(self.source), name=name, title='', body='',
                              status='待生成', error='', output='')]
        return state

    def test_unused_background_pool_source_is_protected_from_overwrite(self):
        victim = self.root / 'unused-pool-source.mp4'
        self.make_video(victim, 'blue')
        original = victim.read_bytes()
        state = self.make_state(victim.stem)
        state['backgrounds'] = [str(self.source), str(victim)]
        try:
            engine.render_static_text(state['jobs'][0], state, self.root / 'private')
        except ValueError as error:
            self.assertIn('覆盖', str(error))  # Only source protection is success here.
        self.assertTrue(victim.read_bytes() == original,
                        'Imported but unpaired video-pool source was overwritten by the final export')

    def revision_conflict(self, workers):
        output = self.root / 'existing-result.mp4'
        self.make_video(output, 'blue')
        original = output.read_bytes()
        editor_store = Store(self.root / 'private')
        initial = self.make_state(output.stem)
        initial['settings']['export_workers'] = workers
        editor_store.save(initial, 0)
        store = ProfileStore(editor_store.directory, 'default')
        state = store.load()
        real_encode = PreparedExport.encode
        real_publish = PreparedExport.publish
        published = []

        def encode_with_external_edit(prepared):
            value = real_encode(prepared)
            newer = editor_store.load()
            newer['jobs'][0]['body'] = 'EDIT MADE AFTER THIS EXPORT STARTED'
            editor_store.save(newer, newer['revision'])
            return value

        def record_publish(prepared):
            published.append(str(prepared.output))
            return real_publish(prepared)

        messages = []
        with patch.object(PreparedExport, 'encode', encode_with_external_edit), \
                patch.object(PreparedExport, 'publish', record_publish):
            result, _ = execute_batch(store, state, ['job'], threading.Event(),
                                      lambda *_: None, messages.append, lambda *_: None)
        self.assertIn('error', result, result)
        self.assertEqual(store.load()['jobs'][0]['body'], 'EDIT MADE AFTER THIS EXPORT STARTED')
        self.assertTrue(output.read_bytes() == original,
                        'Existing result was replaced before the profile revision conflict stopped the batch')
        self.assertEqual(published, [],
                         'Export published a stale result before detecting the saved revision conflict')

    def test_serial_export_checks_revision_before_replacing_existing_result(self):
        self.revision_conflict(1)

    def test_parallel_export_revision_protection_control(self):
        self.revision_conflict(2)

    def test_normal_serial_export_control(self):
        store = Store(self.root / 'private')
        state = store.save(self.make_state('normal-output'), 0)
        result, _ = execute_batch(store, state, ['job'], threading.Event(),
                                  lambda *_: None, lambda *_: None, lambda *_: None)
        self.assertEqual((result['completed'], result['failed']), (1, 0), result)
        self.assertTrue((self.root / 'normal-output.mp4').is_file())

    def test_selected_background_source_protection_control(self):
        state = self.make_state(self.source.stem)
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, '覆盖源文件'):
            engine.render_static_text(state['jobs'][0], state, self.root / 'private')
        self.assertEqual(self.source.read_bytes(), original)

    def test_normal_fade_cycle_export_control(self):
        state = self.make_state('normal-looped-output')
        state['settings'].update(unvoiced_duration_mode='fixed', unvoiced_duration_seconds=2.0,
                                 duration_strategy='loop', loop_transition='fade', transition_seconds=0.2)
        output, _ = engine.render_static_text(state['jobs'][0], state, self.root / 'private')
        self.assertTrue(output.is_file())

    def test_fade_cycle_cache_is_leased_during_handoff_to_encoder(self):
        state = self.make_state('looped-output')
        state['settings'].update(unvoiced_duration_mode='fixed', unvoiced_duration_seconds=2.0,
                                 duration_strategy='loop', loop_transition='fade', transition_seconds=0.2)
        real_prepare_cycle = engine.prepare_cycle
        handoff = {}

        def prune_at_handoff(*args, **kwargs):
            cycle, duration = real_prepare_cycle(*args, **kwargs)
            handoff['path'] = str(cycle)
            handoff['leased'] = media_cache_in_use(cycle)
            # Deterministic scheduling of another export's size-limit cleanup.
            # A tiny limit exercises the same eviction path with tiny test media.
            trim_generated_cache(cycle.parent, '.mp4', 1)
            handoff['exists_after_prune'] = cycle.is_file()
            return cycle, duration

        error = None
        with patch.object(engine, 'prepare_cycle', prune_at_handoff):
            try:
                engine.render_static_text(state['jobs'][0], state, self.root / 'private')
            except Exception as caught:
                error = caught
        self.assertTrue(handoff.get('exists_after_prune'),
                        f'Cycle cache was evicted between preparation and lease acquisition: {handoff}; '
                        f'export error: {str(error)[-450:]}')
        self.assertTrue(handoff['leased'])
        self.assertIsNone(error)
        self.assertFalse(media_cache_in_use(handoff['path']))

    def test_failed_cycle_export_releases_cache_lease(self):
        state = self.make_state('failed-looped-output')
        state['settings'].update(unvoiced_duration_mode='fixed', unvoiced_duration_seconds=2.0,
                                 duration_strategy='loop', loop_transition='fade', transition_seconds=0.2)
        real_prepare_cycle = engine.prepare_cycle
        for error in (RuntimeError('synthetic encode failure'), engine.Cancelled('synthetic cancellation')):
            with self.subTest(error=type(error).__name__):
                cycles = []

                def remember_cycle(*args, **kwargs):
                    cycle, duration = real_prepare_cycle(*args, **kwargs)
                    cycles.append(cycle)
                    self.assertTrue(media_cache_in_use(cycle))
                    return cycle, duration

                with patch.object(engine, 'prepare_cycle', remember_cycle), \
                        patch.object(PreparedExport, 'encode', side_effect=error):
                    with self.assertRaises(type(error)):
                        engine.render_static_text(state['jobs'][0], state, self.root / 'private')
                self.assertEqual(len(cycles), 1)
                self.assertFalse(media_cache_in_use(cycles[0]))

    def test_cached_cycle_is_also_protected_during_handoff(self):
        self.test_normal_fade_cycle_export_control()
        self.test_fade_cycle_cache_is_leased_during_handoff_to_encoder()


class SourceProtectionTests(unittest.TestCase):
    def test_publication_checks_sources_again_before_replacing_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, source = root / 'temporary.mp4', root / 'source.mp4'
            target.write_bytes(b'encoded fixture')
            source.write_bytes(b'KEEP ORIGINAL')
            prepared = PreparedExport([], target, source, '', 1, False, None, [], threading.Event(),
                                      None, lambda *_: None, True, 'cpu', False, False, 1,
                                      encoded=True, source_paths=(str(source),))
            with self.assertRaisesRegex(ValueError, '覆盖源文件'):
                prepared.publish()
            self.assertEqual(source.read_bytes(), b'KEEP ORIGINAL')
            self.assertTrue(target.is_file())

    def test_default_libraries_and_inactive_layer_sources_are_protected(self):
        state = fresh_state()
        state.update(backgrounds=['unpaired.mp4'], music=[{'path': 'music.wav'}],
                     image_pool=[{'path': 'image.png'}], copy_pool=[{'path': 'copy-source.mp4'}],
                     resource_pools=[{'items': [{'path': 'custom.mp4'}]}])
        state['jobs'] = [{'path': 'paired.mp4', 'voice_path': 'override.wav', 'task_voice_path': 'task.wav'}]
        state['settings']['layers'] = [{'kind': 'image', 'enabled': False, 'path': 'disabled.png',
                                        'sequence_items': [{'path': 'sequence.mp4'}]}]
        protected = engine.source_paths(state)
        for path in ('unpaired.mp4', 'music.wav', 'image.png', 'copy-source.mp4', 'custom.mp4',
                     'paired.mp4', 'override.wav', 'task.wav', 'disabled.png', 'sequence.mp4'):
            with self.subTest(path=path):
                self.assertIn(path, protected)
                if Path(path).suffix == '.mp4':
                    with self.assertRaisesRegex(ValueError, '覆盖源文件'):
                        engine.safe_output(Path(path).resolve().parent, Path(path).name, protected)

    def test_preflight_checks_unpaired_source_before_starting_renderer(self):
        from app_plugins.builtin.batch_text_video.background import validate_batch_outputs
        state = fresh_state()
        state['backgrounds'] = [str(Path('unpaired.mp4').resolve())]
        state['jobs'] = [{'id': 'job', 'path': str(Path('paired.mp4').resolve()), 'name': 'unpaired'}]
        with self.assertRaisesRegex(ValueError, '覆盖源文件'):
            validate_batch_outputs(state, ['job'])


class SerialSideEffectTests(unittest.TestCase):
    def test_side_effect_nodes_are_not_prepared_ahead_or_replayed_after_failure(self):
        for kind in ('ai_model_client.request', 'core.python_code'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = Store(root / 'private')
                state = fresh_state()
                state['settings'].update(export_workers=3, layers=[])
                state['jobs'] = [dict(id=str(i), path=str(root / f'source-{i}.mp4'), name=f'output-{i}',
                                     status='待生成', title='', body='') for i in range(2)]
                state = store.save(state, 0)
                events = []

                class Effects:
                    bundle = {'jobs': {'0': {'effect': {'graph': {'nodes': [{'kind': kind}]}}}}}

                    def fork(self, **kwargs):
                        return self

                class Prepared:
                    music, sequence_plan = None, []

                    def __init__(self, job):
                        self.job_id = job['id']
                        self.output = root / (job['name'] + '.mp4')

                    def encode(self):
                        events.append(('encode', self.job_id))
                        if self.job_id == '0':
                            raise RuntimeError('synthetic encode failure')

                    def publish(self):
                        events.append(('publish', self.job_id))
                        self.output.write_bytes(b'fixture')
                        return self.output

                @contextmanager
                def prepare(job, *args, **kwargs):
                    events.append(('prepare', job['id']))
                    yield Prepared(job)

                def renderer(*args, **kwargs):
                    raise AssertionError('unsafe combined renderer was used')

                renderer.prepare_export = prepare
                result, _ = execute_batch(store, state, ['0', '1'], threading.Event(), lambda *_: None,
                    lambda *_: None, lambda job_id, _status: events.append(('done', job_id)),
                    {'static_text': renderer}, node_effects=Effects())
                self.assertEqual((result['completed'], result['failed']), (1, 1), result)
                self.assertEqual([item for item in events if item[0] == 'prepare'], [('prepare', '0'), ('prepare', '1')])
                self.assertLess(events.index(('done', '0')), events.index(('prepare', '1')))


if __name__ == '__main__':
    unittest.main()
