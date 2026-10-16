"""Real FFmpeg: immutable per-task render plans match sequential outputs."""
import copy
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import shutil
import json
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
from PIL import Image, ImageChops, ImageStat
from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.store import Store, fresh_state

FFMPEG=os.environ.get('NODE_FFMPEG','')
FFPROBE=str(Path(FFMPEG).with_name('ffprobe.exe')) if FFMPEG else ''


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(),'requires explicit test FFmpeg')
class RealEncodingPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source.mp4';self.music=self.root/'music.wav'
        self.run_ffmpeg(['-f','lavfi','-i','testsrc2=s=320x568:r=10:d=0.8','-c:v','libx264','-pix_fmt','yuv420p',str(self.source)])
        self.run_ffmpeg(['-f','lavfi','-i','sine=frequency=660:duration=3','-c:a','pcm_s16le',str(self.music)])

    def run_ffmpeg(self,args):
        result=subprocess.run([FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error',*args],
            capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise RuntimeError(result.stderr.decode('utf8','replace'))
        return result.stdout

    def state(self,name,workers,encoder='cpu'):
        store=Store(self.root/(name+'-private'))
        state=fresh_state();state['settings'].update(ffmpeg_path=FFMPEG,aspect='source',
            export_workers=workers,video_encoder=encoder,encoder_fallback=False,
            auto_crop_black_bars=False,output_dir=str(self.root/name),skip_silence=False,background_blur=0)
        body=new_text_component();body.update(id='body',kind='body',source='job',text='',font='Open Sans',font_min=80,
            font_max=120,font_weight=700,width=95,height=45,x=50,y=35)
        footer=new_text_component();footer.update(id='footer',source='sequence',font_min=80,font_max=100,
            width=95,height=25,x=50,y=75,sequence_items=[dict(id=str(i),text=f'POOL-{i}') for i in range(3)],sequence_cursor=0)
        state['settings']['layers']=normalize_layers([body,footer])
        state['music']=[dict(id='m',path=str(self.music),cursor=0.,audio_key='')]
        state['jobs']=[dict(id=f'job-{i}',path=str(self.source),name=f'output-{i}',title=f'TITLE {i}',
            body=f'TASK {i}',status='待生成',output='',error='') for i in range(3)]
        return store,store.save(state,0)

    def batch(self,store,state):
        messages=[]
        result,state=execute_batch(store,state,[v['id'] for v in state['jobs']],threading.Event(),
            lambda *_:None,messages.append,lambda *_:None)
        self.assertEqual((result['completed'],result['failed']),(3,0),(result,messages[-4:]))
        self.assertNotIn('error',result,(result,messages[-4:]))
        return result,state

    def frame(self,path):
        raw=self.run_ffmpeg(['-ss','0.4','-i',str(path),'-frames:v','1','-f','image2pipe','-vcodec','png','-'])
        return Image.open(io.BytesIO(raw)).convert('RGB')

    def test_three_cpu_encoders_overlap_and_match_sequential_frames_audio_and_rotations(self):
        serial_store,serial=self.state('serial',1)
        expected,serial=self.batch(serial_store,serial)
        parallel_store,parallel=self.state('parallel',3)
        barrier=threading.Barrier(3)
        seen=[]
        from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
        original=PreparedExport.encode
        def encode(item):
            seen.append(threading.get_ident())
            barrier.wait(timeout=15)
            return original(item)
        with patch.object(PreparedExport,'encode',encode):
            actual,parallel=self.batch(parallel_store,parallel)
        self.assertEqual(len(set(seen)),3)
        self.assertAlmostEqual(parallel['music'][0]['cursor'],2.4,places=3)
        self.assertEqual(parallel['music'][0]['cursor'],serial['music'][0]['cursor'])
        self.assertEqual(parallel['settings']['layers'][1]['sequence_cursor'],0)
        for a,b in zip(actual['paths'],expected['paths']):
            info=engine.probe(a,FFPROBE)
            self.assertEqual((info['video']['width'],info['video']['height']),(320,568))
            self.assertTrue(info['audio']);self.assertAlmostEqual(info['duration'],.8,delta=.05)
            difference=ImageStat.Stat(ImageChops.difference(self.frame(a),self.frame(b))).mean
            self.assertLess(max(difference),2.,difference)
        self.assertFalse(list((self.root/'parallel').glob('.text-video-*')))

    def test_prepared_result_is_not_published_until_coordinator_requests_it(self):
        store,state=self.state('staged',1)
        job=state['jobs'][0]
        with engine.prepare_static_text(job,state,store.directory,threading.Event()) as prepared:
            self.assertFalse(prepared.output.exists())
            prepared.encode()
            self.assertTrue(prepared.target.is_file())
            self.assertFalse(prepared.output.exists())
            self.assertEqual(store.load()['music'][0]['cursor'],0)
            prepared.publish()
            self.assertTrue(prepared.output.is_file())

    def test_converted_node_bodies_and_explicit_pool_rotation_work_with_two_encoders(self):
        from dataclasses import replace
        from test_batch_copy_binding import ConvertedCopyBindingTests
        from app_plugins.builtin.batch_text_video import node_effects as fx
        from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
        for binding, expected in [('task',['BODY-0','BODY-4']),('rotate',['BODY-3','BODY-4'])]:
            with self.subTest(binding=binding):
                helper=ConvertedCopyBindingTests()
                helper.setUp()
                try:
                    shutil.copyfile(self.source,helper.video)
                    effect,graph,adapter=helper.setup_graph(copy_binding=binding)
                    helper.state['settings'].update(ffmpeg_path=FFMPEG,aspect='source',music_enabled=False,
                        use_voice=False,auto_crop_black_bars=False,export_workers=2)
                    helper.state=helper.store.save(helper.state,helper.state['revision'])
                    bundle,_,_=fx.prepare_bundle(helper.state,['job-0','job-4'],helper.services)
                    runtime=fx.EffectRuntime(bundle,helper.registry)
                    definition=helper.registry.definitions['batch_text_video.text_layer']
                    compiler=definition.compile_operation
                    compiled=[]
                    def capture(operation,*args,**kw):
                        compiled.append(json.loads(dict(operation.options)['layer'])['text'])
                        return compiler(operation,*args,**kw)
                    helper.registry.definitions[definition.kind]=replace(definition,compile_operation=capture)
                    barrier=threading.Barrier(2)
                    encode=PreparedExport.encode
                    def overlap(item):
                        barrier.wait(timeout=15)
                        return encode(item)
                    messages=[]
                    with patch.object(PreparedExport,'encode',overlap):
                        result,state=execute_batch(helper.store,helper.state,['job-0','job-4'],threading.Event(),
                            lambda *_:None,messages.append,lambda *_:None,node_effects=runtime)
                    self.assertEqual((result['completed'],result['failed']),(2,0),(result,messages[-4:]))
                    self.assertEqual(compiled,expected)
                    self.assertEqual(runtime.refs,[])
                    self.assertEqual(runtime.sequence_plan,[])
                    self.assertTrue(all(Path(path).is_file() for path in result['paths']))
                    if binding=='rotate':
                        current=next(v for v in state['settings']['layers'] if v['id']==effect['id'])
                        self.assertEqual(current['source_rotations'][adapter['id']]['cursor'],0)
                finally:
                    helper.doCleanups()

    def test_nvenc_real_on_explicit_synthetic_only_compatible_test_binary(self):
        compatible=os.environ.get('TEST_COMPAT_NVENC_FFMPEG','')
        if not compatible:self.skipTest('compatible synthetic-only NVENC test binary not provided')
        store,state=self.state('gpu',3,'nvenc')
        with patch.object(engine,'resolve_tools',return_value=(compatible,FFPROBE)):
            result,state=self.batch(store,state)
        for output in result['paths']:
            info=engine.probe(output,FFPROBE)
            self.assertEqual(info['video']['codec_name'],'h264')
            self.assertIn('nvenc',str(info['video'].get('tags',{})).lower())
            self.assertTrue(info['audio'])

    def test_intel_qsv_auto_fallback_with_production_ffmpeg_preserves_frames_and_audio(self):
        if os.environ.get('TEST_INTEL_QSV')!='1':
            self.skipTest('Intel QSV hardware test must be explicitly enabled')
        store,state=self.state('intel',3,'auto')
        with patch('model.MediaEncoding.probe_nvenc',return_value=(False,'required nvenc API version 13.1 / found 13.0')):
            result,state=self.batch(store,state)
        serial_store,serial=self.state('intel-reference',1)
        expected,_=self.batch(serial_store,serial)
        for output,reference in zip(result['paths'],expected['paths']):
            info=engine.probe(output,FFPROBE)
            self.assertEqual(info['video']['codec_name'],'h264')
            self.assertIn('qsv',str(info['video'].get('tags',{})).lower())
            self.assertTrue(info['audio'])
            difference=ImageStat.Stat(ImageChops.difference(self.frame(output),self.frame(reference))).mean
            self.assertLess(max(difference),4.,difference)
        self.assertAlmostEqual(state['music'][0]['cursor'],2.4,places=3)


if __name__=='__main__':unittest.main()
