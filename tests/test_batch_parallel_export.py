"""Scheduling and failure tests do not need hardware or user projects."""
import copy
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources
from app_plugins.builtin.batch_text_video.engine import Cancelled
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.export_pipeline import pipeline_workers
from model.MediaEncoding import normalize_export_settings, video_encoding_args, resolve_video_encoder, hardware_error
from model.MediaCacheLeases import media_cache_lease, media_cache_in_use


class ParallelExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'private')
        self.state = fresh_state()
        self.state['settings'].update(export_workers=3, music_enabled=True)
        layer = new_text_component()
        layer.update(source='sequence', sequence_cursor=0, sequence_items=[{'id':str(i),'text':f'COPY-{i}'} for i in range(3)])
        self.state['settings']['layers'] = normalize_layers([layer])
        self.state['music'] = [{'id':'music', 'path':'fake.wav','cursor':0.,'audio_key':'key'}]
        self.state['jobs'] = [dict(id=f'job-{i}',path=f'source-{i}.mp4',name=f'video-{i}',
            title=f'TITLE-{i}',body=f'BODY-{i}',status='待生成',output='',error='') for i in range(4)]
        self.state = self.store.save(self.state,0)
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.prepared, self.published, self.encoding_threads, self.preparing_threads = [], [], [], []
        self.attempts = {}
        self.messages, self.done = [], []
        self.fail_encode = set()
        self.fail_prepare = set()
        self.cancel_during = False
        self.no_resources = False
        self.fail_publish = set()
        self.cancel_prepare = False

    def renderer(self):
        outer = self
        class Prepared:
            def __init__(self,job,state,target,token):
                self.output=outer.root/(job['name']+'.mp4'); self.target=target; self.token=token
                self.music=None if outer.no_resources else {'id':'music','path':'clean.flac','pool_id':'',
                    'audio_key':'key','cursor':state['music'][0]['cursor']+10,'start':state['music'][0]['cursor']}
                layers, self.sequence_plan = resolve_sources(state['settings']['layers'],job['title'],job['body'],job=job)
                if outer.no_resources:self.sequence_plan=[]
                self.job=job; self.selection=layers[0]['text']
                outer.prepared.append((job['id'], self.music['start'] if self.music else None,
                    self.selection,job['title'],job['body']))
            def encode(self):
                with outer.lock:
                    outer.active+=1;outer.peak=max(outer.peak,outer.active)
                    outer.encoding_threads.append(threading.get_ident())
                try:
                    deadline=time.monotonic()+(.12 if self.job['id']=='job-0' else .055)
                    while time.monotonic()<deadline:
                        if self.token.is_set():raise Cancelled('cancel test')
                        time.sleep(.002)
                    if outer.cancel_during:outer.cancel.set()
                    if self.job['id'] in outer.fail_encode:raise RuntimeError('synthetic encoder failure')
                    self.target.write_bytes((self.job['title']+'|'+self.job['body']+'|'+self.selection).encode())
                    return self
                finally:
                    with outer.lock:outer.active-=1
            def publish(self):
                if self.token.is_set():raise Cancelled('cancel test')
                if self.job['id'] in outer.fail_publish:raise FileExistsError('synthetic output appeared')
                outer.published.append(self.job['id'])
                self.target.replace(self.output)
                return self.output
        @contextmanager
        def prepare(job,state,directory,token,*args,**kwargs):
            outer.preparing_threads.append(threading.get_ident())
            if outer.cancel_prepare and job['id']=='job-1':
                outer.cancel.set()
                raise InterruptedError('synthetic model cancellation')
            outer.attempts[job['id']]=outer.attempts.get(job['id'],0)+1
            if job['id'] in outer.fail_prepare:raise ValueError('synthetic prepare failure')
            with tempfile.TemporaryDirectory(dir=outer.root) as temporary:
                yield Prepared(job,state,Path(temporary)/'result.mp4',token)
        def render(*args,**kwargs):raise AssertionError('must use prepared pipeline')
        render.prepare_export=prepare
        return render

    def run_batch(self,done=None):
        return execute_batch(self.store,self.state,[job['id'] for job in self.state['jobs']],self.cancel,
            lambda *args:None,self.messages.append,done or (lambda *args:self.done.append(args)),
            {'static_text':self.renderer()})

    def test_encodes_overlap_but_prepare_and_commit_are_ordered_and_match_tasks(self):
        before=copy.deepcopy(self.state)
        main=threading.get_ident()
        result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(4,0),result)
        self.assertEqual(self.peak,3)
        self.assertTrue(all(t==main for t in self.preparing_threads))
        self.assertTrue(all(t!=main for t in self.encoding_threads))
        self.assertEqual(self.published,['job-0','job-1','job-2','job-3'])
        self.assertEqual([row[1] for row in self.prepared],[0,10,20,30])
        self.assertEqual([row[2] for row in self.prepared],['COPY-0','COPY-1','COPY-2','COPY-0'])
        self.assertEqual(state['music'][0]['cursor'],40)
        self.assertEqual(state['settings']['layers'][0]['sequence_cursor'],1)
        self.assertEqual(self.state,before)
        for i,path in enumerate(result['paths']):
            self.assertTrue(Path(path).read_text().startswith(f'TITLE-{i}|BODY-{i}|'))

    def test_failed_reservation_replays_unpublished_dependents_without_consuming_cursor(self):
        self.fail_encode.add('job-0')
        result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(3,1),result)
        self.assertEqual(self.published,['job-1','job-2','job-3'])
        self.assertGreaterEqual(self.attempts['job-1'],2)
        self.assertGreaterEqual(self.attempts['job-2'],2)
        self.assertEqual([row[1] for row in self.prepared if row[0]=='job-1'][-1],0)
        self.assertEqual([row[2] for row in self.prepared if row[0]=='job-1'][-1],'COPY-0')
        self.assertEqual(state['music'][0]['cursor'],30)
        self.assertEqual(state['settings']['layers'][0]['sequence_cursor'],0)
        self.assertEqual(state['jobs'][0]['status'],'需处理')
        self.assertFalse((self.root/'video-0.mp4').exists())
        self.assertEqual(len(self.done),4)

    def test_independent_failure_does_not_reencode_other_results(self):
        self.no_resources=True
        self.fail_encode.add('job-0')
        result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(3,1),result)
        self.assertTrue(all(value==1 for value in self.attempts.values()))

    def test_prepare_failure_does_not_reserve_music_or_pool_and_others_continue(self):
        self.fail_prepare.add('job-1')
        result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(3,1),result)
        self.assertEqual([row[1] for row in self.prepared],[0,10,20])
        self.assertEqual(state['music'][0]['cursor'],30)

    def test_output_appearing_at_publish_fails_only_that_job_and_replans_dependents(self):
        self.fail_publish.add('job-0')
        old=self.root/'video-0.mp4';old.write_bytes(b'KEEP')
        result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(3,1),result)
        self.assertEqual(old.read_bytes(),b'KEEP')
        self.assertEqual(state['music'][0]['cursor'],30)

    def test_external_state_change_stops_before_replacing_existing_result(self):
        old=self.root/'video-0.mp4';old.write_bytes(b'KEEP')
        original=self.store.load
        calls=0
        def changed():
            nonlocal calls
            calls+=1
            state=original()
            if calls==1:state['revision']+=1
            return state
        with patch.object(self.store,'load',side_effect=changed):
            result,state=self.run_batch()
        self.assertIn('error',result)
        self.assertEqual(self.published,[])
        self.assertEqual(old.read_bytes(),b'KEEP')

    def test_cancel_before_publication_keeps_original_outputs_and_rotation(self):
        old=self.root/'video-0.mp4';old.write_bytes(b'OLD RESULT')
        self.cancel_during=True
        result,state=self.run_batch()
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['completed'],0)
        self.assertEqual(old.read_bytes(),b'OLD RESULT')
        self.assertEqual(state['music'][0]['cursor'],0)
        self.assertFalse(self.published)

    def test_canceled_model_preparation_is_not_counted_as_a_failed_video(self):
        self.cancel_prepare=True
        result,state=self.run_batch()
        self.assertTrue(result['cancelled'])
        self.assertEqual((result['completed'],result['failed']),(0,0))
        self.assertEqual(state['music'][0]['cursor'],0)

    def test_cancel_after_first_commit_keeps_that_success_only(self):
        def done(identifier,status):
            self.done.append((identifier,status))
            if identifier=='job-0':self.cancel.set()
        result,state=self.run_batch(done)
        self.assertEqual(result['completed'],1,result)
        self.assertEqual(self.published,['job-0'])
        self.assertEqual(state['music'][0]['cursor'],10)
        self.assertEqual(state['settings']['layers'][0]['sequence_cursor'],1)

    def test_state_save_failure_stops_pipeline_without_overwriting_other_results(self):
        before=self.store.path.read_bytes()
        old=self.root/'video-1.mp4';old.write_bytes(b'KEEP')
        with patch.object(self.store,'completed',side_effect=OSError('private volume unavailable')):
            result,state=self.run_batch()
        self.assertIn('error',result)
        self.assertEqual(self.published,['job-0'])
        self.assertEqual(old.read_bytes(),b'KEEP')
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_duplicate_destinations_cannot_race_even_if_overwrite_enabled(self):
        self.state['settings']['overwrite']=True
        self.state['jobs'][1]['name']=self.state['jobs'][0]['name']
        result,state=self.run_batch()
        self.assertEqual((result['completed'],result['failed']),(3,1),result)
        self.assertIn('同一成品',state['jobs'][1]['error'])
        self.assertNotIn('job-1',self.published)

    def test_pure_reservations_do_not_write_store(self):
        before=self.store.path.read_bytes()
        planned=self.store.completion_state(self.state,'job-0',self.root/'not-yet.mp4',
            {'id':'music','cursor':10,'audio_key':'key'})
        self.assertEqual(planned['music'][0]['cursor'],10)
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(self.state['music'][0]['cursor'],0)

    def test_invalid_config_returns_a_result_instead_of_escaping_a_qthread(self):
        self.state['settings']['export_workers']=99
        result,state=self.run_batch()
        self.assertIn('error',result)
        self.assertEqual(self.prepared,[])
        self.assertEqual(self.store.load()['music'][0]['cursor'],0)

    def test_already_loaded_renderer_without_new_interface_stays_safe_until_restart(self):
        def renderer(job,state,directory,*args):
            output=self.root/(job['name']+'.mp4')
            output.write_bytes(b'sequential')
            return output,None
        result,state=execute_batch(self.store,self.state,['job-0'],self.cancel,lambda *_:None,
            self.messages.append,lambda *_:None,{'static_text':renderer})
        self.assertEqual(result['completed'],1,result)
        self.assertTrue(any('正常重启' in text for text in self.messages))


