"""Cache lifetime follows the whole batch or player, never worker completion."""
import copy
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video import engine, playback
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.media_cache import MediaCacheSession
from app_plugins.builtin.batch_text_video.music_preparation import MusicPreparationSession, music_session
from app_plugins.builtin.batch_text_video.store import Store, ProfileStore, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class CacheRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_legacy_default_and_global_policy_survive_profile_switch(self):
        store = Store(self.root/'private')
        state = store.save(store.load(), 0)
        self.assertTrue(state['settings']['keep_media_cache'])
        state['render_settings'].pop('keep_media_cache')
        state['settings'].pop('keep_media_cache')
        store.path.write_text(__import__('json').dumps(state), encoding='utf8')
        state = store.load()
        self.assertTrue(state['render_settings']['keep_media_cache'])
        state = store.create_profile(state, 'Other')
        policy = {**state['render_settings'], 'keep_media_cache':False}
        store.save_render_settings(policy)
        for identifier in ('default', state['active_profile']):
            self.assertFalse(ProfileStore(store.directory, identifier).load()['settings']['keep_media_cache'])
        before = store.path.read_bytes()
        with self.assertRaises(ValueError):
            store.save_render_settings({**policy, 'keep_media_cache':0})
        self.assertEqual(store.path.read_bytes(), before)

    def test_global_checkbox_is_in_render_options_and_saves_without_project_undo(self):
        store = Store(self.root/'private')
        with patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(store=store)
            self.addCleanup(dialog.close)
            control = dialog.controls['keep_media_cache']
            self.assertTrue(control.isChecked())
            self.assertTrue(dialog.render_queue_panel.settings_section.isAncestorOf(control))
            control.setChecked(False)
            self.assertTrue(dialog.save_render_settings())
            self.assertFalse(store.load()['settings']['keep_media_cache'])

    def test_private_session_reuses_within_batch_and_removes_only_its_own_files(self):
        root = self.root/'cache'
        root.mkdir()
        original = root/'manual.mov'; original.write_bytes(b'original')
        persistent = root/'video-cache'; persistent.mkdir()
        saved = persistent/('a'*64+'.mp4'); saved.write_bytes(b'previous cache')
        first, other = MediaCacheSession(), MediaCacheSession()
        self.addCleanup(first.close); self.addCleanup(other.close)
        a = first.directory(root, keep=False)
        b = other.directory(root, keep=False)
        self.assertEqual(a, first.directory(root, keep=False))
        self.assertEqual(a, first.directory(self.root/'different-task-folder', keep=False))
        self.assertNotEqual(a, b)
        (a/'video-cache').mkdir(); (a/'video-cache'/'cycle.mp4').write_bytes(b'generated')
        (b/'other.mp4').write_bytes(b'active independent batch')
        first.close()
        self.assertFalse(a.exists())
        self.assertTrue((b/'other.mp4').is_file())
        self.assertEqual(original.read_bytes(), b'original')
        self.assertEqual(saved.read_bytes(), b'previous cache')
        other.close()
        self.assertFalse(b.exists())

    def test_retention_on_preserves_original_cache_location_and_files(self):
        owner = MediaCacheSession()
        root = owner.directory(self.root)
        self.assertEqual(root, self.root)
        file = root/'cached.mp4'; file.write_bytes(b'reusable')
        owner.close()
        self.assertEqual(file.read_bytes(), b'reusable')

    def preview_result(self, *, error=False, canceled=False):
        state = fresh_state(); state['settings']['keep_media_cache'] = False
        job = dict(path='', title='', body='', name='Example')
        before = copy.deepcopy((state, job))
        worker = playback.PlaybackWorker(job, state, self.root, 'a'*64)
        results = []; worker.ready.connect(results.append)
        if canceled: worker.cancel.set()
        def render(job, state, directory, *args, **kwargs):
            output = Path(job['task_dir'])/(job['name']+'.mp4')
            output.write_bytes(b'preview')
            if error: raise RuntimeError('fixture failure after file creation')
            return output, None
        with patch.object(playback, 'resolve_tools', return_value=('ffmpeg','ffprobe')), \
                patch.object(playback, 'probe', return_value={'duration':1}), \
                patch.object(playback, 'render_static_text', side_effect=render):
            worker.run()
        self.assertEqual((state, job), before)
        return results[0]

    def test_preview_survives_worker_end_and_is_removed_after_return_to_editor(self):
        result = self.preview_result()
        self.assertNotIn('error', result)
        output = Path(result['path']); self.assertTrue(output.is_file())
        from test_batch_playback_blur import FakePlayer
        with patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(store=Store(self.root/'private'))
            self.addCleanup(dialog.close)
            dialog._playback_key = result['key']
            with patch('model.MediaPlayback.FfplayReviewSurface', FakePlayer):
                dialog._playback_ready(result)
            player = dialog.playback_surface
            self.assertTrue(player.is_playing())
            self.assertTrue(output.is_file())
            # Pausing keeps seek/replay available while this preview is open.
            player.pause(); self.assertTrue(output.is_file())
            dialog.return_to_editor()
            self.assertTrue(player.released)
            self.assertFalse(output.exists())
            self.assertEqual(dialog._playback_file, '')

    def test_stale_preview_completion_releases_its_disposable_files(self):
        result = self.preview_result()
        output = Path(result['path']); self.assertTrue(output.exists())
        with patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(store=Store(self.root/'private'))
            self.addCleanup(dialog.close)
            dialog._playback_key = 'different'
            dialog._playback_ready(result)
        self.assertFalse(output.exists())

    def test_failed_or_canceled_preview_removes_partial_cache(self):
        for options in ({'error':True}, {'canceled':True}):
            with self.subTest(options=options):
                result = self.preview_result(**options)
                self.assertTrue(result.get('error') or result.get('cancelled'))
                self.assertNotIn('cache_owner', result)
                self.assertFalse(list(self.root.glob('temporary-*')))

    def test_player_initialization_failure_releases_preview_cache(self):
        result = self.preview_result()
        output = Path(result['path'])
        with patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(store=Store(self.root/'private'))
            self.addCleanup(dialog.close)
            dialog._playback_key = result['key']
            with patch('model.MediaPlayback.FfplayReviewSurface', side_effect=RuntimeError('fixture decoder error')):
                dialog._playback_ready(result)
        self.assertFalse(output.exists())

    def test_direct_preparation_keeps_inputs_until_context_exit_including_failure_and_cancel(self):
        from app_plugins.builtin.batch_text_video.music_preparation import current_music_session
        state = fresh_state(); state['settings']['keep_media_cache'] = False
        for exception in (None, RuntimeError, engine.Cancelled):
            roots = []
            @contextmanager
            def prepare(*args, **kwargs):
                session = current_music_session()
                self.assertIsNotNone(session)
                root = session.media_cache.directory(self.root, keep=False)
                (root/'cycle.mp4').write_bytes(b'input used by encoder')
                roots.append(root)
                yield root/'cycle.mp4'
            def run():
                with patch.object(engine, '_prepare_static_text', prepare):
                    with engine.prepare_static_text({}, state, self.root) as prepared:
                        self.assertTrue(prepared.is_file())
                        if exception: raise exception('fixture interruption')
            if exception:
                with self.assertRaises(exception): run()
            else:
                run()
            self.assertEqual(len(roots), 1)
            self.assertFalse(roots[0].exists())
            self.assertIsNone(current_music_session())

    def test_music_workers_finish_before_disposable_cache_is_removed(self):
        entered, release = threading.Event(), threading.Event()
        with MusicPreparationSession(threading.Event(), 1) as session:
            root = session.media_cache.directory(self.root, keep=False)
            def work():
                (root/'music.flac').write_bytes(b'music')
                entered.set()
                self.assertTrue(release.wait(3))
                self.assertTrue((root/'music.flac').is_file())
            future = session.executor.submit(work)
            self.assertTrue(entered.wait(3))
            self.assertTrue(root.exists())
            release.set(); future.result(3)
        self.assertFalse(root.exists())

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'Explicit real FFmpeg required')
    def test_real_parallel_exports_share_one_cycle_then_remove_it_keep_outputs_and_sources(self):
        source = self.root/'source.mp4'
        subprocess.run([FFMPEG, '-y', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'testsrc2=s=160x240:r=10:d=0.6', '-an', '-c:v', 'libx264', str(source)],
            check=True, capture_output=True, timeout=30)
        source_bytes = source.read_bytes()
        for keep in (False, True):
            with self.subTest(keep=keep):
                store = Store(self.root/('private-'+str(keep)))
                state = fresh_state()
                state['settings'].update(ffmpeg_path=FFMPEG, aspect='source', auto_crop_black_bars=False,
                    output_dir=str(self.root/('result-'+str(keep))), export_workers=2,
                    keep_media_cache=keep, unvoiced_duration_mode='fixed', unvoiced_duration_seconds=1.0,
                    transition_seconds=0.1, music_enabled=False, layers=normalize_layers([]))
                from app_plugins.builtin.batch_text_video.components import new_text_component
                label = new_text_component()
                label.update(source='fixed', text='OK', font_min=24, font_max=24)
                state['settings']['layers'] = normalize_layers([label])
                state['jobs'] = [dict(id=str(i), path=str(source), task_dir=str(self.root/('result-'+str(keep))/str(i)), name='result-'+str(i), title='', body='',
                    status='待生成', output='', error='') for i in range(2)]
                state = store.save(state, 0)
                created, input_files = [], []
                original = engine.prepare_cycle
                def cycle(*args, **kwargs):
                    result = original(*args, **kwargs)
                    created.append(result[0]); return result
                from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
                original_encode = PreparedExport.encode
                def encode(prepared):
                    files = [Path(prepared.args[i+1]) for i, arg in enumerate(prepared.args[:-1])
                             if arg == '-i' and 'video-cache' in prepared.args[i+1]]
                    self.assertTrue(files)
                    self.assertTrue(all(file.is_file() for file in files))
                    input_files.extend(files)
                    return original_encode(prepared)
                def external_cache(preferred, *, anchors, namespace):
                    return Path(anchors[0])/'.辅助工具媒体缓存'/namespace
                with patch.object(engine, 'prepare_cycle', side_effect=cycle), patch.object(PreparedExport, 'encode', encode), \
                        patch('model.MediaStorage.media_cache_root', side_effect=external_cache):
                    result, saved = execute_batch(store, state, ['0','1'], threading.Event(),
                        lambda *_:None, lambda *_:None, lambda *_:None)
                self.assertEqual((result['completed'],result['failed']), (2,0), result)
                self.assertEqual(len(created), 2)
                self.assertEqual(created[0] == created[1], not keep)
                self.assertEqual(created[0].exists(), keep)
                self.assertEqual(len(input_files), 2)
                self.assertTrue(all(Path(path).is_file() for path in result['paths']))
                self.assertFalse(list(store.directory.glob('temporary-*')))
                self.assertNotIn('temporary-', __import__('json').dumps(saved['jobs']))
                self.assertEqual(source.read_bytes(), source_bytes)


if __name__ == '__main__':
    unittest.main()
