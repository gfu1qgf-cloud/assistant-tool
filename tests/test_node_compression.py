import copy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from qt_compat import QtCore, QtGui, QtWidgets
from model.node_workflow.encoding import (EncodingSettings, from_parameters, PARAMETERS,
    video_encoder_args, audio_encoder_args, output_filters, mux_args)
from model.node_workflow.graph import NodeRegistry, register_core, new_node
from model.node_workflow.contracts import ResourceRef, RenderPlan, VideoTimeline, VideoSection
from model.node_workflow.video_stream import VideoStream
from model.node_workflow.render import render_plan
from model.BackgroundTasks import TaskRunContext
from PYUI.node_compression_ui import CompressionInspector

FFMPEG=os.environ.get('NODE_FFMPEG','')


class CompressionTests(unittest.TestCase):
    def test_headless_registry_stays_gui_free_and_workspace_installs_same_inspector(self):
        registry=NodeRegistry();register_core(registry)
        self.assertIsNone(registry.definitions['core.compress'].inspector_factory)
        before=registry.definitions['core.compress']
        from PYUI.node_inspectors import install_core_inspectors
        install_core_inspectors(registry)
        after=registry.definitions['core.compress']
        self.assertIs(after.inspector_factory,CompressionInspector)
        self.assertIs(after.build,before.build)
        self.assertEqual(after.parameters,before.parameters)

    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        if os.environ.get('NODE_COMPRESSION_SCREENSHOT'):
            font_id=QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
            families=QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families: cls.app.setFont(QtGui.QFont(families[0],9))

    def test_new_node_exact_screenshot_defaults_and_real_argument_mapping(self):
        registry=NodeRegistry(); register_core(registry)
        node=new_node(registry.definitions['core.compress'])
        settings=from_parameters(node['params'])
        self.assertEqual((settings.codec,settings.crf,settings.audio,settings.audio_kbps,settings.sample_rate,settings.channels),
                         ('libx264',30,'mp3',128,44100,2))
        self.assertEqual(video_encoder_args(settings)[:6],['-c:v','libx264','-preset','medium','-crf','30'])
        self.assertEqual(audio_encoder_args(settings),['-c:a','libmp3lame','-b:a','128k','-ar','44100','-ac','2'])
        self.assertEqual(output_filters('null',settings),'null')

    def test_old_saved_custom_values_remain_unchanged(self):
        params=dict(codec='libx264',preset='veryfast',crf=23,audio='aac',audio_kbps=192)
        before=copy.deepcopy(params); settings=from_parameters(params)
        self.assertEqual((settings.crf,settings.audio,settings.audio_kbps),(23,'aac',192))
        self.assertEqual((settings.sample_rate,settings.channels),(0,0))
        self.assertEqual(params,before)

    def test_pre_restart_five_field_contract_is_safe_for_lazy_render_imports(self):
        from dataclasses import make_dataclass, field
        cls=make_dataclass('EncodingSettings',[(key,type(value),field(default=value)) for key,value in
            dict(codec='libx264',preset='medium',crf=23,audio='aac',audio_kbps=192).items()])
        cls.__module__='model.node_workflow.video_stream'
        old=cls()
        self.assertIn('23',video_encoder_args(old))
        self.assertEqual(audio_encoder_args(old),['-c:a','aac','-b:a','192k'])
        self.assertEqual(output_filters('null',old),'null')
        self.assertEqual(mux_args(old),['-movflags','+faststart'])

    def test_video_rate_controls_are_not_mixed_and_fractional_quality_is_valid(self):
        for mode,flag,value in [('crf','-crf','30.5'),('qp','-qp','31'),('bitrate','-b:v','850k')]:
            args=video_encoder_args(EncodingSettings(rate_control=mode,crf=30.5,qp=31,video_kbps=850))
            self.assertEqual(args[args.index(flag)+1],value)
            self.assertEqual(sum(f in args for f in ('-crf','-qp','-b:v')),1)

    def test_audio_copy_does_not_sneak_in_resampling_or_downmix(self):
        settings=EncodingSettings(audio='copy')
        self.assertEqual(audio_encoder_args(settings),['-c:a','copy'])
        self.assertEqual(audio_encoder_args(settings,timeline=True)[1],'aac')
        for mode,flag in [('auto',None),('cbr','0'),('abr','1')]:
            args=audio_encoder_args(EncodingSettings(audio_mode=mode))
            if flag is None: self.assertNotIn('-abr',args)
            else: self.assertEqual(args[args.index('-abr')+1],flag)

    def test_disabled_optional_controls_do_not_change_video(self):
        settings=EncodingSettings(frame_rate=12,width=320,height=240,keyframe_seconds=.5)
        args=video_encoder_args(settings)
        self.assertNotIn('-force_key_frames',args)
        self.assertNotIn('fps',output_filters('null',settings))
        self.assertEqual(mux_args(settings),['-movflags','+faststart'])
        enabled=replace(settings,frame_rate_enabled=True,resize_enabled=True,keyframe_enabled=True,cfr=True)
        self.assertIn('fps=12',output_filters('null',enabled))
        self.assertIn('scale=320:240',output_filters('null',enabled))
        self.assertIn('-force_key_frames',video_encoder_args(enabled))

    def test_typed_validation_rejects_commands_invalid_audio_and_nonfinite_values(self):
        for values in ({'crf':True},{'crf':float('nan')},{'crf':52},{'audio':'-i'},
                       {'audio_kbps':512},{'sample_rate':12345},{'channels':7},{'width':301,'resize_enabled':True},
                       {'cfr':'yes'},{'rate_control':'crf;evil'},{'codec':'libx265','profile':'baseline'}):
            with self.subTest(values=values),self.assertRaises(ValueError): EncodingSettings(**values)

    def test_inspector_keeps_existing_values_and_preset_never_locks_them(self):
        changes=[]
        panel=CompressionInspector({'crf':23,'audio':'aac','audio_kbps':192},changes.append)
        try:
            self.assertFalse(changes)
            self.assertEqual(panel.controls['crf'].value(),23)
            panel.apply_screenshot()
            self.assertEqual(changes[-1]['crf'],30)
            self.assertEqual(changes[-1]['sample_rate'],44100)
            for key in ('codec','crf','preset','audio','audio_kbps','sample_rate','channels'):
                self.assertTrue(panel.controls[key].isEnabled(),key)
            panel.controls['crf'].setValue(33.5)
            self.assertEqual(changes[-1]['crf'],33.5)
            panel.controls['rate_control'].setCurrentIndex(panel.controls['rate_control'].findData('bitrate'))
            panel.controls['video_kbps'].setValue(900)
            self.assertEqual(changes[-1]['video_kbps'],900)
            panel.controls['rate_control'].setCurrentIndex(panel.controls['rate_control'].findData('crf'))
            self.assertEqual(panel.controls['crf'].value(),33.5)
            if os.environ.get('NODE_COMPRESSION_SCREENSHOT'):
                panel.apply_screenshot()
                panel.resize(390,450);panel.show();self.app.processEvents()
                panel.grab().save(os.environ['NODE_COMPRESSION_SCREENSHOT'])
                for index in (1,2,3):
                    panel.tabs.setCurrentIndex(index);self.app.processEvents()
                    target=Path(os.environ['NODE_COMPRESSION_SCREENSHOT'])
                    panel.grab().save(str(target.with_name(target.stem+f'-{index}'+target.suffix)))
        finally:panel.close()

    def test_inspector_rejected_edit_restores_prior_values(self):
        panel=CompressionInspector({},lambda _value:False)
        try:
            panel.controls['crf'].setValue(15)
            self.assertEqual(panel.controls['crf'].value(),30)
            self.assertEqual(panel.values['crf'],30)
        finally:panel.close()

    def test_inspector_persistence_error_does_not_escape_qt_signal(self):
        def fail(_value): raise OSError('存储位置不可用')
        panel=CompressionInspector({},fail)
        try:
            panel.controls['crf'].setValue(15)
            self.assertEqual(panel.controls['crf'].value(),30)
            self.assertIn('存储位置不可用',panel.info.text())
        finally:panel.close()

    def test_real_workspace_commits_inspector_and_blank_reselection_keeps_settings(self):
        from PYUI.node_workspace import NodeWorkspace
        from model.BackgroundTasks import BackgroundTaskManager
        registry=NodeRegistry();register_core(registry)
        manager=BackgroundTaskManager()
        window=QtWidgets.QWidget();window.appendLog=Mock()
        host=SimpleNamespace(available_nodes=lambda:registry,background_tasks=manager,main_window=window)
        panel=NodeWorkspace(host)
        try:
            panel.add_node('core.compress');self.app.processEvents()
            node=panel.graph['nodes'][-1];node_id=node['id']
            editor=panel.inspector.widget().findChild(CompressionInspector)
            self.assertIsNotNone(editor)
            editor.controls['crf'].setValue(34)
            editor.controls['sample_rate'].setCurrentIndex(editor.controls['sample_rate'].findData(48000))
            panel.scene.clearSelection();self.app.processEvents()
            panel.scene.nodes[node_id].setSelected(True);self.app.processEvents()
            editor=panel.inspector.widget().findChild(CompressionInspector)
            current=next(n for n in panel.graph['nodes'] if n['id']==node_id)
            self.assertEqual(current['params']['crf'],34)
            self.assertEqual(current['params']['sample_rate'],48000)
            self.assertEqual(editor.controls['crf'].value(),34)
            self.assertEqual(editor.controls['sample_rate'].currentData(),48000)
        finally:
            panel.deleteLater();window.deleteLater();manager.shutdown();manager.deleteLater()
            self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),'explicit FFmpeg required')
class RealCompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source.mp4'
        self.ff(['-f','lavfi','-i','testsrc2=s=320x240:r=25:d=2',
                 '-f','lavfi','-i','sine=frequency=440:duration=2',
                 '-c:v','libx264','-preset','ultrafast','-crf','5','-c:a','aac',str(self.source)])
        self.ref=ResourceRef.capture(self.source,'video');self.before=self.source.read_bytes();self.logs=[]
        self.context=TaskRunContext(threading.Event(),lambda *_:None,self.logs.append)
        self.context.ffmpeg=FFMPEG;self.context.task=None;self.context.notes=[]

    def ff(self,args):
        result=subprocess.run([FFMPEG,'-nostdin','-y','-v','error',*args],capture_output=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: self.fail(result.stderr.decode('utf8','replace'))
        return result

    def probe(self,path):
        result=subprocess.run([str(Path(FFMPEG).with_name('ffprobe.exe')),'-v','error','-show_streams','-of','json',str(path)],
                              capture_output=True,check=True,timeout=15)
        return json.loads(result.stdout)['streams']

    def test_screenshot_configuration_really_compresses_and_audio_is_exact(self):
        result=render_plan(RenderPlan(VideoStream(self.ref,(),EncodingSettings()),(),str(self.root/'output.mp4')),self.context,FFMPEG)
        streams=self.probe(result['video'])
        video=next(s for s in streams if s['codec_type']=='video');audio=next(s for s in streams if s['codec_type']=='audio')
        self.assertEqual(video['codec_name'],'h264')
        self.assertEqual((audio['codec_name'],audio['sample_rate'],audio['channels']),('mp3','44100',2))
        self.assertAlmostEqual(int(audio['bit_rate']),128000,delta=3000)
        self.assertLess(result['output_bytes'],result['input_bytes'])
        self.assertGreater(result['size_reduction_percent'],0)
        self.assertTrue(any('压缩结果' in log and '减少' in log for log in self.logs))
        self.assertEqual(self.source.read_bytes(),self.before)
        self.assertEqual(result['video_encodings'],1)
        print('COMPRESSION_SMOKE:',json.dumps({'input_bytes':result['input_bytes'],'output_bytes':result['output_bytes'],
            'reduction_percent':round(result['size_reduction_percent'],1),'video':video['codec_name'],
            'audio':audio['codec_name'],'sample_rate':audio['sample_rate'],'channels':audio['channels']}))

    def test_resize_fps_and_bitrate_apply_once_for_file_and_timeline(self):
        for number,source in enumerate((self.ref,VideoTimeline((VideoSection(self.ref,0,2),),320,240,'25','test',True))):
            settings=EncodingSettings(rate_control='bitrate',video_kbps=200,resize_enabled=True,width=160,height=120,
                                      frame_rate_enabled=True,frame_rate=10,cfr=True,keyframe_enabled=True,keyframe_seconds=.5)
            result=render_plan(RenderPlan(VideoStream(source,(),settings),(),str(self.root/f'mode-{number}.mp4')),self.context,FFMPEG)
            video=next(s for s in self.probe(result['video']) if s['codec_type']=='video')
            self.assertEqual((video['width'],video['height']),(160,120))
            self.assertEqual(video['avg_frame_rate'],'10/1')
            self.assertEqual(result['video_encodings'],1)
        self.assertEqual(self.source.read_bytes(),self.before)

    def test_larger_output_is_reported_honestly_without_dropping_requested_changes(self):
        first=render_plan(RenderPlan(VideoStream(self.ref,(),EncodingSettings()),(),str(self.root/'small.mp4')),self.context,FFMPEG)
        small=ResourceRef.capture(first['video'],'video');before=Path(small.path).read_bytes()
        result=render_plan(RenderPlan(VideoStream(small,(),EncodingSettings(crf=0)),(),str(self.root/'lossless.mp4')),self.context,FFMPEG)
        self.assertGreater(result['output_bytes'],result['input_bytes'])
        self.assertLess(result['size_reduction_percent'],0)
        self.assertTrue(any('比源文件大' in message for message in self.logs))
        self.assertEqual(Path(small.path).read_bytes(),before)