class EncodingPolicyTests(unittest.TestCase):
    def test_defaults_preserve_cpu_quality_and_explicit_limits(self):
        settings={};normalize_export_settings(settings)
        self.assertEqual(settings,dict(video_encoder='cpu',encoder_fallback=True,export_workers=1,export_quality='high'))
        self.assertEqual(video_encoding_args('cpu'),['-c:v','libx264','-preset','veryfast','-crf','20'])
        self.assertIn('25',video_encoding_args('cpu',preview=True))
        self.assertIn('19',video_encoding_args('nvenc'))
        self.assertIn('-threads',video_encoding_args('cpu',workers=4))
        for field,value in [('export_workers',0),('export_workers',9),('export_workers',True),
                            ('video_encoder','bogus'),('encoder_fallback','yes')]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                normalize_export_settings(dict(settings,**{field:value}))

    def test_hardware_unavailable_is_explicit_and_never_updates_a_driver(self):
        settings=dict(video_encoder='nvenc',encoder_fallback=True,export_workers=4)
        messages=[]
        with patch('model.MediaEncoding.probe_nvenc',return_value=(False,'required nvenc API version: 13.1 / found 13.0')):
            self.assertEqual(resolve_video_encoder('unused',settings,report=messages.append),'cpu')
            self.assertIn('回退CPU',messages[0]);self.assertIn('驱动',messages[0])
            with self.assertRaisesRegex(RuntimeError,'驱动'):
                resolve_video_encoder('unused',dict(settings,encoder_fallback=False))
        with patch('model.MediaEncoding.probe_nvenc',return_value=(True,'')):
            self.assertEqual(resolve_video_encoder('unused',settings),'nvenc')
        self.assertFalse(hardware_error('No space left on device'))

    def test_auto_tries_intel_after_incompatible_nvidia_instead_of_stopping(self):
        settings=dict(video_encoder='auto',encoder_fallback=True,export_workers=4)
        messages=[]
        with patch('model.MediaEncoding.probe_nvenc',return_value=(False,'required nvenc API version')) as nvenc, \
             patch('model.MediaEncoding.probe_qsv',return_value=(True,'')) as qsv:
            self.assertEqual(resolve_video_encoder('unused',settings,report=messages.append),'qsv')
            nvenc.assert_called_once();qsv.assert_called_once()
            self.assertIn('Intel QSV',messages[0]);self.assertNotIn('回退CPU',messages[0])
        with patch('model.MediaEncoding.probe_nvenc',return_value=(False,'no device')), \
             patch('model.MediaEncoding.probe_qsv',return_value=(False,'MFX unavailable')):
            self.assertEqual(resolve_video_encoder('unused',settings),'cpu')
            with self.assertRaisesRegex(RuntimeError,'Intel QSV'):
                resolve_video_encoder('unused',dict(settings,video_encoder='qsv',encoder_fallback=False))

    def test_side_effect_nodes_disable_speculative_replay_but_plain_nodes_do_not(self):
        def effects(kind):return SimpleNamespace(bundle={'jobs':{'id':{'graph':{'graph':{'nodes':[{'kind':kind}]}}}}})
        for kind in ('ai_model_client.request','core.python_code'):
            self.assertEqual(pipeline_workers({'export_workers':4},effects(kind)),1)
        self.assertEqual(pipeline_workers({'export_workers':4},effects('core.text_palette')),4)

    def test_each_video_gets_separate_node_refs_and_rotation_lists(self):
        from app_plugins.builtin.batch_text_video.node_effects import EffectRuntime
        runtime=EffectRuntime({'jobs':{}},object())
        a,b=runtime.fork(),runtime.fork()
        a.sequence_plan.append({'id':'first'});a.refs.append('first')
        self.assertEqual(b.sequence_plan,[]);self.assertEqual(b.refs,[])
        self.assertIs(a.bundle,b.bundle)

    def test_media_cache_lease_handles_nested_use_and_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cache.mp4'
            self.assertFalse(media_cache_in_use(path))
            with media_cache_lease(path):
                with self.assertRaises(ValueError):
                    with media_cache_lease(path):raise ValueError('test')
                self.assertTrue(media_cache_in_use(path))
            self.assertFalse(media_cache_in_use(path))

    def test_cache_pruning_protects_leased_files_and_trims_after_release(self):
        from model.MediaCacheLeases import trim_generated_cache
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            a=root/('a'*64+'.mp4');b=root/('b'*64+'.mp4');source=root/'user-source.mp4'
            a.write_bytes(b'a'*10);b.write_bytes(b'b'*10);source.write_bytes(b'preserve')
            with media_cache_lease(a),media_cache_lease(b):
                trim_generated_cache(root,'.mp4',10)
                self.assertTrue(a.exists() and b.exists())
            trim_generated_cache(root,'.mp4',10)
            self.assertEqual(sum(p.stat().st_size for p in (a,b) if p.exists()),10)
            self.assertEqual(source.read_bytes(),b'preserve')

    def test_runtime_nvenc_failure_retries_cpu_once_without_changing_composition(self):
        from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
        from model.MediaEncoding import video_encoding_args
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=['ffmpeg','-i','source',*video_encoding_args('nvenc'),'-pix_fmt','yuv420p','-color_primaries','bt709',str(root/'temp.mp4')]
            calls=[]
            def run(command,*_a,**_k):
                calls.append(command)
                if len(calls)==1:raise RuntimeError('NVENC OpenEncodeSessionEx: session limit')
                (root/'temp.mp4').write_bytes(b'test')
            prepared=PreparedExport(args,root/'temp.mp4',root/'out.mp4','probe',1.,True,None,[],
                threading.Event(),None,lambda _:None,False,'nvenc',True,False,4)
            with patch('app_plugins.builtin.batch_text_video.engine._run',side_effect=run), \
                 patch('app_plugins.builtin.batch_text_video.engine.probe',return_value={'duration':1.,'audio':True}):
                prepared.encode()
            self.assertEqual(prepared.encoder,'cpu')
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[1][:3],args[:3])
            self.assertEqual(calls[1][-5:],args[-5:])
            self.assertIn('libx264',calls[1]);self.assertNotIn('h264_nvenc',calls[1])

    def test_intel_runtime_failure_can_fall_back_without_leaving_nv12_override(self):
        from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=['ffmpeg','-i','source',*video_encoding_args('qsv'),'-pix_fmt','nv12',str(root/'temp.mp4')]
            calls=[]
            def run(command,*_args,**_kwargs):
                calls.append(command)
                if len(calls)==1:raise RuntimeError('MFX session unavailable')
                (root/'temp.mp4').write_bytes(b'test')
            item=PreparedExport(args,root/'temp.mp4',root/'out.mp4','probe',1.,False,None,[],
                threading.Event(),None,lambda _:None,False,'qsv',True,False,4)
            with patch('app_plugins.builtin.batch_text_video.engine._run',side_effect=run), \
                 patch('app_plugins.builtin.batch_text_video.engine.probe',return_value={'duration':1.,'audio':False}):
                item.encode()
            self.assertEqual(item.encoder,'cpu')
            self.assertEqual(calls[-1][calls[-1].index('-pix_fmt')+1],'yuv420p')

    def test_hardware_probe_cancellation_kills_the_child_without_running_on_the_ui(self):
        from model.MediaEncoding import probe_qsv
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'fake.exe';executable.write_bytes(b'test')
            cancel=threading.Event();cancel.set()
            process=Mock();process.communicate.return_value=(b'',b'')
            with patch('model.MediaEncoding.subprocess.Popen',return_value=process),self.assertRaises(InterruptedError):
                probe_qsv(str(executable),cancel)
            process.kill.assert_called_once()
            process.communicate.assert_called_once_with(timeout=10)


if __name__=='__main__':unittest.main()
