import copy
import io
import threading
import time
import unittest
from unittest.mock import Mock,patch
from app_plugins.builtin.batch_text_video import memory_pressure as memory
from app_plugins.builtin.batch_text_video.render_slots import render_slot
from app_plugins.builtin.batch_text_video.media_errors import MediaMemoryError
from app_plugins.builtin.batch_text_video.engine import Cancelled
from app_plugins.builtin.batch_text_video.render_timing import phase
from test_batch_encoding_stall import EncodingStallTests
from test_batch_parallel_export import ParallelExportTests


def status(free=64):return dict(total_physical=64*memory.GIB,free_physical=free*memory.GIB,total_commit=100*memory.GIB,free_commit=free*memory.GIB)


class AdmissionTests(unittest.TestCase):
    def test_commit_and_physical_headroom_are_both_required(self):
        for key in ('free_physical','free_commit'):
            value=status();value[key]=128*memory.MIB
            with patch.object(memory,'read_status',return_value=value):
                allowed,detail=memory.Admission().request(512*memory.MIB)
                self.assertFalse(allowed);self.assertIn('提交余量',detail)

    def test_burst_reservations_prevent_simultaneous_overcommit(self):
        guard=memory.Admission()
        with patch.object(memory,'read_status',return_value=status(4)),patch.object(memory.time,'monotonic',return_value=10):
            self.assertTrue(guard.request(memory.GIB)[0]);self.assertTrue(guard.request(memory.GIB)[0])
            self.assertFalse(guard.request(memory.GIB)[0])
        with patch.object(memory,'read_status',return_value=status(4)),patch.object(memory.time,'monotonic',return_value=13):
            self.assertTrue(guard.request(memory.GIB)[0])

    def test_unknown_counter_keeps_original_concurrency_and_failure_backoff_is_bounded(self):
        guard=memory.Admission()
        with patch.object(memory,'read_status',return_value=None):self.assertTrue(guard.request(memory.GIB)[0])
        with patch.object(memory,'read_status',return_value=status()),patch.object(memory.time,'monotonic',return_value=10):
            guard.failed();self.assertFalse(guard.request(memory.GIB)[0])
        with patch.object(memory,'read_status',return_value=status()),patch.object(memory.time,'monotonic',return_value=13):
            self.assertTrue(guard.request(memory.GIB)[0])

    def test_real_windows_counter_is_read_only_and_reports_commit_limit(self):
        value=memory.read_windows_status()
        if value is None:self.skipTest('Windows memory counter unavailable')
        self.assertGreater(value['total_physical'],0);self.assertGreater(value['total_commit'],0)
        self.assertGreaterEqual(value['free_commit'],0)
        self.assertLessEqual(value['free_commit'],value['total_commit'])
        self.assertLessEqual(value['free_physical'],value['total_physical'])

    def test_waiting_memory_has_separate_timing_phase(self):
        self.assertEqual(phase('等待内存释放'),'wait_memory')
        self.assertEqual(phase('编码中'),'encode')

    def test_decoder_diagnostics_are_bounded_and_preserve_memory_errors(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.decoder_log import DecoderLog
        process=SimpleNamespace(stderr=io.BytesIO(b'x'*100000+b'Cannot allocate memory'),returncode=1)
        diagnostics=DecoderLog(process)
        try:
            with self.assertRaisesRegex(MediaMemoryError,'可提交内存不足'):diagnostics.check(process,'decode')
            self.assertLessEqual(sum(map(len,diagnostics.parts)),8*4096)
        finally:diagnostics.close()
        self.assertFalse(diagnostics.thread.is_alive())


class EncodeMemoryTests(unittest.TestCase):
    def test_running_encode_can_finish_while_new_encode_waits_then_resumes(self):
        first=threading.Event();release=threading.Event();second=threading.Event();errors=[]
        guard=memory.Admission();progress=Mock();progress.stage=Mock()
        def snapshot():return status(64 if not first.is_set() or release.is_set() else .1)
        def run_one():
            try:
                with render_slot('test-memory-finish',8,memory_bytes=512*memory.MIB):
                    first.set();release.wait(3)
            except BaseException as error:errors.append(error)
        def run_two():
            try:
                with render_slot('test-memory-finish',8,memory_bytes=512*memory.MIB,progress=progress):second.set()
            except BaseException as error:errors.append(error)
        with patch.object(memory,'ADMISSION',guard),patch.object(memory,'read_status',side_effect=snapshot):
            one=threading.Thread(target=run_one);two=threading.Thread(target=run_two)
            one.start();self.assertTrue(first.wait(2));two.start()
            self.assertFalse(second.wait(.15));release.set();one.join(3);two.join(3)
            self.assertTrue(second.is_set());self.assertFalse(one.is_alive());self.assertFalse(two.is_alive())
        self.assertEqual(errors,[]);progress.stage.assert_any_call('等待内存释放')

    def test_cancel_memory_wait_is_prompt_and_never_starts_encode(self):
        token=threading.Event();started=threading.Event();errors=[]
        def run():
            try:
                with render_slot('test-memory-cancel',8,token,memory_bytes=memory.GIB):started.set()
            except BaseException as error:errors.append(error)
        with patch.object(memory,'ADMISSION',memory.Admission()),patch.object(memory,'read_status',return_value=status(.1)):
            thread=threading.Thread(target=run);thread.start();time.sleep(.05);token.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(started.is_set())
        self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],Cancelled)

    def test_memory_error_waits_and_retries_same_prepared_assignment_without_cpu_retry(self):
        prepared=EncodingStallTests().prepared();prepared.render_scope='test-memory-retry'
        prepared.assignment={'sources':{'a':'fixed.mov'}};original=copy.deepcopy(prepared.assignment);args=list(prepared.args)
        gate=Mock();gate.request.return_value=(True,'')
        with patch.object(memory,'ADMISSION',gate),patch.object(prepared,'_encode',side_effect=[MediaMemoryError('系统可提交内存不足'),prepared]) as encode:
            self.assertIs(prepared.encode(),prepared)
        self.assertEqual(encode.call_count,2);gate.failed.assert_called_once()
        self.assertEqual(prepared.assignment,original);self.assertEqual(prepared.args,args)
        prepared.progress.stage.assert_any_call('等待内存释放')

    def test_repeated_allocation_failure_has_finite_retries(self):
        prepared=EncodingStallTests().prepared();gate=Mock();gate.request.return_value=(True,'')
        with patch.object(memory,'ADMISSION',gate),patch.object(prepared,'_encode',side_effect=MemoryError()) as encode:
            with self.assertRaisesRegex(MediaMemoryError,'连续重试'):prepared.encode()
        self.assertEqual(encode.call_count,4);self.assertFalse(prepared.encoded)


