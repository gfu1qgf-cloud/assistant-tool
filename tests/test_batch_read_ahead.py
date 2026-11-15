from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from app_plugins.builtin.batch_text_video import preparation_budget as budget
from app_plugins.builtin.batch_text_video import memory_pressure as memory
from app_plugins.builtin.batch_text_video.engine import Cancelled
from test_batch_parallel_export import ParallelExportTests


def status(free=64,commit=64):
    return dict(total_physical=64*budget.GIB,free_physical=int(free*budget.GIB),
        total_commit=128*budget.GIB,free_commit=int(commit*budget.GIB))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.guard=budget.PreparationBudget()
        self.memory=patch.object(memory,'read_status',return_value=status());self.memory.start();self.addCleanup(self.memory.stop)
        self.disk=patch.object(budget.shutil,'disk_usage',return_value=SimpleNamespace(free=100*budget.GIB));self.disk.start();self.addCleanup(self.disk.stop)

    def request(self,amount=512*budget.MIB):return self.guard.request(amount,self.root)[0]

    def test_global_buffer_has_finite_capacity_and_idempotent_release(self):
        tickets=[self.request() for _ in range(budget.MAX_AHEAD)]
        self.assertTrue(all(tickets));self.assertIsNone(self.request())
        tickets[0].release();tickets[0].release()
        replacement=self.request();self.assertIsNotNone(replacement)
        for ticket in tickets[1:]+[replacement]:ticket.release()
        self.assertEqual(self.guard.held,{})

    def test_low_physical_or_commit_headroom_pauses_extra_preparation(self):
        for value in (status(.2),status(64,.2)):
            with patch.object(memory,'read_status',return_value=value):self.assertIsNone(self.request())
        self.assertIsNotNone(self.request())

    def test_low_or_unreadable_cache_disk_does_not_admit_extra_work(self):
        with patch.object(budget.shutil,'disk_usage',return_value=SimpleNamespace(free=budget.GIB)):
            self.assertIsNone(self.request())
        with patch.object(budget.shutil,'disk_usage',side_effect=OSError('synthetic missing volume')):
            self.assertIsNone(self.request())

    def test_unknown_memory_keeps_the_original_just_in_time_path(self):
        with patch.object(memory,'read_status',return_value=None):self.assertIsNone(self.request())

    def test_large_caption_resource_estimates_reduce_the_buffer(self):
        self.assertIsNotNone(self.request(3*budget.GIB))
        self.assertIsNone(self.request(2*budget.GIB))

    def test_concurrent_projects_share_one_atomic_limit(self):
        results=[];lock=threading.Lock();barrier=threading.Barrier(16)
        def request():
            barrier.wait();ticket=self.request()
            with lock:results.append(ticket)
        threads=[threading.Thread(target=request) for _ in range(16)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3);self.assertFalse(thread.is_alive())
        self.assertEqual(sum(v is not None for v in results),budget.MAX_AHEAD)
        for ticket in results:
            if ticket is not None:ticket.release()
        self.assertFalse(self.guard.held)

    def test_prepared_queue_wait_does_not_inflate_actual_preparation_time(self):
        from app_plugins.builtin.batch_text_video.render_timing import phase
        self.assertEqual(phase('已准备待渲染'),'wait_encode')
        self.assertEqual(phase('等待预准备资源'),'wait_prepare')
        self.assertEqual(phase('预准备 · 准备素材'),'prepare')


