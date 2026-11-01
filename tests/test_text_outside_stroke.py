import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass
from app_plugins.builtin.batch_text_video.caption_style_render import render_design
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, _text_layout
from app_plugins.builtin.batch_text_video.text_components import draw_text_groups
from app_plugins.builtin.batch_text_video.text_outline import outside_ring, layout_path


class OutsideStrokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self, **values):
        layer = new_caption()
        layer.update(font='Arial', font_weight=400, font_min=90, font_max=90,
            color='#ffffff', outline_color='#ff0000', outline_opacity=100,
            outline_width=15, outline_position='outside', shadow_enabled=False,
            background_enabled=False, caption_preset='appear',
            x=50,y=50,width=96,height=90, text='thin il BO')
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none',karaoke_highlight=False)
        layer.update(values)
        return normalize_layers([layer])[0]

    def static(self, layer, groups):
        image = QtGui.QImage(700,200,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        draw_text_groups(painter,groups,layer,30,30,1)
        painter.end()
        return image

    def test_legacy_defaults_and_invalid_values(self):
        layer=self.layer();layer.pop('outline_position')
        before=copy.deepcopy(layer)
        self.assertEqual(normalize_layers([layer])[0]['outline_position'],'center')
        self.assertEqual(layer,before)
        for value in ('inside','outer',None,True,[],{}):
            with self.subTest(value=value),self.assertRaises(ValueError):self.layer(outline_position=value)

    def test_geometry_is_outside_ink_with_round_corners_and_exact_extent(self):
        path=QtGui.QPainterPath();path.addRect(10,10,50,30)
        ring=outside_ring(path,12)
        self.assertTrue(ring.intersected(path).simplified().isEmpty())
        self.assertEqual(ring.boundingRect(),QtCore.QRectF(-2,-2,74,54))
        self.assertTrue(ring.contains(QtCore.QPointF(5,20)))
        self.assertFalse(ring.contains(QtCore.QPointF(20,20)))
        self.assertTrue(outside_ring(path,0).isEmpty())

    def test_static_thick_border_preserves_all_opaque_glyph_pixels(self):
        layer=self.layer()
        groups,_=_text_layout('thin il BO',layer['font'],90,620,'left',1.15,layer,1)
        plain=self.static({**layer,'outline_width':0},groups)
        actual=self.static(layer,groups)
        opaque=outside=0
        for y in range(200):
            for x in range(700):
                a,b=plain.pixelColor(x,y),actual.pixelColor(x,y)
                if a.alpha()==255:
                    self.assertEqual(b,a)
                    opaque+=1
                elif a.alpha()==0 and b.alpha()>240:
                    outside+=1
        self.assertGreater(opaque,1000)
        self.assertGreater(outside,1000)
        qa=os.environ.get('LZX_OUTSIDE_STROKE_QA')
        if qa:actual.save(str(Path(qa)/'static-outside.png'))

    def test_static_shaped_paths_cover_bidi_accents_and_decorations(self):
        layer=self.layer(underline=True,strikeout=True)
        groups,_=_text_layout('Božia שלום',layer['font'],60,620,'left',1.15,layer,1)
        path=layout_path(groups[0][0])
        self.assertFalse(path.isEmpty())
        self.assertTrue(outside_ring(path,9).intersected(path).simplified().isEmpty())
        plain=self.static({**layer,'outline_width':0},groups)
        actual=self.static(layer,groups)
        for y in range(200):
            for x in range(700):
                if plain.pixelColor(x,y).alpha()==255:
                    self.assertEqual(actual.pixelColor(x,y),plain.pixelColor(x,y))

    def test_upright_vertical_paths_and_rendering_are_supported(self):
        from app_plugins.builtin.batch_text_video.vertical_text import text_layout
        layer=self.layer(writing_mode='horizontal')
        for mode in ('vertical_lr','vertical_rl'):
            groups,_,_=text_layout('中文\nAb',layer['font'],50,600,150,'left',1.15,
                                  {**layer,'writing_mode':mode},1)
            path=layout_path(groups[0][0]);self.assertFalse(path.isEmpty())
            self.assertTrue(outside_ring(path,10).intersected(path).simplified().isEmpty())
            self.assertFalse(self.static(layer,groups).isNull())

    def test_caption_geometry_preserves_fill_origin_motion_and_outline_alpha(self):
        from app_plugins.builtin.batch_text_video.caption_appearance import add_foreground
        path=QtGui.QPainterPath();path.addRect(0,0,20,40)
        captured=[]
        def add(path,*args,**kw):captured.append((path,args,kw))
        layer=self.layer(outline_opacity=35)
        add_foreground(add,path,10,20,0,2,'#ffffff',.5,layer,1,{'motion':'pop','motion_ms':160})
        ring,fill=captured
        self.assertTrue(ring[0].intersected(path).simplified().isEmpty())
        self.assertAlmostEqual(ring[1][-1],.175)
        self.assertEqual(ring[2]['origin_x'],10)
        self.assertEqual(ring[2]['origin_y'],20)
        self.assertEqual(ring[2]['motion'],fill[2]['motion'])
        self.assertEqual(fill[2]['border'],0)
        self.assertEqual(fill[0],path)

    def test_catalog_multilayer_and_follow_use_the_same_exterior_paths(self):
        from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives
        layer=self.layer(caption_follow_mode='line', caption_effects={
            'anim_in_type':'none','anim_out_type':'none','karaoke_highlight':False,
            'stroke_expand_enabled':True,'stroke_expand_layers':2,'stroke_expand_feather':0})
        primitives,events=make_primitives([dict(text='thin',start=0,end=1)],layer,540,300,2)
        path=events[0]['_native_path']
        strokes=[p for p in primitives if p['z'] in (-2,-1)]
        self.assertEqual(len(strokes),4)
        for stroke in strokes:
            self.assertEqual(stroke['border'],0)
            self.assertTrue(stroke['native'].intersected(path).simplified().isEmpty())
            self.assertIn('follow_track',stroke)
            self.assertEqual(stroke['origin_x'],path.boundingRect().width()/2)

    def test_long_follow_caption_keeps_native_ass_bounded(self):
        words=[dict(text='Slovensko',start=i*.25,end=(i+1)*.25) for i in range(180)]
        layer=self.layer(caption_follow_mode='line',caption_per_line=2,font_min=48,font_max=48)
        with tempfile.TemporaryDirectory() as directory:
            ass=Path(directory)/'long.ass'
            events=write_caption_ass(ass,words,layer,540,960,46)
            self.assertEqual(len(events),180)
            self.assertLess(ass.stat().st_size,64*1024*1024)

    def test_option_next_to_thickness_persists_in_project_node_and_parameter_window(self):
        from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties
        from app_plugins.builtin.batch_text_video.caption_parameter_window import parameter_targets
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        from app_plugins.builtin.batch_text_video.store import Store
        panel=TextBoxProperties();self.addCleanup(panel.close)
        layer=self.layer();panel.set_layer(layer)
        control=panel.controls['outline_position']
        form=panel.style_sections.sections['outline'].content.layout()
        self.assertEqual(form.getWidgetPosition(control)[0]+1,
                         form.getWidgetPosition(panel.controls['outline_width'])[0])
        changes=[];panel.changed.connect(changes.append)
        control.setCurrentIndex(control.findData('center'))
        self.assertEqual(changes[-1]['outline_position'],'center')
        control.setCurrentIndex(control.findData('outside'))
        self.assertEqual(changes[-1]['outline_position'],'outside')
        self.assertEqual(apply_style(layer,PREFIX+'默认白字')['outline_position'],'outside')
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);state=store.load();state['settings']['layers']=normalize_layers([layer])
            store.save(state,0)
            saved=next(v for v in store.load()['settings']['layers'] if v['id']==layer['id'])
            self.assertEqual(saved['outline_position'],'outside')
        edits=[];editor=CaptionNodeEditor({},edits.append);self.addCleanup(editor.close)
        editor.mode.setCurrentIndex(1)
        self.assertEqual(parameter_targets(editor.panel.caption_properties)[('layer','outline_position')][1],'描边位置')
        node_control=editor.panel.text_properties.controls['outline_position']
        node_control.setCurrentIndex(node_control.findData('outside'))
        self.assertEqual(edits[-1]['caption']['outline_position'],'outside')
        qa=os.environ.get('LZX_OUTSIDE_STROKE_QA')
        if qa:
            panel.style_sections.sections['outline'].set_expanded(True)
            panel.resize(580,950);panel.show();self.app.processEvents()
            panel.style_sections.sections['outline'].grab().save(str(Path(qa)/'outline-ui.png'))

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_native_thick_outline_preserves_real_glyphs_and_matches_qt(self):
        words=[dict(text='thin',start=0,end=2)]
        layer=self.layer(font_min=150,font_max=150)
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            plain,_=render_design({**layer,'outline_width':0},540,300,words=words,duration=2,time=1)
            qt,_=render_design(layer,540,300,words=words,duration=2,time=1)
            ass=folder/'caption.ass'
            write_caption_ass(ass,words,layer,540,300,2)
            # Explicit RGB matrix makes the pixel comparison independent of
            # libass's legacy limited-range colour compatibility conversion.
            ass.write_text(ass.read_text('utf8').replace('YCbCr Matrix: TV.601','YCbCr Matrix: None'),encoding='utf8')
            run=subprocess.run([os.environ['NODE_FFMPEG'],'-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','color=c=black:s=540x300:r=1:d=2','-vf','format=rgb24,ass=caption.ass',
                '-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],cwd=folder,capture_output=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stderr.decode('utf8','replace'))
            native=QtGui.QImage.fromData(run.stdout,'PNG');self.assertFalse(native.isNull())
            protected=0;outside=0;safe_outline=0;matched_outline=0
            for y in range(300):
                for x in range(540):
                    p,q,n=plain.pixelColor(x,y),qt.pixelColor(x,y),native.pixelColor(x,y)
                    if p.alpha()==255:
                        self.assertGreater(q.green(),245)
                        if all(plain.pixelColor(x+dx,y+dy).alpha()==255 for dx,dy in ((-2,0),(2,0),(0,-2),(0,2))):
                            protected+=1;self.assertGreater(n.green(),230)
                    if p.alpha()==0 and q.alpha()==255 and q.red()>240 and q.green()<10:
                        outside+=1
                        neighbors=[qt.pixelColor(x+dx,y+dy) for dx,dy in ((-2,0),(2,0),(0,-2),(0,2))]
                        if all(v.alpha()==255 and v.red()>240 and v.green()<10 for v in neighbors):
                            safe_outline+=1
                            matched_outline+=n.red()>230 and n.green()<25
            self.assertGreater(protected,50)
            self.assertGreater(outside,400)
            self.assertGreater(safe_outline,100)
            self.assertGreater(matched_outline/safe_outline,.9)
            qa=os.environ.get('LZX_OUTSIDE_STROKE_QA')
            if qa:
                qt.save(str(Path(qa)/'caption-outside-qt.png'))
                native.save(str(Path(qa)/'caption-outside-export.png'))


if __name__=='__main__':unittest.main()
