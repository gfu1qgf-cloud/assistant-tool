import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.caption_clip import clip_bounds,mask_image,append_caption
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives,render_design,write_primitives_ass
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.engine import filter_file_option

FFMPEG=os.environ.get('NODE_FFMPEG','')


class VerticalClipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def layer(self,**values):
        layer=new_follow_caption();layer.update(font='Arial',font_min=48,font_max=48,
            x=50,y=50,width=37.5,height=40,caption_follow_mode='line',caption_clip_feather=20,
            outline_width=0,shadow_enabled=False,background_enabled=False)
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none',karaoke_highlight=False)
        layer.update(values);return normalize_layers([layer])[0]

    def primitive(self,layer,*,picture=''):
        path=QtGui.QPainterPath();path.addRect(0,0,280 if not picture else 24,160 if not picture else 24)
        bounds=clip_bounds(layer,320,240)
        value=dict(native=path,x=20. if not picture else 248.,y=40. if not picture else 108.,
            width=path.boundingRect().width(),height=path.boundingRect().height(),start=0.,end=2.,
            color=['#ffffff'],opacity=1.,border=0.,border_color='#000000',blur=0.,rotation=0.,z=0,
            motion='none',motion_ms=0,out='none',out_ms=0,follow_track=((0.,0.,0.),(2.,0.,0.)),
            screen_clip=(bounds.left(),bounds.top(),bounds.right(),bounds.bottom()))
        if picture:value.update(cursor_image=picture,z=2)
        return value

    def test_default_releases_horizontal_clip_for_hard_and_soft_follow(self):
        for amount in (0,20):
            layer=self.layer(caption_clip_feather=amount)
            values,_=make_primitives([dict(text='pokoj',start=0,end=1)],layer,320,240,2)
            tracked=[p for p in values if p.get('follow_track')]
            self.assertTrue(tracked)
            self.assertTrue(all(p['screen_clip'][0]==0 and p['screen_clip'][2]==320 for p in tracked))

    def test_vertical_mask_has_no_horizontal_gradient_or_side_cut(self):
        layer=self.layer();mask=mask_image(layer,320,240)
        for y in (72,75,90,120,150,167):
            self.assertEqual(mask.pixelColor(0,y).red(),mask.pixelColor(160,y).red())
            self.assertEqual(mask.pixelColor(319,y).red(),mask.pixelColor(160,y).red())
        self.assertEqual(mask.pixelColor(0,120).red(),255)
        self.assertEqual(mask.pixelColor(160,20).red(),0)
        self.assertLess(mask.pixelColor(160,75).red(),mask.pixelColor(160,90).red())

    def test_four_edge_option_and_mask_cache_remain_independent(self):
        vertical=self.layer(width=100);all_edges={**vertical,'caption_clip_axis':'all'}
        a=mask_image(vertical,320,240);b=mask_image(all_edges,320,240)
        self.assertEqual(a.pixelColor(0,120).red(),255);self.assertLess(b.pixelColor(0,120).red(),20)
        self.assertEqual(mask_image(vertical,320,240).pixelColor(0,120).red(),255)

    def test_old_config_defaults_vertical_preserves_amount_and_rejects_bad_axis(self):
        layer=self.layer();layer.pop('caption_clip_axis');before=copy.deepcopy(layer)
        value=normalize_layers([layer])[0];self.assertEqual(layer,before)
        self.assertEqual(value['caption_clip_axis'],'vertical');self.assertEqual(value['caption_clip_feather'],20)
        for invalid in (None,True,[],{},'horizontal'):
            with self.assertRaises(ValueError):self.layer(caption_clip_axis=invalid)

    def test_basic_control_names_direction_and_saves_through_original_signal(self):
        panel=CaptionProperties();self.addCleanup(panel.deleteLater);panel.set_layer(self.layer())
        control=panel.controls['caption_clip_axis']
        self.assertEqual(control.currentData(),'vertical');self.assertIn('不裁左右',control.currentText())
        self.assertEqual(panel.forms['timing'].labelForField(panel.controls['caption_clip_feather']).text(),'边缘渐隐')
        values=[];panel.changed.connect(values.append)
        control.setCurrentIndex(control.findData('all'));self.assertEqual(values[-1]['caption_clip_axis'],'all')
        root=os.environ.get('LZX_VERTICAL_CLIP_QA')
        if root:panel.show();self.app.processEvents();panel.basic_widget.grab().save(str(Path(root)/'vertical-clip-controls.png'));panel.close()

    def test_auto_wrapping_font_size_positions_and_follow_focus_do_not_change(self):
        words=[dict(text=word,start=i*.25,end=(i+1)*.25)
               for i,word in enumerate(('Pane prosím ochraňuj rodinu neprehliadaj '.split())*18)]
        for mode in ('line','word'):
            layer=self.layer(x=40,caption_follow_mode=mode,caption_per_line=4)
            vertical,events=make_primitives(words,layer,540,960,23)
            other,reference=make_primitives(words,{**layer,'caption_clip_axis':'all'},540,960,23)
            keys=('text','x','y','width','height','line','_font_pixel_size','start','spoken_end')
            self.assertEqual([tuple(e[k] for k in keys) for e in events],
                             [tuple(e[k] for k in keys) for e in reference])
            self.assertEqual(next(p['follow_track'] for p in vertical if p.get('follow_track')),
                             next(p['follow_track'] for p in other if p.get('follow_track')))
            rows={}
            for event in events:rows.setdefault(event['line'],[]).append(event['text'])
            self.assertGreater(len(rows),20)
            self.assertLess(max(map(len,rows.values())),4)  # width still forces earlier wrapping
            self.assertEqual({e['_font_pixel_size'] for e in events},{24})
            self.assertGreater(events[-1]['y'],960)

    def test_complete_editor_composition_does_not_reapply_horizontal_box_clip(self):
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.layout import render_overlay
        for axis in ('vertical','all'):
            layer=self.layer(caption_clip_axis=axis);primitive=self.primitive(layer)
            settings=fresh_state()['settings'];settings.update(layers=[layer],darkness=0,
                _editor_timing=dict(duration=2,time=1))
            with patch('app_plugins.builtin.batch_text_video.caption_style_render.make_primitives',return_value=([primitive],[])):
                image,_=render_overlay('','test',320,240,settings)
            self.assertEqual(image.pixelColor(40,120).alpha(),255 if axis=='vertical' else 0)

    @unittest.skipUnless(FFMPEG,'Real FFmpeg required')
    def test_native_and_preview_preserve_side_pixels_and_fade_only_top_bottom(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for axis,amount in (('vertical',0),('vertical',20),('all',20)):
                layer=self.layer(caption_clip_axis=axis,caption_clip_feather=amount)
                primitive=self.primitive(layer);ass=root/f'{axis}-{amount}.ass'
                write_primitives_ass(ass,[primitive],[],layer,320,240)
                with patch('app_plugins.builtin.batch_text_video.caption_style_render.make_primitives',return_value=([primitive],[])):
                    qt,_=render_design(layer,320,240,words=[dict(text='test',start=0,end=2)],duration=2,time=1)
                args=[FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error','-filter_complex_threads','1',
                    '-f','lavfi','-i','color=c=blue:s=320x240:r=10:d=2']
                filters=[];append_caption(args,filters,'0:v','out',ass,layer,320,240,2,10,1)
                script=root/'graph.txt';script.write_text(';'.join(filters),encoding='utf8');target=root/'frame.png'
                result=subprocess.run(args+[filter_file_option(FFMPEG),str(script),'-map','[out]','-ss','1','-frames:v','1',str(target)],
                    capture_output=True,timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));native=QtGui.QImage(str(target))
                for x,y in ((40,120),(280,120),(160,73),(160,90),(160,167),(40,20)):
                    self.assertAlmostEqual(native.pixelColor(x,y).red(),qt.pixelColor(x,y).alpha(),delta=5)
                self.assertEqual(qt.pixelColor(40,120).alpha(),255 if axis=='vertical' else 0)
                output=os.environ.get('LZX_VERTICAL_CLIP_QA')
                if output:qt.save(str(Path(output)/f'{axis}-{amount}-qt.png'));native.save(str(Path(output)/f'{axis}-{amount}-native.png'))

    @unittest.skipUnless(FFMPEG,'Real FFmpeg required')
    def test_picture_cursor_also_releases_sides_for_hard_and_soft_clip(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);image=root/'cursor.png'
            pixels=QtGui.QImage(24,24,QtGui.QImage.Format.Format_ARGB32_Premultiplied);pixels.fill(QtGui.QColor('cyan'));pixels.save(str(image))
            for axis,amount in (('vertical',0),('vertical',20),('all',20)):
                layer=self.layer(caption_clip_axis=axis,caption_clip_feather=amount)
                cursor=self.primitive(layer,picture=str(image));text=self.primitive(layer)
                text['native']=QtGui.QPainterPath();text['native'].addRect(0,0,5,5);text.update(x=150.,y=120.,width=5.,height=5.)
                ass=root/f'image-{axis}-{amount}.ass';write_primitives_ass(ass,[text,cursor],[],layer,320,240)
                args=[FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error','-filter_complex_threads','1',
                    '-f','lavfi','-i','color=c=blue:s=320x240:r=10:d=2']
                filters=[];append_caption(args,filters,'0:v','out',ass,layer,320,240,2,10,1)
                script=root/'graph.txt';script.write_text(';'.join(filters),encoding='utf8');target=root/'image.png'
                result=subprocess.run(args+[filter_file_option(FFMPEG),str(script),'-map','[out]','-ss','1','-frames:v','1',str(target)],
                    capture_output=True,timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                native=QtGui.QImage(str(target));self.assertAlmostEqual(native.pixelColor(255,120).green(),255 if axis=='vertical' else 0,delta=5)