class ReadAheadPipelineTests(ParallelExportTests):
    def setUp(self):
        super().setUp();self.no_resources=True
        self.guard=budget.PreparationBudget()
        for target,name,value in ((budget,'BUFFER',self.guard),(memory,'read_status',lambda:status()),
                (budget.shutil,'disk_usage',lambda *_:SimpleNamespace(free=100*budget.GIB))):
            context=patch.object(target,name,value);context.start();self.addCleanup(context.stop)

    def gated_renderer(self,workers=1,release_job='job-1',cancel_job=None):
        self.state['settings']['export_workers']=workers
        renderer=super().renderer();original=renderer.prepare_export;ready=threading.Event()
        outer=self
        @contextmanager
        def prepare(job,*args,**kwargs):
            with original(job,*args,**kwargs) as prepared:
                if job['id']==release_job:ready.set()
                if job['id']==cancel_job:outer.cancel.set()
                encode=prepared.encode
                if int(job['id'].split('-')[-1])<workers:
                    def blocked():
                        deadline=time.monotonic()+5
                        while not ready.wait(.01):
                            if prepared.token.is_set():raise Cancelled('cancel gated encode')
                            if time.monotonic()>deadline:raise AssertionError('Encoder filled but queued tasks were never prepared')
                        return encode()
                    prepared.encode=blocked
                yield prepared
        renderer.prepare_export=prepare
        return renderer,ready

    def run_renderer(self,renderer,*,effects=None,binding=None):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        stages=[]
        def progress(*_):pass
        progress.stage=lambda *values:stages.append(values)
        result,state=execute_batch(self.store,self.state,[j['id'] for j in self.state['jobs']],self.cancel,
            progress,self.messages.append,lambda *v:self.done.append(v),{'static_text':renderer},node_effects=effects,elevenlabs_binding=binding)
        self.assertEqual(self.guard.held,{},self.messages[-3:])
        return result,state,stages

    def test_single_encoder_prepares_following_tasks_before_first_encode_finishes(self):
        renderer,ready=self.gated_renderer()
        result,state,stages=self.run_renderer(renderer)
        self.assertTrue(ready.is_set());self.assertEqual((result['completed'],result['failed']),(4,0),result)
        self.assertEqual(self.peak,1);self.assertEqual(len(set(self.preparing_threads)),1)
        self.assertTrue(any('预准备' in v[-1] for v in stages),stages)
        self.assertTrue(any(v[-1]=='已准备待渲染' for v in stages))

    def test_full_encoder_pool_keeps_preparing_without_adding_encoding_threads(self):
        renderer,ready=self.gated_renderer(workers=2,release_job='job-2')
        result,state,_=self.run_renderer(renderer)
        self.assertTrue(ready.is_set());self.assertEqual(result['completed'],4,result)
        self.assertEqual(self.peak,2);self.assertEqual(len(set(self.preparing_threads)),1)

    def test_resource_pressure_pauses_only_lookahead_and_normal_encodes_continue(self):
        self.state['settings']['export_workers']=1
        renderer=super().renderer();original=renderer.prepare_export;attempted=threading.Event()
        def denied(*_):attempted.set();return None,'synthetic full buffer'
        @contextmanager
        def prepare(job,*args,**kwargs):
            with original(job,*args,**kwargs) as prepared:
                encode=prepared.encode
                if job['id']=='job-0':
                    def blocked():
                        self.assertTrue(attempted.wait(3));self.assertEqual(len(self.prepared),1)
                        return encode()
                    prepared.encode=blocked
                yield prepared
        renderer.prepare_export=prepare
        with patch.object(self.guard,'request',side_effect=denied):result,state,_=self.run_renderer(renderer)
        self.assertEqual((result['completed'],result['failed']),(4,0),result)

    def test_failed_ahead_preparation_releases_budget_and_remaining_tasks_continue(self):
        renderer,_=self.gated_renderer(release_job='job-2');self.fail_prepare.add('job-1')
        result,state,_=self.run_renderer(renderer)
        self.assertEqual((result['completed'],result['failed']),(3,1),result)

    def test_cancellation_discards_queued_contexts_and_releases_all_tickets(self):
        renderer,_=self.gated_renderer(release_job='never',cancel_job='job-1')
        result,state,_=self.run_renderer(renderer)
        self.assertTrue(result['cancelled']);self.assertEqual(result['completed'],0)
        self.assertFalse(self.published)

    def test_preparation_callback_failure_does_not_leak_an_unclaimed_ticket(self):
        renderer,_=self.gated_renderer(release_job='never')
        original=self.messages
        class Messages(list):
            def append(self,text):
                if text.startswith('[2/4] 准备'):raise OSError('synthetic coordinator failure')
                super().append(text)
        self.messages=Messages()
        result,state,_=self.run_renderer(renderer)
        self.assertIn('synthetic coordinator failure',result.get('error',''))
        self.assertEqual(result['completed'],0)

    def test_paid_or_code_workflows_keep_the_original_single_task_boundary(self):
        renderer=super().renderer();original=renderer.prepare_export
        @contextmanager
        def prepare(job,*args,**kwargs):
            with original(job,*args,**kwargs) as prepared:
                encode=prepared.encode
                def check():
                    self.assertEqual(len(self.prepared),int(job['id'].split('-')[-1])+1)
                    return encode()
                prepared.encode=check
                yield prepared
        renderer.prepare_export=prepare
        effects=SimpleNamespace(bundle={'jobs':{'job':{'effect':{'graph':{'nodes':[{'kind':'ai_model_client.request'}]}}}}},fork=lambda **_:SimpleNamespace())
        with patch.object(self.guard,'request',side_effect=AssertionError('Side-effect workflow must not read ahead')):
            result,state,_=self.run_renderer(renderer,effects=effects)
        self.assertEqual((result['completed'],result['failed']),(4,0),result)
        self.assertEqual(self.peak,1)

    def test_cancelled_native_lookahead_reuses_frozen_music_and_sources_on_retry(self):
        from app_plugins.builtin.batch_text_video.render_assignment import apply,capture
        from app_plugins.builtin.batch_text_video.components import resolve_sources
        self.no_resources=False;self.state['settings']['export_workers']=1
        def native(cancel_after=None):
            renderer=super(ReadAheadPipelineTests,self).renderer();original=renderer.prepare_export
            @contextmanager
            def prepare(job,state,*args,**kwargs):
                job,state=apply(job,state)
                with original(job,state,*args,**kwargs) as prepared:
                    layers,_=resolve_sources(state['settings']['layers'],job['title'],job['body'],job=job)
                    prepared.assignment=capture(job,state,layers,prepared.music,'')
                    if cancel_after and job['id']=='job-0':
                        def blocked():
                            while not prepared.token.is_set():time.sleep(.005)
                            raise Cancelled('cancel native lookahead')
                        prepared.encode=blocked
                    if job['id']==cancel_after:self.cancel.set()
                    yield prepared
            renderer.prepare_export=prepare;return renderer
        result,state,_=self.run_renderer(native('job-2'))
        self.assertTrue(result['cancelled']);self.assertFalse(self.published)
        assigned={j['id']:j['render_assignment'] for j in state['jobs'] if j.get('render_assignment')}
        self.assertGreaterEqual(len(assigned),2)
        self.state=state;self.cancel.clear();self.prepared.clear()
        result,state,_=self.run_renderer(native())
        self.assertEqual((result['completed'],result['failed']),(4,0),result)
        self.assertEqual(state['music'][0]['cursor'],40)
        self.assertEqual(state['settings']['layers'][0]['sequence_cursor'],1)
        for identifier,selection in assigned.items():
            value=next(v for v in self.prepared if v[0]==identifier)
            self.assertEqual(value[1],selection['music']['item']['cursor'])
            self.assertIn(value[2],[v['value'] for v in selection['sources'].values()])

    def test_paid_cloud_alignment_keeps_the_existing_preparation_boundary(self):
        self.state['settings']['export_workers']=1
        renderer=super().renderer();original=renderer.prepare_export
        @contextmanager
        def prepare(job,*args,**kwargs):
            with original(job,*args,**kwargs) as prepared:
                encode=prepared.encode
                def check():
                    self.assertEqual(len(self.prepared),int(job['id'].split('-')[-1])+1)
                    return encode()
                prepared.encode=check;yield prepared
        renderer.prepare_export=prepare
        with patch.object(self.guard,'request',side_effect=AssertionError('Cloud binding must not start extra paid work')):
            result,state,_=self.run_renderer(renderer,binding='synthetic-offline-session')
        self.assertEqual((result['completed'],result['failed']),(4,0),result)


def load_tests(loader,tests,pattern):
    suite=loader.loadTestsFromTestCase(BudgetTests)
    for name in ReadAheadPipelineTests.__dict__:
        if name.startswith('test_'):suite.addTest(ReadAheadPipelineTests(name))
    return suite
