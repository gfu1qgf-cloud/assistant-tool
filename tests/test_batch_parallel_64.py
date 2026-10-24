"""64 is a real shared scheduling limit, with bounded per-encoder threads."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from model.MediaEncoding import MAX_EXPORT_WORKERS, normalize_export_settings, video_encoding_args, filter_thread_args
from app_plugins.builtin.batch_text_video.render_slots import configure, render_slot
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.store import Store, fresh_state, file_identity


class Parallel64Tests(unittest.TestCase):
    def test_policy_accepts_64_rejects_65_and_bounds_each_encoder_threads(self):
        self.assertEqual(MAX_EXPORT_WORKERS, 64)
        for workers in (1, 8, 16, 32, 64):
            settings = {'export_workers': workers}; normalize_export_settings(settings)
            self.assertEqual(settings['export_workers'], workers)
        for value in (0, 65, True, 8.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_export_settings({'export_workers': value})
        with patch('model.MediaEncoding.os.cpu_count', return_value=32):
            args = video_encoding_args('cpu', workers=64)
            self.assertEqual(args[args.index('-threads')+1], '1')
            self.assertEqual(filter_thread_args(64), ['-filter_threads', '1', '-filter_complex_threads', '1'])

    def test_shared_gate_can_expand_from_8_to_64_without_starving_waiters(self):
        scope = '64-regression-'+uuid.uuid4().hex
        gate = configure(scope, 8); condition = threading.Condition()
        release = threading.Event(); cancel = threading.Event()
        active = peak = entered = 0
        def encode():
            nonlocal active, peak, entered
            with render_slot(scope, 64, cancel):
                with condition:
                    active += 1; entered += 1; peak = max(peak, active); condition.notify_all()
                try: release.wait(10)
                finally:
                    with condition: active -= 1; condition.notify_all()
        with ThreadPoolExecutor(max_workers=64) as pool:
            futures = [pool.submit(encode) for _ in range(64)]
            try:
                with condition:
                    self.assertTrue(condition.wait_for(lambda: entered == 8, timeout=5))
                self.assertEqual(gate.limit, 8)
                configure(scope, 64)
                with condition:
                    self.assertTrue(condition.wait_for(lambda: entered == 64, timeout=5))
                self.assertEqual(peak, 64)
                configure(scope, 4)
                self.assertEqual(gate.limit, 4)
            finally: release.set()
            for future in futures: future.result(timeout=5)
        self.assertEqual(gate.active, 0)
        self.assertEqual(len(gate.waiters), 0)

    def test_72_jobs_reach_64_encoders_and_publish_once_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = Store(root/'private'); state = fresh_state()
            state['settings'].update(export_workers=64, music_enabled=False, output_dir=str(root/'out'), layers=[])
            state['jobs'] = [dict(id=str(i), path='', name=f'video-{i}', title='', body='',
                status='待生成', output='', error='') for i in range(72)]
            state = store.save(state, 0)
            condition = threading.Lock(); barrier = threading.Barrier(64)
            active = peak = 0; published = []; messages = []
            scope = file_identity(store.path)
            class Prepared:
                def __init__(self, job, token):
                    self.job, self.token = job, token
                    self.output = root/(job['name']+'.mp4'); self.target = root/(job['name']+'.temporary')
                    self.music = None; self.sequence_plan = []
                def encode(self):
                    nonlocal active, peak
                    with render_slot(scope, 64, self.token):
                        with condition: active += 1; peak = max(peak, active)
                        try:
                            if int(self.job['id']) < 64: barrier.wait(timeout=8)
                            self.target.write_bytes(self.job['id'].encode())
                        finally:
                            with condition: active -= 1
                    return self
                def publish(self):
                    published.append(self.job['id']); self.target.replace(self.output); return self.output
            @contextmanager
            def prepare(job, snapshot, cache, token, *args, **kwargs):
                self.assertEqual(snapshot['settings']['export_workers'], 64)
                yield Prepared(job, token)
            def renderer(*args, **kwargs): raise AssertionError('prepared pipeline required')
            renderer.prepare_export = prepare
            result, final = execute_batch(store, state, list(map(str, range(72))), threading.Event(),
                lambda *_: None, messages.append, lambda *_: None, {'static_text': renderer})
            self.assertEqual((result['completed'], result['failed']), (72, 0), (result, messages[-8:]))
            self.assertNotIn('error', result)
            self.assertEqual(peak, 64)
            self.assertEqual(published, list(map(str, range(72))))
            self.assertTrue(all(Path(path).is_file() for path in result['paths']))
            self.assertTrue(all(job['status'] == '已完成' for job in final['jobs']))
            print('64-worker scheduler: peak=64, ordered published=72', flush=True)
