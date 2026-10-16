"""Shared stitch algorithm, deferred output, and real multi-input FFmpeg jobs."""
import base64
from dataclasses import replace
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from qt_compat import QtGui, QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ResourceRef, RenderPlan, VideoComposition, VideoTimeline, VideoSection
from model.node_workflow.graph import NodeRegistry, register_core, new_node, empty_graph, validate
from model.node_workflow.tasks import prepare_job
from model.node_workflow.render import run_workflow, render_plan, probe
from model.node_workflow.preview import run_preview
from model.node_workflow.video_stream import VideoStream
from model.node_workflow.media import MediaSession
from app_plugins.builtin.video_stitch import engine
from app_plugins.builtin.video_stitch.workflow_nodes import stitch_node, build_stitch
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node, build_caption

FFMPEG=os.environ.get('NODE_FFMPEG','')


class StitchNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.left,self.right=self.root/'left.mp4',self.root/'right.mp4'
        self.left.write_bytes(b'left fixture'); self.right.write_bytes(b'right fixture')
        self.registry=NodeRegistry(); register_core(self.registry)
        self.registry.register('video_stitch',stitch_node()); self.registry.register('batch_text_video',caption_node())
        self.context=TaskRunContext(threading.Event(),lambda *_:None,lambda *_:None)
        self.context.ffmpeg=FFMPEG; self.context.workflow_registry=self.registry
    def tearDown(self): self.temp.cleanup()
    def params(self,**values): return {**{p.key:p.default for p in stitch_node().parameters},**values}
    def fake_media(self):
        return [engine.Media(str(self.left),160,90,1.8,Fraction(10),0,1,1.8),
                engine.Media(str(self.right),80,120,.6,Fraction(12),0,-1,0)]
    def test_required_ports_and_deferred_shared_build(self):
        definition=stitch_node()
        self.assertEqual([(p.key,p.data_type,p.required) for p in definition.inputs],[('left','video',True),('right','video',True)])
        self.assertEqual([(p.key,p.data_type) for p in definition.outputs],[('video','video')])
        node=new_node(definition)
        self.assertTrue(validate({**empty_graph(),'nodes':[node]},self.registry,root_id=node['id'])[0])
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        with patch('app_plugins.builtin.video_stitch.workflow_nodes.source_media',side_effect=self.fake_media()), \
             patch.object(engine,'make_plan',wraps=engine.make_plan) as shared, \
             patch('subprocess.Popen',side_effect=AssertionError('build 不得编码')):
            result=build_stitch({'left':ResourceRef.capture(self.left,'video'),'right':ResourceRef.capture(self.right,'video')},self.params(),self.context)['video']
        shared.assert_called_once(); self.assertIsInstance(result,VideoStream); self.assertIsInstance(result.source,VideoComposition)
        self.assertEqual(result.source.info['video'],{'width':240,'height':120,'avg_frame_rate':'10'})
        self.assertAlmostEqual(result.source.info['duration'],1.8)
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()},before)
    def test_other_basis_mute_and_known_task_identity(self):
        a=replace(ResourceRef.capture(self.left,'video'),task_key='task-a'); b=ResourceRef.capture(self.right,'video')
        with patch('app_plugins.builtin.video_stitch.workflow_nodes.source_media',side_effect=self.fake_media()):
            result=build_stitch({'left':a,'right':b},self.params(basis='right',audio='mute'),self.context)['video']
        self.assertEqual(result.task_key,'task-a'); self.assertEqual(result.source.info['video']['avg_frame_rate'],'12')
        self.assertFalse(result.source.info['audio'])
        with patch('app_plugins.builtin.video_stitch.workflow_nodes.source_media',side_effect=self.fake_media()),self.assertRaises(ValueError):
            build_stitch({'left':a,'right':replace(b,task_key='task-b')},self.params(),self.context)
    def graph(self,output='result.mp4',**params):
        graph=empty_graph(); nodes={}
        for name,kind in (('left','core.import'),('right','core.import'),('stitch','video_stitch.horizontal'),
                          ('settle','core.settle'),('preview','core.preview'),('export','core.export')):
            nodes[name]=new_node(self.registry.definitions[kind]); graph['nodes'].append(nodes[name])
        nodes['left']['params']['video']=str(self.left); nodes['right']['params']['video']=str(self.right)
        nodes['stitch']['params'].update(params); nodes['export']['params'].update(destination='file',output=str(self.root/output))
        for a,out,b,inp in (('left','video','stitch','left'),('right','video','stitch','right'),('stitch','video','settle','video'),
                            ('settle','plan','preview','value'),('preview','value','export','plan')):
            graph['connections'].append([nodes[a]['id'],out,nodes[b]['id'],inp])
        graph['output_node']=nodes['export']['id']; return graph,nodes


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),'需要正式 FFmpeg')
class RealStitchNodeTests(StitchNodeTests):
    def ffmpeg(self,*args):
        result=subprocess.run([FFMPEG,'-v','error','-y',*args],capture_output=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace')); return result.stdout
    def media(self,*,right_audio=False,long_tail=False):
        self.ffmpeg('-f','lavfi','-i','color=red:s=160x90:r=10:d=1.8','-f','lavfi','-i','sine=frequency=440:duration=1.8',
                    '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(self.left))
        args=['-f','lavfi','-i','color=blue:s=80x120:r=12:d=0.6']
        if right_audio: args+=['-f','lavfi','-i','sine=frequency=880:duration='+('2.4' if long_tail else '0.6'),'-c:a','aac']
        self.ffmpeg(*args,'-c:v','libx264','-pix_fmt','yuv420p',str(self.right))
    def render(self,graph):
        payload,_=prepare_job(graph,self.registry,None,None); payload['ffmpeg']=FFMPEG
        return run_workflow(payload,self.context,registry=self.registry)
    def test_real_preview_export_dimensions_loop_pixels_audio_one_encoding(self):
        self.media(); graph,nodes=self.graph()
        payload,_=prepare_job(graph,self.registry,None,None); payload.update(ffmpeg=FFMPEG,preview_node=nodes['preview']['id'])
        preview=run_preview(payload,self.context,registry=self.registry)
        image=QtGui.QImage.fromData(base64.b64decode(preview['png']))
        self.assertEqual((image.width(),image.height()),(240,120))
        self.assertGreater(image.pixelColor(70,60).red(),180); self.assertGreater(image.pixelColor(200,60).blue(),180)
        self.assertLess(image.pixelColor(70,5).red(),30)
        result=self.render(graph); info=probe(result['video'],str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertEqual((info['video']['width'],info['video']['height']),(240,120))
        self.assertAlmostEqual(info['duration'],1.8,delta=.15); self.assertTrue(info['audio'])
        self.assertEqual(result['video_encodings'],1); self.assertEqual(result['intermediate_videos'],0)
        image=QtGui.QImage.fromData(self.ffmpeg('-ss','1.5','-i',result['video'],'-frames:v','1','-f','image2pipe','-c:v','png','pipe:1'))
        self.assertGreater(image.pixelColor(200,60).blue(),180)
        self.assertEqual(sorted(p.name for p in self.root.glob('*.mp4')),['left.mp4','result.mp4','right.mp4'])
    def test_right_basis_mute_and_mix(self):
        self.media(right_audio=True)
        for basis,audio,duration in (('right','mute',.6),('left','mix',1.8),('left','right',1.8)):
            graph,_=self.graph(basis+'-'+audio+'.mp4',basis=basis,audio=audio)
            result=self.render(graph); info=probe(result['video'],str(Path(FFMPEG).with_name('ffprobe.exe')))
            self.assertAlmostEqual(info['duration'],duration,delta=.15); self.assertEqual(info['audio'],audio!='mute')
    def test_long_audio_tail_does_not_delay_short_video_repetition(self):
        self.media(right_audio=True,long_tail=True); graph,_=self.graph(audio='right'); result=self.render(graph)
        image=QtGui.QImage.fromData(self.ffmpeg('-ss','1.4','-i',result['video'],'-frames:v','1','-f','image2pipe','-c:v','png','pipe:1'))
        self.assertGreater(image.pixelColor(200,60).blue(),180)
        self.assertAlmostEqual(probe(result['video'],str(Path(FFMPEG).with_name('ffprobe.exe')))['duration'],1.8,delta=.15)
    def test_timeline_memory_inputs_and_following_subtitles(self):
        self.media(); left=ResourceRef.capture(self.left,'video'); right=ResourceRef.capture(self.right,'video')
        timeline=VideoTimeline((VideoSection(left,.2,1.2),),160,90,'10','')
        with MediaSession(self.context,FFMPEG) as session:
            self.context.media=session; cached=session.convert(right,'memory',16)
            composed=build_stitch({'left':timeline,'right':cached},self.params(),self.context)['video']
            srt=self.root/'words.srt'; srt.write_text('1\n00:00:00,000 --> 00:00:00,800\nTEST\n',encoding='utf8')
            operation=build_caption({'subtitles':ResourceRef.capture(srt,'subtitles')},{'font':'Arial','size':18,'color':'#ffffff','outline':1,'position':'middle'},self.context)['operations'][0]
            result=render_plan(RenderPlan(composed,(operation,),str(self.root/'with-caption.mp4')),self.context,FFMPEG)
            self.assertTrue(Path(result['video']).is_file())
            self.assertAlmostEqual(probe(result['video'],str(Path(FFMPEG).with_name('ffprobe.exe')))['duration'],1,delta=.15)

    def test_nested_stitch_and_independent_branch_subtitle_assets(self):
        self.media()
        inputs=[]
        for index,path in enumerate((self.left,self.right)):
            srt=self.root/f'words-{index}.srt'
            srt.write_text(f'1\n00:00:00,000 --> 00:00:00,400\nSIDE {index}\n',encoding='utf8')
            operation=build_caption({'subtitles':ResourceRef.capture(srt,'subtitles')},
                {'font':'Arial','size':18,'color':'#ffffff','outline':1,'position':'middle'},self.context)['operations'][0]
            inputs.append(VideoStream(ResourceRef.capture(path,'video'),(operation,)))
        first=build_stitch(dict(zip(('left','right'),inputs)),self.params(),self.context)['video']
        nested=build_stitch({'left':first,'right':ResourceRef.capture(self.left,'video')},self.params(audio='mute'),self.context)['video']
        result=render_plan(RenderPlan(nested,(),str(self.root/'nested.mp4')),self.context,FFMPEG)
        info=probe(result['video'],str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertEqual((info['video']['width'],info['video']['height']),(400,120))
        self.assertFalse(info['audio']); self.assertAlmostEqual(info['duration'],1.8,delta=.15)
        self.assertEqual(result['intermediate_videos'],0)

    def test_exact_odd_dimensions_are_not_silently_resized(self):
        for path,color,size in ((self.left,'red','101x91'),(self.right,'blue','80x80')):
            self.ffmpeg('-f','lavfi','-i',f'color={color}:s={size}:r=10:d=0.5,format=yuv444p',
                '-c:v','libx264','-pix_fmt','yuv444p',str(path))
        graph,_=self.graph(dimensions='exact',audio='mute')
        result=self.render(graph); info=probe(result['video'],str(Path(FFMPEG).with_name('ffprobe.exe')))
        left=engine.probe(self.left,str(Path(FFMPEG).with_name('ffprobe.exe')))
        right=engine.probe(self.right,str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertEqual((info['video']['width'],info['video']['height']),(left.width+right.width,max(left.height,right.height)))


if __name__=='__main__': unittest.main()
