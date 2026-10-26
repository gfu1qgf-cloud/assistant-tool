import copy
import os
from pathlib import Path
import tempfile
import threading
from contextlib import contextmanager
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.render_timing import BatchTiming, clock_text, summary, phase
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
import test_batch_parallel_export as parallel_tests


class Clock:
    def __init__(self): self.now = 1.
    def __call__(self): return self.now


class RenderTimingTests(unittest.TestCase):
    def test_parallel_elapsed_is_wall_time_not_sum_of_jobs_and_only_saved_items_count(self):
        clock = Clock(); timing = BatchTiming(['a', 'b'], clock=clock)
        timing.stage('a', '准备素材'); timing.stage('b', '准备素材')
        clock.now = 4; timing.stage('a', '等待编码名额')
        clock.now = 6; timing.stage('a', '编码中'); timing.stage('b', '编码中')
        clock.now = 16; timing.stage('a', '等待保存')
        clock.now = 26; timing.stage('a', '保存成品')
        clock.now = 27
        value = timing.snapshot('a', '已完成', 60)
        self.assertEqual(value['phases'], dict(prepare=3., wait_encode=2., encode=10., wait_save=10., save=1.))
        self.assertEqual(value['elapsed_seconds'], 26)
        timing.done('a', '已完成')
        clock.now = 31; timing.snapshot('b', '已完成', 90); timing.done('b', '已完成')
        result = timing.result(dict(completed=2, failed=0))
        self.assertEqual(result['elapsed_seconds'], 30)
        self.assertEqual(result['output_seconds'], 150)
        self.assertIn('4.00条/分', summary(result)); self.assertIn('15.00秒/条', summary(result))
        self.assertIn('5.00×', summary(result))

    def test_duplicate_stages_retry_and_completion_do_not_double_count(self):
        clock = Clock(); timing = BatchTiming(['a'], clock=clock)
        timing.stage('a', '编码中'); clock.now = 6; timing.stage('a', '编码中')
        clock.now = 11; timing.stage('a', '停滞恢复：CPU重试')
        clock.now = 16; timing.pause('a'); clock.now = 21; timing.stage('a', '准备素材')
        clock.now = 26; timing.done('a', '需处理'); timing.done('a', '已完成')
        result = timing.result(dict(completed=0, failed=1))
        self.assertEqual(result['items']['a']['phases'], dict(encode=15., replay=5., prepare=5.))
        self.assertEqual(result['output_seconds'], 0)
        self.assertEqual(phase('编码等待：45秒未推进'), 'encode')

    def test_progress_100_does_not_count_unsaved_output_or_consume_extra_callbacks(self):
        clock = Clock(); timing = BatchTiming(['a'], clock=clock); calls = []
        update, done = timing.callbacks(lambda *a: calls.append(('progress', a)), lambda *a: calls.append(('done', a)))
        update('a', 100)
        self.assertEqual(timing.snapshot('a')['status'], '')
        done('a', '已完成')
        self.assertEqual(calls, [('progress', ('a', 100)), ('done', ('a', '已完成'))])
        self.assertEqual(clock_text(3661.8), '01:01:01')
        self.assertEqual(clock_text(float('nan')), '00:00:00')

    def fixture(self):
        fixture = parallel_tests.ParallelExportTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        return fixture

    def test_independent_finished_outputs_publish_before_slow_first_without_rerender(self):
        fixture = self.fixture(); fixture.no_resources = True
        # Ordering must not depend on 55ms/120ms sleeps on a loaded Windows
        # machine. Hold job 0 until another result is actually published.
        published = threading.Event(); original_renderer = fixture.renderer
        def renderer():
            render = original_renderer(); original_prepare = render.prepare_export
            @contextmanager
            def prepare(*args, **kwargs):
                with original_prepare(*args, **kwargs) as item:
                    encode, publish = item.encode, item.publish
                    def gated_encode():
                        if item.job['id'] == 'job-0' and not published.wait(5):
                            raise AssertionError('Later independent result could not publish')
                        return encode()
                    def notify_publish():
                        result = publish()
                        if item.job['id'] != 'job-0': published.set()
                        return result
                    item.encode, item.publish = gated_encode, notify_publish
                    yield item
            render.prepare_export = prepare
            return render
        fixture.renderer = renderer
        result, state = fixture.run_batch()
        self.assertEqual(result['completed'], 4)
        self.assertNotEqual(fixture.published[0], 'job-0')
        self.assertTrue(all(count == 1 for count in fixture.attempts.values()))
        self.assertGreater(result['timing']['elapsed_seconds'], 0)
        self.assertTrue(all(job['render_timing']['elapsed_seconds'] > 0 for job in state['jobs']))
        self.assertTrue(all(job['render_timing']['status'] == '已完成' for job in fixture.store.load()['jobs']))

    def test_resource_dependent_outputs_keep_safe_order_and_failure_rotation_rules(self):
        fixture = self.fixture()
        result, state = fixture.run_batch()
        self.assertEqual(fixture.published, ['job-0', 'job-1', 'job-2', 'job-3'])
        self.assertEqual(state['music'][0]['cursor'], 40)
        self.assertEqual(result['timing']['completed'], 4)

    def native_renderer(self, fixture):
        from contextlib import contextmanager
        from app_plugins.builtin.batch_text_video.render_assignment import apply, capture
        from app_plugins.builtin.batch_text_video.components import resolve_sources
        render = fixture.renderer(); original = render.prepare_export
        @contextmanager
        def prepare(job, state, *args, **kwargs):
            bound_job, bound_state = apply(job, state)
            with original(bound_job, bound_state, *args, **kwargs) as item:
                layers, _ = resolve_sources(bound_state['settings']['layers'], job['title'], job['body'])
                item.assignment = capture(bound_job, bound_state, layers, item.music, '')
                yield item
        render.prepare_export = prepare
        return render

    def test_fixed_music_and_pool_assignments_publish_out_of_order_and_retry_without_reallocation(self):
        fixture = self.fixture(); render = self.native_renderer(fixture)
        fixture.fail_encode.add('job-0')
        with patch.object(fixture, 'renderer', return_value=render): result, state = fixture.run_batch()
        self.assertEqual((result['completed'], result['failed']), (3, 1))
        self.assertNotEqual(fixture.published[0], 'job-0')
        self.assertTrue(all(value == 1 for value in fixture.attempts.values()))
        self.assertEqual(state['music'][0]['cursor'], 40)
        self.assertEqual(state['jobs'][0]['render_assignment']['music']['item']['cursor'], 0)
        fixture.fail_encode.clear(); fixture.state = state; fixture.cancel.clear()
        from app_plugins.builtin.batch_text_video.background import execute_batch
        result, state = execute_batch(fixture.store, state, ['job-0'], fixture.cancel, lambda *_: None,
            fixture.messages.append, lambda *_: None, {'static_text': render})
        self.assertEqual((result['completed'], result['failed']), (1, 0))
        self.assertEqual(state['music'][0]['cursor'], 40)
        self.assertEqual(fixture.prepared[-1][1:3], (0, 'COPY-0'))

    def test_failed_allocation_record_is_not_encoded_and_does_not_advance_cursor(self):
        fixture = self.fixture(); render = self.native_renderer(fixture)
        original = fixture.store.save
        def reject(state, revision):
            if any(job.get('render_assignment') for job in state['jobs']):
                raise OSError('synthetic allocation record failure')
            return original(state, revision)
        with patch.object(fixture, 'renderer', return_value=render), patch.object(fixture.store, 'save', reject):
            result, state = fixture.run_batch()
        self.assertEqual(result['completed'], 0)
        self.assertEqual(fixture.peak, 0)
        self.assertEqual(fixture.store.load()['music'][0]['cursor'], 0)

    def test_plan_metrics_survive_reload_without_duplicating_item_details(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); state = fresh_state()
            state['jobs'] = [dict(id='a', path='', name='a', title='', body='', status='待生成')]
            store.save(state, 0)
            plan = store.add_render_plan('default', ['a'])
            store.update_render_plan(plan['id'], 'succeeded', timing=dict(elapsed_seconds=60, completed=2, items={'a': {}}))
            saved = store.load()['render_plans'][0]['render_timing']
            self.assertEqual(saved, dict(elapsed_seconds=60, completed=2))

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Explicit real FFmpeg required')
    def test_real_parallel_encoding_records_phases_duration_and_saved_job_metrics(self):
        from test_batch_encoding_pipeline_real import RealEncodingPipelineTests
        fixture = RealEncodingPipelineTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        store, state = fixture.state('timed', 2)
        result, state = fixture.batch(store, state)
        self.assertAlmostEqual(result['timing']['output_seconds'], 2.4, delta=.05)
        for job in store.load()['jobs']:
            measured = job['render_timing']
            self.assertGreater(measured['phases']['encode'], 0)
            self.assertGreater(measured['phases']['prepare'], 0)
            self.assertGreater(measured['elapsed_seconds'], 0)


class RenderTimingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_live_timer_freezes_at_completion_and_uses_memory_only(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'):
            state = fresh_state(); state['jobs'] = [dict(id='a', path='', name='a', title='', body='', status='待生成')]
            store = Store(root); store.save(state, 0)
            owner = BatchTextVideoDialog(store=store); panel = owner.render_queue_panel.timing
            clock = Clock()
            with patch('app_plugins.builtin.batch_text_video.render_timing_ui.time.monotonic', clock):
                panel.begin('default', ['a'], queued=False)
                panel.stage('default', 'a', '编码中'); clock.now = 31
                panel.stage('default', 'a', '已完成')
                with patch.object(Path, 'stat', side_effect=AssertionError('Timer must not read files')):
                    panel.refresh(); panel.refresh_row(0)
                self.assertIn('2.00条/分', panel.text())
                self.assertEqual(owner.jobs.item(0, 3).text(), '00:00:30')
                panel.finish('default', {'timing': dict(elapsed_seconds=30, completed=1, items={'a': dict(elapsed_seconds=30, phases=dict(encode=30))})})
                before = panel.text(); clock.now = 121; panel.refresh()
                self.assertEqual(panel.text(), before)
                self.assertIn('编码', owner.jobs.item(0, 3).toolTip())
            destination = os.environ.get('LZX_RENDER_TIMING_SCREENSHOT')
            if destination:
                owner.right_tabs.setCurrentWidget(owner.render_queue_panel)
                owner.resize(1400, 900); owner.show(); self.app.processEvents()
                self.assertTrue(owner.grab().save(destination))
            owner.close()

    def test_queued_timer_has_no_fake_speed_and_old_jobs_have_no_fabricated_duration(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'):
            owner = BatchTextVideoDialog(store=Store(root)); panel = owner.render_queue_panel.timing
            panel.begin('default', ['a'])
            self.assertIn('后台排队', panel.text()); self.assertNotIn('条/分', panel.text())
            self.assertIsNone(panel.item_value('default', dict(id='a')))
            owner.close()