class PipelineMemoryTests(ParallelExportTests):
    def test_waiting_for_next_preparation_does_not_block_publishing_finished_encode(self):
        guard=Mock()
        guard.request.side_effect=lambda amount:(not self.prepared or bool(self.published),'waiting for release')
        with patch.object(memory,'ADMISSION',guard):result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(4,0))
        self.assertEqual(self.published[0],'job-0')
        self.assertEqual(self.attempts,dict.fromkeys([f'job-{i}' for i in range(4)],1))
        self.assertIn('waiting for release',self.messages)

    def test_cancel_before_memory_available_does_not_prepare_or_fail_any_job(self):
        guard=Mock()
        def waiting(amount):self.cancel.set();return False,'waiting'
        guard.request.side_effect=waiting
        with patch.object(memory,'ADMISSION',guard):result,state=self.run_batch()
        self.assertTrue(result['cancelled']);self.assertEqual(result['failed'],0)
        self.assertEqual(self.prepared,[]);self.assertEqual(self.done,[])

    def test_transient_native_preparation_memory_error_is_retried_without_failed_record(self):
        original=self.renderer;calls=[0]
        def renderer():
            render=original();prepare=render.prepare_export
            def once(*args,**kwargs):
                calls[0]+=1
                if calls[0]==1:raise MemoryError()
                return prepare(*args,**kwargs)
            render.prepare_export=once;return render
        guard=Mock();guard.request.return_value=(True,'')
        with patch.object(self,'renderer',side_effect=renderer),patch.object(memory,'ADMISSION',guard):result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(4,0));guard.failed.assert_called_once()
        guard.request.assert_any_call(memory.estimate(settings=self.state['settings'])*2)
        self.assertTrue(all(j['status']=='已完成' for j in state['jobs']))


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite([loader.loadTestsFromTestCase(AdmissionTests),loader.loadTestsFromTestCase(EncodeMemoryTests)])
    for name in PipelineMemoryTests.__dict__:
        if name.startswith('test_'):suite.addTest(PipelineMemoryTests(name))
    return suite
