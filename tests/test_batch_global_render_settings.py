import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app_plugins.builtin.batch_text_video.store import Store, ProfileStore
from app_plugins.builtin.batch_text_video.background import batch_resources, run_batch_video_job
from app_plugins.builtin.batch_text_video.render_slots import configure, render_slot
from model.BackgroundTasks import resources_conflict
from model.AppDataStorage import data_file
from model.MediaCacheLeases import media_cache_build


class GlobalRenderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'project')

    def job(self, identifier):
        folder = self.root/identifier
        folder.mkdir(exist_ok=True)
        return dict(id=identifier, path=str(self.root/'background.mp4'), task_dir=str(folder),
                    name=identifier, title='Nádej', body='Pokoj.', status='待生成', output='', error='')

    def two_profiles(self):
        state = self.store.load()
        state['jobs'] = [self.job('a')]
        state = self.store.save(state, 0)
        state = self.store.create_profile(state, '第二项目')
        second = state['active_profile']
        state['jobs'] = [self.job('b')]
        state = self.store.save(state, state['revision'])
        self.store.select_profile(state, 'default')
        return second

    def test_legacy_project_encoding_options_do_not_override_the_global_policy(self):
        second = self.two_profiles()
        legacy = self.store.load()
        legacy.pop('render_settings')
        legacy['settings'].update(video_encoder='nvenc', export_workers=1, export_quality='medium')
        legacy['profiles'][1]['data']['settings'].update(video_encoder='qsv', export_workers=8, export_quality='low')
        data_file(self.store.path).write_json(legacy)
        migrated = self.store.load()
        self.assertEqual(migrated['render_settings']['export_workers'], 2)
        self.assertEqual(migrated['render_settings']['video_encoder'], 'nvenc')
        switched = self.store.select_profile(migrated, second)
        self.assertEqual(switched['settings']['video_encoder'], 'nvenc')
        self.assertEqual(switched['settings']['export_workers'], 2)
        self.assertEqual(switched['settings']['export_quality'], 'medium')

    def test_global_edit_survives_running_profile_commit_and_applies_to_every_profile(self):
        second = self.two_profiles()
        worker = ProfileStore(self.store.directory, second)
        frozen = worker.load()
        policy = {**frozen['render_settings'], 'video_encoder':'qsv', 'export_workers':4, 'export_quality':'low'}
        self.store.save_render_settings(policy)
        output = self.root/'finished.mp4'
        output.write_bytes(b'video')
        worker.completed(frozen, 'b', output, None)
        current = self.store.load()
        self.assertEqual(current['render_settings'], policy)
        for identifier in ('default', second):
            data = ProfileStore(self.store.directory, identifier).load()
            for key, value in policy.items():
                self.assertEqual(data['settings'][key], value)
        self.assertEqual(frozen['settings']['video_encoder'], 'cpu')

    def test_only_the_same_project_or_output_directory_conflicts(self):
        second = self.two_profiles()
        first = ProfileStore(self.store.directory, 'default').load()
        other = ProfileStore(self.store.directory, second).load()
        left = batch_resources(self.store, first, ['a'])
        right = batch_resources(self.store, other, ['b'])
        self.assertFalse(resources_conflict(left, right))
        self.assertTrue(resources_conflict(left, batch_resources(self.store, first, ['a'])))
        other['jobs'][0]['task_dir'] = first['jobs'][0]['task_dir']
        self.assertTrue(resources_conflict(left, batch_resources(self.store, other, ['b'])))

    def test_one_global_budget_bounds_cross_project_encoding_and_waiting_can_cancel(self):
        scope = str(self.root/'budget')
        configure(scope, 2)
        release, two_inside = threading.Event(), threading.Event()
        state_lock = threading.Lock()
        active = peak = 0
        def encode(cancel):
            nonlocal active, peak
            with render_slot(scope, 8, cancel):
                with state_lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 2:
                        two_inside.set()
                try:
                    if not release.wait(3):
                        raise TimeoutError('测试编码未释放')
                finally:
                    with state_lock:
                        active -= 1
            return True
        with ThreadPoolExecutor(max_workers=4) as executor:
            first = executor.submit(encode, threading.Event())
            second = executor.submit(encode, threading.Event())
            self.assertTrue(two_inside.wait(3))
            canceled = threading.Event()
            waiting = executor.submit(encode, canceled)
            fourth = executor.submit(encode, threading.Event())
            canceled.set()
            from app_plugins.builtin.batch_text_video.engine import Cancelled
            try:
                with self.assertRaises(Cancelled):
                    waiting.result(3)
            finally:
                release.set()
            self.assertTrue(first.result(3))
            self.assertTrue(second.result(3))
            self.assertTrue(fourth.result(3))
        self.assertEqual(peak, 2)

    def test_cache_build_locks_are_key_scoped(self):
        held, released = threading.Event(), threading.Event()
        same_entered, other_entered = threading.Event(), threading.Event()
        def first():
            with media_cache_build(self.root/'same.flac'):
                held.set()
                if not released.wait(3):
                    raise TimeoutError('测试缓存未释放')
        def enter(path, flag):
            with media_cache_build(path):
                flag.set()
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(first)]
            self.assertTrue(held.wait(3))
            futures += [executor.submit(enter, self.root/'same.flac', same_entered),
                        executor.submit(enter, self.root/'other.flac', other_entered)]
            try:
                self.assertTrue(other_entered.wait(3))
                self.assertFalse(same_entered.is_set())
            finally:
                released.set()
            for future in futures:
                future.result(3)
        self.assertTrue(same_entered.is_set())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG') and Path(os.environ.get('NODE_FFMPEG','')).is_file(),
                         '需要本地FFmpeg短片')
    def test_four_one_video_projects_enter_real_encoders_together(self):
        from qt_compat import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ffmpeg = os.environ['NODE_FFMPEG']
        source = self.root/'background.mp4'
        subprocess.run([ffmpeg, '-y', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=160x240:r=10:d=0.4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
            check=True, timeout=15, capture_output=True)
        profiles = []
        for number in range(4):
            state = self.store.load()
            if number: state = self.store.create_profile(state, f'项目{number}')
            identifier, job_id = state['active_profile'], f'job-{number}'
            profiles.append((identifier, job_id))
            state['jobs'] = [self.job(job_id)]
            state['settings'].update(layers=[], aspect='source', use_voice=False, music_enabled=False,
                                      auto_crop_black_bars=False, unvoiced_duration_mode='source')
            self.store.save(state, state['revision'])
        self.store.save_render_settings({**self.store.load()['render_settings'], 'export_workers':4,
                                         'ffmpeg_path':ffmpeg, 'video_encoder':'cpu'})
        from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
        original = PreparedExport._encode
        barrier = threading.Barrier(4)
        seen, phases = [], []
        lock = threading.Lock()
        def encode(prepared):
            with lock: seen.append(prepared.workers)
            barrier.wait(10)
            return original(prepared)
        def run(identifier, job_id):
            state = ProfileStore(self.store.directory, identifier).load()
            context = SimpleNamespace(is_canceled=lambda:False, report=lambda *_:None, log=lambda *_:None,
                report_item_stage=lambda item, phase: phases.append((item, phase)))
            return run_batch_video_job(dict(store_dir=str(self.store.directory), profile_id=identifier,
                                            state=state, ids=[job_id]), context)
        with patch.object(PreparedExport, '_encode', encode), ThreadPoolExecutor(max_workers=4) as executor:
            results = [f.result(30) for f in [executor.submit(run, *profile) for profile in profiles]]
        self.assertEqual([r['completed'] for r in results], [1,1,1,1])
        self.assertEqual(seen, [4,4,4,4])
        self.assertEqual(sum(phase == '编码中' for _, phase in phases), 4)
        for identifier, _ in profiles:
            self.assertEqual(ProfileStore(self.store.directory, identifier).load()['jobs'][0]['status'], '已完成')

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG') and Path(os.environ.get('NODE_FFMPEG','')).is_file(),
                         '需要本地FFmpeg短片')
    def test_two_real_projects_encode_together_and_commit_independent_results(self):
        from qt_compat import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        from app_plugins.builtin.batch_text_video.components import new_text_component
        ensure_fonts()
        ffmpeg = os.environ['NODE_FFMPEG']
        source, music = self.root/'background.mp4', self.root/'music.wav'
        subprocess.run([ffmpeg, '-y', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=320x240:r=25:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
        subprocess.run([ffmpeg, '-y', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=2:sample_rate=48000', str(music)], check=True)
        second = self.two_profiles()
        for identifier in ('default', second):
            scoped = ProfileStore(self.store.directory, identifier)
            state = scoped.load()
            text = new_text_component()
            text.update(source='fixed', text='Nádej A' if identifier == 'default' else 'Pokoj B',
                        width=90, height=60, font_min=24, font_max=42)
            state['settings'].update(layers=[text], aspect='source', use_voice=False, skip_silence=False,
                                     music_enabled=True, unvoiced_duration_mode='source')
            state['music'] = [dict(id='music', path=str(music), cursor=0., audio_key='', last_used='')]
            scoped.save(state, state['revision'])
        policy = {**self.store.load()['render_settings'], 'ffmpeg_path':ffmpeg, 'overwrite':True, 'export_workers':2}
        self.store.save_render_settings(policy)
        from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
        original = PreparedExport._encode
        overlap = threading.Barrier(2)
        def simultaneous(prepared):
            overlap.wait(10)
            return original(prepared)
        def run(identifier, job_id):
            state = ProfileStore(self.store.directory, identifier).load()
            context = SimpleNamespace(is_canceled=lambda:False, report=lambda *_:None, log=lambda *_:None)
            return run_batch_video_job(dict(store_dir=str(self.store.directory), profile_id=identifier,
                                            state=state, ids=[job_id]), context)
        with patch.object(PreparedExport, '_encode', simultaneous), ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(run, 'default', 'a'), executor.submit(run, second, 'b')]
            results = [future.result(45) for future in jobs]
        self.assertEqual([result['completed'] for result in results], [1,1])
        frame_hashes = []
        for identifier in ('default', second):
            state = ProfileStore(self.store.directory, identifier).load()
            self.assertEqual(state['jobs'][0]['status'], '已完成')
            self.assertTrue(Path(state['jobs'][0]['output']).is_file())
            self.assertGreater(state['music'][0]['cursor'], .9)
            frame_hashes.append(subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-i',
                state['jobs'][0]['output'], '-map', '0:v', '-frames:v', '1', '-f', 'hash', '-hash', 'md5', '-'],
                check=True, stdout=subprocess.PIPE).stdout)
        self.assertNotEqual(frame_hashes[0], frame_hashes[1])
