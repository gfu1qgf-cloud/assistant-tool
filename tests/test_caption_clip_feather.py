import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.caption_clip import geometry, mask_image, append_caption
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives, render_design, write_primitives_ass
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.captions import write_caption_ass
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.store import Store, fresh_state


class CaptionClipFeatherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self, **values):
        layer=new_follow_caption()
        layer.update(font='Arial',font_min=48,font_max=48,outline_width=0,shadow_enabled=False,
                     background_enabled=False,caption_follow_mode='line',caption_clip_feather=8)
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none',karaoke_highlight=False)
        layer.update(values)
        return normalize_layers([layer])[0]

    def rectangle_layer(self, soft, opacity=100):
        return self.layer(x=50,y=50,width=62.5,height=100/240*100,
                          caption_clip_feather=20 if soft else 0,opacity=opacity)

    def test_mask_is_continuous_symmetric_and_bounded_on_all_four_edges(self):
        mask=mask_image(self.rectangle_layer(True),320,240)
        value=lambda x,y:mask.pixelColor(x,y).red()
        self.assertEqual(value(160,120),255)
        self.assertEqual(value(0,0),0)
        self.assertEqual(value(60,100),value(259,100))
        self.assertEqual(value(160,70),value(160,169))
        self.assertAlmostEqual(value(65,75),round(255*5.5/20),delta=1)
        self.assertLess(value(60,100),value(65,100))
        self.assertLess(value(65,100),value(75,100))
        self.assertEqual(mask.size(),mask_image(self.rectangle_layer(True),320,240,alpha=True).size())

    def test_zero_default_and_validation_preserve_existing_projects(self):
        layer=self.layer();layer.pop('caption_clip_feather');before=copy.deepcopy(layer)
        self.assertEqual(normalize_layers([layer])[0]['caption_clip_feather'],0)
        self.assertEqual(layer,before)
        self.assertIsNone(geometry(self.layer(caption_clip_feather=0),320,240))
        self.assertIsNone(geometry(self.layer(caption_follow_mode='none'),320,240))
        for invalid in (-1,50.1,True,'8',float('nan')):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):self.layer(caption_clip_feather=invalid)

    def test_control_is_first_basic_row_and_can_be_found_in_parameter_overview(self):
        from app_plugins.builtin.batch_text_video.caption_parameter_window import parameter_targets
        panel=CaptionProperties();self.addCleanup(panel.close)
        layer=self.layer();panel.set_layer(layer)
        control=panel.controls['caption_clip_feather']
        self.assertTrue(panel.basic_widget.isAncestorOf(control))
        self.assertFalse(panel.follow_section.isAncestorOf(control))
        self.assertEqual(panel.forms['timing'].getWidgetPosition(control)[0],0)
        self.assertTrue(control.isEnabled())
        changes=[];panel.changed.connect(changes.append);control.setValue(12)
        self.assertEqual(changes[-1]['caption_clip_feather'],12)
        self.assertEqual(parameter_targets(panel)[('layer','caption_clip_feather')][1],'裁切边缘柔化')
        panel.set_layer(self.layer(caption_follow_mode='none'));self.assertTrue(control.isEnabled())
        control.setValue(16)
        self.assertEqual(changes[-1]['caption_clip_feather'],16)
        qa=os.environ.get('LZX_CAPTION_CLIP_UI')
        if qa:
            panel.set_layer(layer);panel.resize(520,1000);panel.show();self.app.processEvents()
            self.assertTrue(panel.basic_widget.grab().save(qa))

    def test_preset_project_and_node_preserve_the_basic_parameter(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        layer=self.layer(caption_clip_feather=14)
        self.assertEqual(apply_style(layer,PREFIX+'默认白字')['caption_clip_feather'],14)
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);state=store.load();state['settings']['layers']=normalize_layers([layer]);store.save(state,0)
            saved=next(item for item in store.load()['settings']['layers'] if item['id']==layer['id'])
            self.assertEqual(saved['caption_clip_feather'],14)
        changes=[];editor=CaptionNodeEditor({},changes.append);self.addCleanup(editor.close)
        editor.mode.setCurrentIndex(1);panel=editor.panel.caption_properties
        panel.controls['caption_follow_mode'].setCurrentIndex(panel.controls['caption_follow_mode'].findData('line'))
        panel.controls['caption_clip_feather'].setValue(18)
        self.assertEqual(changes[-1]['caption']['caption_clip_feather'],18)

    def test_long_caption_ass_does_not_grow_when_feather_is_enabled(self):
        words=[dict(text='Slovensko',start=i*.25,end=(i+1)*.25) for i in range(180)]
        with tempfile.TemporaryDirectory() as folder:
            soft,hard=Path(folder)/'soft.ass',Path(folder)/'hard.ass'
            events=write_caption_ass(soft,words,self.layer(caption_per_line=2),540,960,46.)
            write_caption_ass(hard,words,self.layer(caption_per_line=2,caption_clip_feather=0),540,960,46.)
            self.assertEqual(len(events),len(words))
            self.assertEqual(soft.read_bytes(),hard.read_bytes())
            self.assertLess(soft.stat().st_size,64*1024*1024)

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_real_libass_mask_matches_qt_and_preserves_layer_alpha_and_background(self):
        from qt_compat import QtCore
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            for soft in (False,True):
                for opacity in (100,50):
                    with self.subTest(soft=soft,opacity=opacity):
                        layer=self.rectangle_layer(soft,opacity)
                        path=QtGui.QPainterPath();path.addRect(QtCore.QRectF(0,0,200,100))
                        primitive=dict(native=path,x=60.,y=70.,width=200.,height=100.,start=0.,end=2.,
                            color=['#FFFFFF'],opacity=1.,border=0.,border_color='#000000',blur=0.,rotation=0.,z=0,
                            motion='none',motion_ms=0,out='none',out_ms=0,
                            follow_track=((0.,0.,0.),(2.,0.,0.)),screen_clip=(60,70,260,170))
                        ass=folder/(str(soft)+str(opacity)+'.ass')
                        write_primitives_ass(ass,[primitive],[],layer,320,240)
                        with patch('app_plugins.builtin.batch_text_video.caption_style_render.make_primitives',return_value=([primitive],[])):
                            qt,_=render_design(layer,320,240,words=[dict(text='Alpha',start=0.,end=2.)],duration=2.,time=1.)
                        args=[os.environ['NODE_FFMPEG'],'-y','-nostdin','-hide_banner','-loglevel','error',
                              '-f','lavfi','-i','color=c=blue:s=320x240:r=25:d=2']
                        filters=[];count=append_caption(args,filters,'0:v','out',ass,layer,320,240,2.,25,1)
                        self.assertEqual(count,int(soft))
                        target=ass.with_suffix('.png')
                        subprocess.run(args+['-filter_complex',';'.join(filters),'-map','[out]',
                            '-ss','1','-frames:v','1',str(target)],check=True,capture_output=True,timeout=30)
                        native=QtGui.QImage(str(target))
                        for x,y in ((0,0),(59,100),(260,100),(160,100),(60,100),(65,75),(160,70),(160,75),(259,100),(160,169)):
                            expected=round(qt.pixelColor(x,y).alpha()*opacity/100)
                            self.assertAlmostEqual(native.pixelColor(x,y).red(),expected,delta=5,msg=(soft,opacity,x,y))
                        self.assertGreater(native.pixelColor(0,0).blue(),245)
                        self.assertEqual(qt.pixelColor(160,100).alpha(),255)
                        if soft:
                            self.assertLess(qt.pixelColor(60,100).alpha(),20)
                            self.assertLess(ass.with_suffix('.clip.png').stat().st_size,100_000)
                        qa=os.environ.get('LZX_CAPTION_CLIP_QA')
                        if qa and opacity==100:
                            Path(qa).mkdir(parents=True,exist_ok=True)
                            qt.save(str(Path(qa)/(('soft' if soft else 'hard')+'-qt.png')))
                            native.save(str(Path(qa)/(('soft' if soft else 'hard')+'-ffmpeg.png')))

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_real_batch_export_keeps_audio_and_uses_one_small_static_mask(self):
        from app_plugins.builtin.batch_text_video import engine
        ffmpeg=os.environ['NODE_FFMPEG']
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);voice=folder/'voice.wav';srt=folder/'words.srt'
            subprocess.run([ffmpeg,'-y','-nostdin','-loglevel','error','-f','lavfi','-i',
                'sine=frequency=440:duration=2',str(voice)],check=True,capture_output=True,timeout=30)
            srt.write_text('1\n00:00:00,100 --> 00:00:00,800\nAlpha\n\n2\n00:00:01,000 --> 00:00:01,800\nBeta\n',encoding='utf8')
            layer=self.layer(caption_timing='file',caption_file=str(srt))
            state=fresh_state();state['settings'].update(ffmpeg_path=ffmpeg,aspect='custom',canvas_width=320,
                canvas_height=240,use_voice=True,auto_crop_black_bars=False,output_dir=str(folder/'out'),
                music_enabled=False,layers=normalize_layers([layer]))
            for item in state['settings']['layers']:
                if item['id']!=layer['id']:item['enabled']=False
            job=dict(id='j',path='',name='result',title='',body='Alpha Beta',voice_path=str(voice))
            with engine.prepare_static_text(job,state,folder/'private') as prepared:
                masks=[Path(prepared.args[i+1]) for i,arg in enumerate(prepared.args[:-1])
                       if arg=='-i' and prepared.args[i+1].endswith('.clip.png')]
                self.assertEqual(len(masks),1);self.assertTrue(masks[0].is_file())
                self.assertLess(masks[0].stat().st_size,100_000)
                prepared.encode();output=prepared.publish()
                self.assertTrue(masks[0].is_file())
            self.assertFalse(masks[0].exists())
            info=engine.probe(output,str(Path(ffmpeg).with_name('ffprobe.exe')))
            self.assertTrue(info['audio']);self.assertAlmostEqual(info['duration'],2.,delta=.1)

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_real_caption_node_and_timed_caption_share_mask_and_preserve_audio(self):
        from test_node_caption_styles import CaptionStyleNodeTests
        from model.node_workflow.contracts import ResourceRef,RenderPlan
        from model.node_workflow.render import render_plan,probe
        from app_plugins.builtin.batch_text_video.workflow_timing import build_timing,timing_node
        helper=CaptionStyleNodeTests();helper.setUp();self.addCleanup(helper.temp.cleanup)
        ffmpeg=os.environ['NODE_FFMPEG']
        subprocess.run([ffmpeg,'-y','-nostdin','-loglevel','error','-f','lavfi','-i',
            'color=c=blue:s=360x640:r=25:d=3.5','-f','lavfi','-i','sine=frequency=440:duration=3.5',
            '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(helper.source)],
            check=True,capture_output=True,timeout=30)
        helper.video=ResourceRef.capture(helper.source,'video')
        helper.registry.register('batch_text_video',timing_node())
        layer=helper.layer('appear',caption_follow_mode='line',caption_clip_feather=8)
        operation=helper.operation(layer)
        for timed in (False,True):
            ops=build_timing({'operations':(operation,)},{'start':.2,'end':3.2},helper.context)['operations'] if timed else (operation,)
            output=helper.root/(str(timed)+'.mp4')
            render_plan(RenderPlan(helper.video,ops,str(output)),helper.context,ffmpeg)
            info=probe(output,str(Path(ffmpeg).with_name('ffprobe.exe')))
            self.assertTrue(info['audio']);self.assertAlmostEqual(info['duration'],3.5,delta=.1)
            self.assertFalse(list(helper.root.glob('.node-render-*')))


if __name__=='__main__':unittest.main()
