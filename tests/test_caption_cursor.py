import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption,offset_at
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.captions import write_caption_ass
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives,render_design
from app_plugins.builtin.batch_text_video.caption_clip import append_caption
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.engine import filter_file_option

FFMPEG=os.environ.get('NODE_FFMPEG','')


class CursorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.image=self.root/'光标图片.png'
        image=QtGui.QImage(16,24,QtGui.QImage.Format.Format_ARGB32_Premultiplied);image.fill(QtCore.Qt.GlobalColor.transparent)
        painter=QtGui.QPainter(image);painter.fillRect(3,2,10,19,QtGui.QColor('#00ffff'));painter.end();image.save(str(self.image))

    def layer(self,**changes):
        layer=new_follow_caption();layer.update(font='Arial',font_min=48,font_max=48,
            caption_follow_mode='line',caption_display_mode='char',caption_cursor=True,
            caption_per_line=3,outline_width=0,shadow_enabled=False,background_enabled=False,
            x=50,y=50,width=85,height=40)
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none',karaoke_highlight=False)
        layer.update(changes);return normalize_layers([layer])[0]

    def words(self,count=90):
        return [dict(text='pokoj',start=i*.4,end=(i+1)*.4) for i in range(count)]

    def test_bar_and_picture_use_the_same_late_word_follow_track_and_viewport(self):
        words=self.words()
        for mode in ('bar','image'):
            layer=self.layer(caption_cursor_mode=mode,caption_cursor_image=str(self.image))
            from app_plugins.builtin.batch_text_video import caption_cursor
            with patch.object(caption_cursor,'read_image',wraps=caption_cursor.read_image) as load:
                primitives,events=make_primitives(words,layer,540,960,36)
            self.assertLessEqual(load.call_count,1)
            cursors=[p for p in primitives if p.get('cursor_track')]
            self.assertLess(len(cursors),10)
            moment=30.05
            last=next(p for p in cursors if p['start']<=moment<p['end'])
            self.assertIn('follow_track',last);self.assertIn('screen_clip',last)
            offset=offset_at(last['follow_track'],moment)
            self.assertGreater(abs(offset[1]),500)
            from app_plugins.builtin.batch_text_video.caption_style_render import motion_at
            position=motion_at(last,moment)
            self.assertLess(last['y']+position[2],960)
            self.assertGreater(last['y']+position[2],0)

    def test_image_ui_roundtrip_and_quiet_help_labels(self):
        panel=CaptionProperties();self.addCleanup(panel.deleteLater);panel.set_layer(self.layer())
        updates=[];panel.changed.connect(updates.append)
        panel.controls['caption_cursor_mode'].setCurrentIndex(panel.controls['caption_cursor_mode'].findData('image'))
        with patch.object(QtWidgets.QFileDialog,'getOpenFileName',return_value=(str(self.image),'')):panel.choose_cursor_image()
        panel.controls['caption_cursor_scale'].setValue(135);panel.controls['caption_cursor_offset_x'].setValue(9)
        self.assertEqual(updates[-1]['caption_cursor_image'],str(self.image));self.assertEqual(updates[-1]['caption_cursor_scale'],135)
        self.assertEqual(updates[-1]['caption_cursor_offset_x'],9)
        self.assertTrue(panel.controls['caption_cursor_color'].isHidden())
        self.assertTrue(panel.style_note.isHidden());self.assertTrue(panel.display_note.isHidden());self.assertTrue(panel.source_note.isHidden())
        self.assertTrue(panel.controls['caption_follow_mode'].toolTip())
        panel.show();self.app.processEvents()
        output=os.environ.get('LZX_CURSOR_QA')
        if output:panel.grab().save(str(Path(output)/'cursor-ui.png'))
        panel.close()

    def test_image_path_validation_and_disabled_image_does_not_load(self):
        for value in (None,True,'bad\npath'):
            with self.assertRaises(ValueError):self.layer(caption_cursor_image=value)
        for field,value in (('caption_cursor_scale',0),('caption_cursor_scale',float('nan')),('caption_cursor_offset_x',1001)):
            with self.assertRaises(ValueError):self.layer(**{field:value})
        layer=self.layer(caption_cursor=False,caption_cursor_mode='image',caption_cursor_image='missing.png')
        render_design(layer,540,960,words=self.words(2),duration=1,time=.4)

    def test_image_settings_save_and_undo_in_original_project_history(self):
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        state=fresh_state();layer=self.layer(id='cursor-test');state['settings']['layers']=[layer]
        store=Store(self.root/'state');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'):dialog=BatchTextVideoDialog(store=store)
        try:
            dialog.layer_panel.select_layer_id('cursor-test');editor=dialog.layer_panel.caption_properties
            editor.controls['caption_cursor_mode'].setCurrentIndex(editor.controls['caption_cursor_mode'].findData('image'))
            dialog.persist();self.assertEqual(dialog.layer_panel.current()['caption_cursor_mode'],'image')
            self.assertTrue(dialog.undo_edit());self.assertEqual(dialog.layer_panel.current()['caption_cursor_mode'],'bar')
            self.assertTrue(dialog.redo_edit());self.assertEqual(dialog.layer_panel.current()['caption_cursor_mode'],'image')
        finally:dialog.edit_timer.stop();dialog.close();dialog.deleteLater();self.app.processEvents()

    def test_image_caption_node_captures_resource_and_compiles_native_fragment(self):
        import threading
        from model.BackgroundTasks import TaskRunContext
        from model.node_workflow.contracts import SubtitleTrack,SubtitleCue
        from app_plugins.builtin.batch_text_video.workflow_nodes import build_caption
        from app_plugins.builtin.batch_text_video.workflow_caption import compile_caption
        from app_plugins.api import VideoFilterFragment
        context=TaskRunContext(threading.Event(),lambda *_:None,lambda *_:None)
        layer=self.layer(caption_timing='task',caption_cursor_mode='image',caption_cursor_image=str(self.image))
        subtitles=SubtitleTrack((SubtitleCue(0,1,'pokoj'),SubtitleCue(1.1,1.8,'Pane')),'',2)
        operation=build_caption({'subtitles':subtitles},{'caption':layer},context)['operations'][0]
        self.assertIn('cursor_image',dict(operation.assets))
        output=compile_caption(operation,self.root,context,dict(duration=2,video=dict(width=540,height=960,avg_frame_rate='20/1')))
        self.assertIsInstance(output[0],VideoFilterFragment);self.assertGreaterEqual(len(output[0].inputs),2)
        self.assertTrue(any('sendcmd=' in line for line in output[0].filters))
        if FFMPEG:
            from model.node_workflow.filter_graph import VideoFilterChain,append_chain
            args=[FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error','-filter_complex_threads','1',
                '-f','lavfi','-i','color=c=black:s=540x960:r=20:d=2']
            filters=[];append_chain(VideoFilterChain(output),args,filters,'0:v',1,final='final')
            script=self.root/'node-graph.txt';script.write_text(';'.join(filters),encoding='utf8')
            args += [filter_file_option(FFMPEG),str(script),'-map','[final]','-ss','0.5','-frames:v','1','-f','null','-']
            result=subprocess.run(args,capture_output=True,timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        self.image.write_bytes(b'changed')
        with self.assertRaises(Exception):compile_caption(operation,self.root,context,dict(duration=2,video=dict(width=540,height=960)))

    @unittest.skipUnless(FFMPEG,'Real FFmpeg required')
    def test_full_batch_renderer_composes_picture_cursor_and_cleans_temporary_assets(self):
        import wave
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe
        voice=self.root/'voice.wav'
        with wave.open(str(voice),'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b'\0\0'*12800)
        layer=self.layer(caption_timing='uniform',caption_cursor_mode='image',caption_cursor_image=str(self.image))
        state=fresh_state();state['settings'].update(layers=[layer],use_voice=True,music_enabled=False,video_encoder='cpu',
            ffmpeg_path=FFMPEG,aspect='custom',canvas_width=320,canvas_height=480,output_dir=str(self.root),overwrite=True)
        job=dict(id='render',path='',name='picture-cursor',title='',body='pokoj Pane',voice_path=str(voice))
        before=copy.deepcopy((state,job))
        output,_=render_static_text(job,state,self.root/'private')
        self.assertTrue(output.is_file());self.assertEqual((state,job),before)
        self.assertAlmostEqual(probe(output,str(Path(FFMPEG).with_name('ffprobe.exe')))['duration'],.8,delta=.1)
        self.assertFalse(any(p.name.startswith('.text-video-') for p in self.root.iterdir()))

    @unittest.skipUnless(FFMPEG,'Real FFmpeg required')
    def test_native_png_cursor_matches_preview_after_follow_scroll(self):
        import numpy as np
        words=self.words(20);duration=8.;moment=6.05
        for mode,feather in (('bar',0),('image',0),('image',8)):
            layer=self.layer(caption_cursor_mode=mode,caption_cursor_image=str(self.image),caption_clip_feather=feather,caption_cursor_color='#00ffff')
            path=self.root/f'cursor-{mode}-{feather}.ass';write_caption_ass(path,words,layer,540,960,duration)
            self.assertEqual(path.with_suffix('.cursor.json').is_file(),mode=='image')
            args=[FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error','-filter_complex_threads','1',
                '-f','lavfi','-i',f'color=c=black:s=540x960:r=20:d={duration}']
            filters=[];used=append_caption(args,filters,'0:v','output',path,layer,540,960,duration,20,1)
            if mode=='image':self.assertGreaterEqual(used,2)
            graph=self.root/f'graph{feather}.txt';graph.write_text(';'.join(filters),encoding='utf8')
            target=self.root/f'frame{feather}.png'
            args += [filter_file_option(FFMPEG),str(graph),'-map','[output]','-ss',str(moment),'-frames:v','1','-update','1',str(target)]
            process=subprocess.run(args,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(process.returncode,0,process.stderr.decode('utf8','replace'))
            actual=QtGui.QImage(str(target));preview,_=render_design(layer,540,960,words=words,duration=duration,time=moment)
            output=os.environ.get('LZX_CURSOR_QA')
            if output:
                actual.save(str(Path(output)/f'cursor-{mode}-native-{feather}.png'))
                preview.save(str(Path(output)/f'cursor-{mode}-preview-{feather}.png'))
            def bounds(image):
                image=image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
                data=np.frombuffer(image.constBits().asstring(image.sizeInBytes()),np.uint8).reshape(960,540,4)
                color=data[:,:,:3].astype(np.int16)
                ys,xs=np.where((color[:,:,1]>60)&(color[:,:,2]>60)&(color[:,:,1]-color[:,:,0]>20)&(color[:,:,2]-color[:,:,0]>20)&(data[:,:,3]>80))
                self.assertGreater(len(xs),1)
                return np.array([xs.min(),ys.min(),xs.max(),ys.max()])
            self.assertLessEqual(max(abs(bounds(actual)-bounds(preview))),3)
            output=os.environ.get('LZX_CURSOR_QA')
            if output:actual.save(str(Path(output)/f'cursor-native-{feather}.png'))
