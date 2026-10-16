"""Measured line backgrounds, asymmetric expansion and shared render parity."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.layout import _text_layout, ensure_fonts
from app_plugins.builtin.batch_text_video.text_background import text_rectangles, background_path, draw_background
from app_plugins.builtin.batch_text_video.scrolling_text import render_scroll_assets, render_scroll_preview, scroll_timing
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel

FFMPEG = os.environ.get('NODE_FFMPEG','')


class TextBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            if isinstance(widget,LayerPanel): widget.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()

    def layer(self,**values):
        layer = new_text_component()
        layer.update(id='text',text='MODLITBA, V KTOREJ ZLO\nSTRÁCA MOC PANE\nAmen. 🙏🌹',
            font='Open Sans',font_min=48,font_max=48,x=50,y=50,width=90,height=85,
            color='#00ffff',outline_width=0,vertical='center',background_enabled=True,
            background_color='#7f2732',background_opacity=100,background_radius=10,
            background_mode='text')
        layer.update(values)
        return normalize_layers([layer])[0]

    def groups(self,layer):
        return _text_layout(layer['text'],layer['font'],48,800,layer['align'],layer['line_spacing'],layer)[0]

    def test_each_line_tracks_actual_width_and_alignment_not_whole_frame(self):
        for align in ('left','center','right'):
            layer = self.layer(text='Dlhšia modlitba\nAmen.',align=align)
            rects = text_rectangles(self.groups(layer),10,20)
            self.assertEqual(len(rects),2); self.assertGreater(rects[0].width(),rects[1].width()*1.5)
            if align=='left': self.assertAlmostEqual(rects[0].left(),rects[1].left(),delta=1)
            elif align=='center': self.assertAlmostEqual(rects[0].center().x(),rects[1].center().x(),delta=1)
            else: self.assertAlmostEqual(rects[0].right(),rects[1].right(),delta=1)

    def test_unicode_and_empty_lines_have_measured_background_not_character_guesses(self):
        layer = self.layer(text='Á Ľ Š Ť Ž\n\n🙏🌹\n  ')
        self.assertEqual(len(text_rectangles(self.groups(layer),0,0)),2)

    def test_four_extensions_are_independent_and_do_not_reflow_or_resize_text(self):
        layer = self.layer(); groups = self.groups(layer)
        base = background_path(layer,groups,0,0,1,QtCore.QRectF(0,0,800,900)).boundingRect()
        expanded = {**layer,'background_extra_left':12,'background_extra_right':31,
                    'background_extra_top':5,'background_extra_bottom':19}
        rect = background_path(expanded,groups,0,0,1,QtCore.QRectF(0,0,800,900)).boundingRect()
        self.assertAlmostEqual(base.left()-rect.left(),12); self.assertAlmostEqual(rect.right()-base.right(),31)
        self.assertAlmostEqual(base.top()-rect.top(),5); self.assertAlmostEqual(rect.bottom()-base.bottom(),19)
        a,sa = render_text_box(layer,1080,1500); b,sb = render_text_box(expanded,1080,1500)
        self.assertEqual(sa,sb); self.assertNotEqual(a,b)
        text_only,_ = render_text_box({**layer,'background_enabled':False},1080,1500)
        for x in range(0,1080,3):
            for y in range(0,1500,3):
                pixel = text_only.pixelColor(x,y)
                if pixel.alpha()==255 and pixel.name()=='#00ffff':
                    self.assertEqual(a.pixelColor(x,y),b.pixelColor(x,y))

    def test_joining_and_opacity_do_not_darken_overlap_or_fill_blank_paragraph(self):
        layer = self.layer(text='Amen.\nAmen.',background_opacity=50,background_extra_top=10,background_extra_bottom=10)
        groups = self.groups(layer)
        image = QtGui.QImage(900,300,QtGui.QImage.Format.Format_ARGB32_Premultiplied); image.fill(0)
        painter = QtGui.QPainter(image)
        draw_background(painter,layer,groups,20,20,1,QtCore.QRectF(0,0,900,300)); painter.end()
        self.assertLessEqual(max(image.pixelColor(x,y).alpha() for x in range(0,900,3) for y in range(0,300,3)),128)
        layer = self.layer(text='Amen.\n\nAmen.',background_extra_top=6,background_extra_bottom=6)
        rects = text_rectangles(self.groups(layer),0,0)
        path = background_path(layer,self.groups(layer),0,0,1,QtCore.QRectF(0,0,800,900))
        middle = QtCore.QPointF(rects[0].center().x(),(rects[0].bottom()+rects[1].top())/2)
        self.assertFalse(path.contains(middle))

    def test_vertical_mode_follows_columns_and_supports_asymmetric_extents(self):
        from app_plugins.builtin.batch_text_video.vertical_text import text_layout
        layer = self.layer(text='主赐平安\n祷告',writing_mode='vertical_rl')
        groups,*_ = text_layout(layer['text'],layer['font'],48,800,900,layer['align'],layer['line_spacing'],layer)
        rects = text_rectangles(groups,0,0)
        self.assertEqual(len(rects),2); self.assertNotEqual(rects[0].height(),rects[1].height())
        before = background_path(layer,groups,0,0,1,QtCore.QRectF(0,0,800,900)).boundingRect()
        after = background_path({**layer,'background_extra_right':20},groups,0,0,1,QtCore.QRectF(0,0,800,900)).boundingRect()
        self.assertAlmostEqual(after.right()-before.right(),20)

    def test_old_box_background_pixels_are_preserved_and_can_expand(self):
        layer = self.layer(background_mode='box')
        old = {key:value for key,value in layer.items() if key not in {
            'background_mode','background_join_lines','background_extra_left','background_extra_right','background_extra_top','background_extra_bottom'}}
        before,_ = render_text_box(old,540,750); after,_ = render_text_box(layer,540,750)
        self.assertEqual(before,after)
        expanded,_ = render_text_box({**layer,'background_extra_left':40},540,750)
        self.assertNotEqual(expanded,after)

    def test_scroll_background_moves_with_text_and_extensions_leave_timing_unchanged(self):
        layer = self.layer(text='Amen.\nBožia láska\n'*10,text_animation='up',height=30)
        a = render_scroll_assets(layer,360,640)
        expanded = {**layer,'background_extra_left':12,'background_extra_top':30,'background_extra_bottom':20}
        b = render_scroll_assets(expanded,360,640)
        self.assertEqual(a['text_height'],b['text_height']); self.assertEqual(a['font_size'],b['font_size'])
        self.assertEqual(scroll_timing(layer,10,a['height'],a['text_height']),scroll_timing(expanded,10,b['height'],b['text_height']))
        self.assertGreater(b['strip'].height(),a['strip'].height())
        self.assertEqual(b['strip_top'],10)
        early,_ = render_scroll_preview(expanded,360,640,duration=10,time=0)
        middle,_ = render_scroll_preview(expanded,360,640,duration=10,time=5)
        self.assertNotEqual(early,middle)

    def test_ui_and_save_keep_mode_extensions_and_node_reuses_same_controls(self):
        with tempfile.TemporaryDirectory() as root:
            layer = self.layer(); panel = LayerPanel([layer],root); panel.select_layer_id('text')
            controls = panel.text_properties.controls
            self.assertEqual(controls['background_mode'].currentData(),'text')
            for side,n in (('left',12),('right',30),('top',6),('bottom',9)):
                controls['background_extra_'+side].setValue(n)
                self.assertEqual(panel.current()['background_extra_'+side],n)
            state = fresh_state(); state['settings']['layers'] = panel.values()
            store = Store(root); store.save(state,0)
            saved = next(item for item in store.load()['settings']['layers'] if item['id']=='text')
            self.assertEqual(saved['background_extra_right'],30)
            from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
            editor = LayerNodeEditor({'layer':saved,'pool_mode':'rotate','offset':0},lambda _v:None)
            self.assertEqual(editor.panel.text_properties.controls['background_mode'].currentData(),'text')
            editor.deleteLater()

    def test_invalid_values_are_rejected_before_rendering(self):
        for change in ({'background_mode':'bad'},{'background_mode':[]},{'background_extra_left':float('nan')},
                       {'background_extra_top':-1},{'background_extra_right':301},{'background_join_lines':'true'}):
            with self.subTest(change=change),self.assertRaises(ValueError): self.layer(**change)


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),'FFmpeg not configured')
class TextBackgroundMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([]); ensure_fonts()

    def test_real_preview_export_and_scroll_use_the_same_background(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'source.mp4'
            result = subprocess.run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
                'color=c=0x304050:s=160x284:r=10:d=1','-c:v','libx264',str(path)],capture_output=True,timeout=30,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0)
            layer = TextBackgroundTests.layer(self,background_extra_left=12,background_extra_right=12,
                                             background_extra_top=6,background_extra_bottom=6)
            state = fresh_state(); state['settings'].update(ffmpeg_path=FFMPEG,music_enabled=False,darkness=0,
                layers=normalize_layers([layer]+[dict(item,enabled=False) for item in DEFAULT_LAYERS]))
            job = {'id':'job','path':str(path),'title':'','body':'','task_dir':root,'name':'final'}; state['jobs']=[job]
            for animation in ('none','up'):
                state['settings']['layers'][0].update(text_animation=animation)
                if animation=='up': state['settings']['layers'][0]['scroll_start_offset']=-25
                final,_ = render_static_text({**job,'name':'final-'+animation},state,root)
                played,_ = render_static_text({**job,'name':'preview-'+animation},state,root,preview=True)
                frames = []
                for source in (final,played):
                    result = subprocess.run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-i',str(source),
                        '-vf','scale=540:960:flags=lanczos','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                        capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                    frames.append(np.frombuffer(result.stdout,np.uint8).astype(float))
                self.assertLess(np.abs(frames[0]-frames[1]).mean(),3)
