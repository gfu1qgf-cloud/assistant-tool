import copy
import math
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.caption_cursor import normalize as normalize_cursor
from app_plugins.builtin.batch_text_video.caption_display import reveal_events
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives,motion_at,render_design
from app_plugins.builtin.batch_text_video.caption_basic_render import make_primitives as make_basic
from app_plugins.builtin.batch_text_video.caption_styles import uses_style_renderer
from app_plugins.builtin.batch_text_video.captions import write_caption_ass
from app_plugins.builtin.batch_text_video.caption_clip import append_caption
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.engine import filter_file_option
from test_caption_cursor import CursorTests,FFMPEG


class ContinuousCursorTests(CursorTests):
    def layer(self,**values):
        return super().layer(**{'caption_cursor_continuous':True,**values})

    def gapped_words(self):return [dict(text='ab',start=0.,end=.6),dict(text='cd',start=1.4,end=2.)]

    def primitives(self,layer,words=None,duration=2.4):
        function=make_primitives if uses_style_renderer(layer) else make_basic
        return function(words or self.gapped_words(),layer,540,960,duration)

    def cursors(self,primitives):return [p for p in primitives if p.get('cursor_track')]

    def position(self,primitives,time):
        active=[p for p in self.cursors(primitives) if p['start']<=time<p['end']]
        self.assertEqual(len(active),1)
        value=active[0];_size,x,y,alpha,_blur=motion_at(value,time)
        self.assertEqual(alpha,1.)
        return value['x']+x,value['y']+y

    def test_defaults_enable_continuous_without_mutating_legacy_input_and_validate_values(self):
        value=self.layer();value.pop('caption_cursor_continuous');value.pop('caption_cursor_move_ms');before=copy.deepcopy(value)
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        layer=normalize_layers([value])[0]
        self.assertTrue(layer['caption_cursor_continuous']);self.assertEqual(layer['caption_cursor_move_ms'],120)
        self.assertEqual(value,before)
        for key,bad in (('caption_cursor_continuous',1),('caption_cursor_move_ms',True),('caption_cursor_move_ms',-1),('caption_cursor_move_ms',1001)):
            with self.subTest(key=key,bad=bad),self.assertRaises(ValueError):normalize_cursor({**layer,key:bad})

    def test_cursor_never_disappears_between_letters_words_or_cues_in_both_renderers(self):
        for follow in ('none','line'):
            layer=self.layer(caption_follow_mode=follow)
            self.assertEqual(uses_style_renderer(layer),follow!='none')
            primitives,_=self.primitives(layer)
            cursor=self.cursors(primitives);end=max(p['end'] for p in cursor)
            for t in np.linspace(.01,end-.001,180):self.position(primitives,float(t))
            self.position(primitives,.9)

    def test_cursor_moves_from_previous_letter_after_next_reveal_and_holds_through_pause(self):
        primitives,_=self.primitives(self.layer())
        a=self.position(primitives,1.4);b=self.position(primitives,1.46);c=self.position(primitives,1.521)
        self.assertGreater(math.dist(a,b),1)
        self.assertLess(math.dist(a,b),math.dist(a,c))
        self.assertLess(math.dist(b,c),math.dist(a,c))
        self.assertEqual(self.position(primitives,.9),self.position(primitives,1.39))

    def test_disabled_mode_and_zero_ms_jump_directly_but_both_remain_visible(self):
        layer=self.layer(caption_cursor_continuous=False);primitives,_=self.primitives(layer)
        self.position(primitives,.9)
        self.assertGreater(math.dist(self.position(primitives,1.399),self.position(primitives,1.4)),1)
        self.assertEqual(self.position(primitives,1.4),self.position(primitives,1.45))
        instant,_=self.primitives(self.layer(caption_cursor_move_ms=0))
        self.assertGreater(math.dist(self.position(instant,1.399),self.position(instant,1.4)),1)
        self.assertEqual(self.position(instant,1.4),self.position(instant,1.45))

    def test_cursor_options_do_not_retime_words_or_grapheme_reveal(self):
        words=[dict(text='áno',start=.2,end=.8),dict(text='pokoj',start=1.4,end=2.)];before=copy.deepcopy(words)
        schedules=[]
        for continuous,milliseconds in ((False,120),(True,120),(True,0)):
            layer=self.layer(caption_cursor_continuous=continuous,caption_cursor_move_ms=milliseconds)
            _primitives,events=self.primitives(layer,words)
            schedules.append([(p['text'],p['start'],p['_cursor_until']) for p in reveal_events(events,layer)])
        self.assertEqual(schedules[0],schedules[1]);self.assertEqual(schedules[1],schedules[2]);self.assertEqual(words,before)

    def test_long_follow_has_shared_bounded_track_and_correct_late_position(self):
        for follow in ('line','word'):
            for mode in ('bar','image'):
                layer=self.layer(caption_follow_mode=follow,caption_cursor_mode=mode,caption_cursor_image=str(self.image))
                primitives,_=self.primitives(layer,self.words(90),36.)
                cursors=self.cursors(primitives)
                self.assertLess(len(cursors),10);self.assertEqual(len({id(p['cursor_track']) for p in cursors}),1)
                self.assertLess(len(cursors[0]['cursor_track']),1000)
                x,y=self.position(primitives,30.05);self.assertGreater(y,0);self.assertLess(y,960)
                self.assertGreater(x,0);self.assertLess(x,540)

    def test_ui_checkbox_duration_and_undo_save_roundtrip(self):
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        store=Store(self.root/'private');state=fresh_state();state['settings']['layers']=[self.layer(id='cursor-test')];store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=store)
            try:
                dialog.layer_panel.select_layer_id('cursor-test');editor=dialog.layer_panel.caption_properties
                self.assertEqual(editor.controls['caption_cursor_continuous'].text(),'连续动画')
                editor.controls['caption_cursor_continuous'].setChecked(False);self.assertTrue(dialog.persist())
                self.assertFalse(dialog.layer_panel.current()['caption_cursor_continuous'])
                self.assertTrue(dialog.undo_edit());self.assertTrue(dialog.layer_panel.current()['caption_cursor_continuous'])
                self.assertTrue(dialog.redo_edit());self.assertFalse(dialog.layer_panel.current()['caption_cursor_continuous'])
                editor.controls['caption_cursor_continuous'].setChecked(True)
                editor.controls['caption_cursor_move_ms'].setValue(240);self.assertTrue(dialog.persist())
                saved=next(l for l in store.load()['settings']['layers'] if l['id']=='cursor-test')
                self.assertTrue(saved['caption_cursor_continuous']);self.assertEqual(saved['caption_cursor_move_ms'],240)
                output=os.environ.get('LZX_CURSOR_QA')
                if output:editor.grab().save(str(Path(output)/'continuous-cursor-ui.png'))
            finally:
                dialog.close();dialog.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    @unittest.skipUnless(FFMPEG,'Real FFmpeg required')
    def test_real_bar_and_png_are_visible_in_gaps_and_match_preview_mid_motion(self):
        times=(.9,1.46,1.9);fps=50
        for follow,mode in (('none','bar'),('none','image'),('line','bar'),('line','image'),('word','bar'),('word','image')):
            with self.subTest(follow=follow,mode=mode):
                layer=self.layer(caption_follow_mode=follow,caption_cursor_mode=mode,caption_cursor_image=str(self.image),caption_cursor_color='#00ffff',caption_clip_feather=8 if follow!='none' else 0)
                path=self.root/(follow+'-'+mode+'.ass');write_caption_ass(path,self.gapped_words(),layer,540,960,2.4)
                args=[FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error','-filter_complex_threads','1','-f','lavfi','-i','color=c=black:s=540x960:r=50:d=2.4']
                filters=[];append_caption(args,filters,'0:v','output',path,layer,540,960,2.4,fps,1)
                select='+'.join(f'eq(n\\,{round(t*fps)})' for t in times)
                filters.append(f'[output]select={select}[selected]')
                script=self.root/(follow+'-'+mode+'.graph');script.write_text(';'.join(filters),encoding='utf8')
                args += [filter_file_option(FFMPEG),str(script),'-map','[selected]','-fps_mode','passthrough','-frames:v','3','-pix_fmt','rgba','-f','rawvideo','pipe:1']
                result=subprocess.run(args,capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,960,540,4);self.assertEqual(len(frames),3)
                def bounds(data):
                    # Thin ASS vectors blend through YUV chroma; identify hue,
                    # rather than requiring saturated RGB on a one-pixel line.
                    color=data[:,:,:3].astype(np.int16)
                    ys,xs=np.where((color[:,:,1]>60)&(color[:,:,2]>60)&(color[:,:,1]-color[:,:,0]>20)&(color[:,:,2]-color[:,:,0]>20)&(data[:,:,3]>80))
                    self.assertGreater(len(xs),1)
                    return np.array([xs.min(),ys.min(),xs.max(),ys.max()])
                for i,t in enumerate(times):
                    preview,_=render_design(layer,540,960,words=self.gapped_words(),duration=2.4,time=t)
                    image=preview.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
                    data=np.frombuffer(image.constBits().asstring(image.sizeInBytes()),np.uint8).reshape(960,540,4)
                    output=os.environ.get('LZX_CURSOR_QA')
                    if output:
                        QtGui.QImage(frames[i].tobytes(),540,960,540*4,QtGui.QImage.Format.Format_RGBA8888).copy().save(str(Path(output)/f'{follow}-{mode}-{t}-actual.png'))
                        preview.save(str(Path(output)/f'{follow}-{mode}-{t}-preview.png'))
                    self.assertLessEqual(max(abs(bounds(frames[i])-bounds(data))),4)
                output=os.environ.get('LZX_CURSOR_QA')
                if output:
                    QtGui.QImage(frames[1].tobytes(),540,960,540*4,QtGui.QImage.Format.Format_RGBA8888).copy().save(str(Path(output)/(follow+'-'+mode+'-continuous-native.png')))


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in ContinuousCursorTests.__dict__:
        if name.startswith('test_'):suite.addTest(ContinuousCursorTests(name))
    return suite
