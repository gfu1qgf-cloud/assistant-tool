import copy
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers,compose_layers,image_rect
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.captions import new_caption,caption_events
from app_plugins.builtin.batch_text_video.size_modes import MODES,effective
from app_plugins.builtin.batch_text_video.store import Store,fresh_state

FFMPEG=os.environ.get('NODE_FFMPEG','')


def rgba(image):
    image=image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()),np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4).copy()


class SizeModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.wide=self.picture('wide.png',120,40)
        self.tall=self.picture('tall.png',40,120)
    def picture(self,name,width,height,alpha=255):
        image=QtGui.QImage(width,height,QtGui.QImage.Format.Format_RGBA8888)
        for y in range(height):
            for x in range(width):
                axis=x/width if width>height else y/height
                image.setPixelColor(x,y,QtGui.QColor(*((255,0,0) if axis<1/3 else (0,255,0) if axis<2/3 else (0,0,255)),alpha))
        path=self.root/name;self.assertTrue(image.save(str(path)));return path
    def layer(self,path=None,mode='fixed',**values):
        layer=dict(id='visual',kind='image',path=str(path or self.wide),source='fixed',x=17,y=30,width=35,height=40,size_mode=mode)
        layer.update(values);return normalize_layers([layer])[0]
    def text(self,kind='text',mode='fixed',**values):
        layer=new_caption() if kind=='caption' else new_text_component()
        layer.update(kind=kind,id=kind if kind in {'title','body'} else layer['id'],size_mode=mode,
            x=25,y=35,width=30,height=45,text='Hello world. Normal text wraps without stretching.',
            font_min=24,font_max=24,padding=4,outline_width=0,shadow_enabled=False,background_enabled=False)
        layer.update(values);return normalize_layers([layer])[0]

    def test_missing_mode_preserves_existing_manual_geometry_and_pixels(self):
        old=self.layer();old.pop('size_mode');before=copy.deepcopy(old)
        actual=normalize_layers([old])[0]
        self.assertEqual(actual['size_mode'],'fixed')
        self.assertEqual((actual['x'],actual['y'],actual['width'],actual['height']),(17,30,35,40))
        self.assertTrue(np.array_equal(rgba(compose_layers({},[old],80,120,0)),rgba(compose_layers({},[actual],80,120,0))))
        self.assertEqual(old,before)

    def test_schema_rejects_invalid_modes(self):
        for mode in ('',None,True,{},'scale=huge'):
            with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'尺寸方式'):self.layer(mode=mode)

    def test_full_geometry_tracks_any_canvas_without_overwriting_manual_values(self):
        source=self.layer(mode='cover',position_reference='face')
        before=copy.deepcopy(source);view=effective(source)
        self.assertEqual(tuple(view[k] for k in ('x','y','width','height')),(50,50,100,100))
        for w,h in ((80,120),(1920,1080),(1000,1000)):
            self.assertEqual(image_rect(source,QtGui.QImage(str(self.wide)),w,h),QtCore.QRectF(0,0,w,h))
        from app_plugins.builtin.batch_text_video.face_anchor import enabled
        self.assertFalse(enabled(source));self.assertEqual(source,before)

    def test_cover_and_stretch_fill_both_canvas_orientations_and_pool_item_sizes(self):
        for path,w,h in ((self.wide,80,120),(self.tall,120,80)):
            for mode in ('cover','stretch'):
                with self.subTest(mode=mode,path=path.name):
                    pixels=rgba(compose_layers({},[self.layer(path,mode)],w,h,0))
                    self.assertTrue(np.all(pixels[:,:,3]==255))
                    if mode=='cover':self.assertTrue(np.all(pixels[:,:,1]>250))
                    else:
                        start=pixels[h//2,0] if path==self.wide else pixels[0,w//2]
                        end=pixels[h//2,-1] if path==self.wide else pixels[-1,w//2]
                        self.assertGreater(start[0],250);self.assertGreater(end[2],250)

    def test_transparency_survives_both_full_modes(self):
        path=self.picture('alpha.png',120,40,128)
        for mode in ('cover','stretch'):
            pixels=rgba(compose_layers({},[self.layer(path,mode)],80,120,0))
            self.assertTrue(np.all(pixels[:,:,3]==128))

    def test_geometry_editor_mode_switch_restores_dimensions_and_face_reference(self):
        from app_plugins.builtin.batch_text_video.geometry_ui import GeometryEditor
        editor=GeometryEditor();self.addCleanup(editor.deleteLater)
        original=self.layer(position_reference='face');changed=[];editor.changed.connect(changed.append)
        editor.bind(original,QtCore.QSize(80,120),'percent');self.assertFalse(changed)
        editor.size_mode.setCurrentIndex(editor.size_mode.findData('cover'))
        self.assertEqual(changed[-1]['size_mode'],'cover');self.assertEqual(changed[-1]['x'],17)
        self.assertEqual(editor.properties['width'].value(),100);self.assertFalse(editor.size_pair.isEnabled())
        self.assertFalse(editor.position_pair.isEnabled());self.assertFalse(editor.snap_button.isEnabled())
        editor.bind(editor.layer,QtCore.QSize(120,80),'pixels')
        self.assertEqual(editor.properties['width'].value(),120)
        editor.size_mode.setCurrentIndex(editor.size_mode.findData('fixed'))
        self.assertTrue(editor.size_pair.isEnabled());self.assertEqual(editor.layer['width'],35)
        self.assertEqual(editor.layer['position_reference'],'face');self.assertEqual(editor.layer['y'],30)

    def test_all_sized_property_editors_share_the_mode_control_including_watermark_corners(self):
        from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
        from app_plugins.builtin.batch_text_video.watermarks import new_watermark
        from app_plugins.builtin.batch_text_video.scrolling_text import new_scrolling_component
        layers=[self.layer(),self.text('title'),self.text('body'),self.text(),new_scrolling_component(),self.text('caption'),new_watermark()]
        panel=LayerPanel(layers,str(self.root));self.addCleanup(panel.deleteLater)
        for layer in panel.values():
            panel.select_layer_id(layer['id'])
            editor=panel.editor.geometry_editor
            if editor:
                self.assertEqual([editor.size_mode.itemData(i) for i in range(editor.size_mode.count())],[v for _,v in MODES])
                editor.size_mode.setCurrentIndex(editor.size_mode.findData('stretch'))
                self.assertEqual(panel.current()['size_mode'],'stretch')
        watermark=next(v for v in panel.values() if v['kind']=='watermark')
        panel.select_layer_id(watermark['id']);editor=panel.watermark_properties
        for key in watermark['corners']:
            editor.corner.setCurrentIndex(editor.corner.findData(key))
            editor.geometry_editor.size_mode.setCurrentIndex(editor.geometry_editor.size_mode.findData('cover'))
            child=panel.current()['corners'][key]
            self.assertEqual(child['size_mode'],'cover');self.assertEqual(effective(child)['width'],100)
            self.assertFalse(editor.placement.isEnabled())

    def test_text_title_and_body_use_full_frame_layout_without_stretching_letters(self):
        from app_plugins.builtin.batch_text_video.text_components import render_text_box
        for kind in ('title','body','text'):
            source=self.text(kind);reference=effective({**source,'size_mode':'cover'});reference={**reference,'size_mode':'fixed'}
            expected,font=render_text_box(reference,320,240)
            for mode in ('cover','stretch'):
                actual,size=render_text_box({**source,'size_mode':mode},320,240)
                self.assertEqual(size,font);self.assertEqual(size,24)
                self.assertTrue(np.array_equal(rgba(actual),rgba(expected)),kind)

    def test_scroll_and_follow_caption_viewports_use_the_whole_canvas(self):
        from app_plugins.builtin.batch_text_video.scrolling_text import new_scrolling_component,box_geometry,render_scroll_assets
        from app_plugins.builtin.batch_text_video.caption_clip import clip_bounds
        layer=new_scrolling_component();layer.update(text='One two three four five six seven eight.',size_mode='cover',width=20,height=20,x=10,y=10)
        layer=normalize_layers([layer])[0]
        self.assertEqual(box_geometry(layer,320,240),(0,0,320,240))
        assets=render_scroll_assets(layer,320,240);self.assertEqual((assets['width'],assets['height']),(320,240))
        caption=self.text('caption','cover');self.assertEqual(clip_bounds(caption,320,240),QtCore.QRectF(0,0,320,240))
        words=[dict(text='Hello',start=0,end=.4),dict(text='world',start=.4,end=.8)]
        reference={**effective(caption),'size_mode':'fixed'}
        first=caption_events(words,caption,320,240,1);second=caption_events(words,reference,320,240,1)
        self.assertEqual([(v['x'],v['y']) for v in first],[(v['x'],v['y']) for v in second])

    def test_preview_bounds_and_hit_testing_respect_full_mode_without_editing_hidden_geometry(self):
        from app_plugins.builtin.batch_text_video.preview import Preview
        preview=Preview();self.addCleanup(preview.deleteLater);preview.resize(640,480)
        layer=self.layer(mode='cover');preview.set_layers([layer],layer['id'])
        self.assertEqual(preview.layer_rect(layer),preview.canvas_rect());self.assertFalse(preview.handle_rects(layer))
        target,_=preview.hit_test(preview.canvas_rect().center());self.assertEqual(target['id'],layer['id'])
        self.assertEqual(preview.layers[0]['width'],35)

    def test_modes_persist_copy_and_style_rotation_keeps_sizing_shared(self):
        from app_plugins.builtin.batch_text_video.caption_rotation import snapshot
        state=fresh_state();layer=self.layer(mode='cover');state['settings']['layers']=[layer]
        store=Store(self.root/'private');store.save(state,0);loaded=store.load()['settings']['layers'][0]
        self.assertEqual(loaded['size_mode'],'cover');self.assertEqual(loaded['width'],35)
        self.assertNotIn('size_mode',snapshot(self.text('caption','stretch')))

    def test_mode_changes_have_normal_undo_and_redo(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        state=fresh_state();layer=self.text();state['settings']['layers']=[layer]
        store=Store(self.root/'undo');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            dialog=BatchTextVideoDialog(store=store);self.addCleanup(dialog.deleteLater);self.addCleanup(dialog.close)
            dialog.layer_panel.select_layer_id(layer['id']);dialog.reset_editor_history()
            editor=dialog.layer_panel.editor.geometry_editor
            editor.size_mode.setCurrentIndex(editor.size_mode.findData('cover'));dialog.flush_editor_history()
            self.assertEqual(dialog.layer_panel.current()['size_mode'],'cover')
            dialog.undo_edit();self.assertEqual(dialog.layer_panel.current()['size_mode'],'fixed')
            dialog.redo_edit();self.assertEqual(dialog.layer_panel.current()['size_mode'],'cover')
            self.assertEqual(dialog.layer_panel.current()['width'],30)

    def test_caption_parameter_window_can_edit_mode_and_disables_manual_width(self):
        from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
        from app_plugins.builtin.batch_text_video.caption_parameter_window import parameter_targets
        from app_plugins.builtin.batch_text_video.caption_parameter_edit import apply_value,token_for,editable
        layer=self.text('caption','cover')
        panel=LayerPanel([layer],str(self.root));self.addCleanup(panel.deleteLater)
        panel.select_layer_id(layer['id']);owner=panel.caption_properties;owner.show_parameters()
        window=owner.parameter_window;self.addCleanup(window.close)
        self.assertIn(('layer','size_mode'),parameter_targets(owner))
        self.assertFalse(editable(window,('layer','width'))[0])
        self.assertTrue(apply_value(window,token_for(window,('layer','size_mode')),'stretch'))
        self.assertEqual(panel.current()['size_mode'],'stretch');self.assertEqual(panel.current()['width'],30)

    def test_caption_appearance_presets_preserve_mode_unless_layout_reset_is_requested(self):
        from app_plugins.builtin.batch_text_video.caption_styles import apply_style
        layer=self.text('caption','cover')
        styled=apply_style(layer,'modern:talk_highlight')
        self.assertEqual(styled['size_mode'],'cover');self.assertEqual(styled['width'],30)
        reset=apply_style(layer,'modern:talk_highlight',reset_layout=True)
        self.assertEqual(reset['size_mode'],'fixed')

    def test_watermark_full_frame_text_bypasses_corner_placement_without_stretch(self):
        from app_plugins.builtin.batch_text_video.watermarks import new_watermark,expand_watermarks
        from app_plugins.builtin.batch_text_video.text_components import render_text_box
        obj=new_watermark();child=obj['corners']['top_right']
        child.update(text='Watermark text',enabled=True,size_mode='cover',font_min=24,font_max=24)
        obj=normalize_layers([obj])[0];part=next(v for v in expand_watermarks([obj]) if v['_watermark_corner']=='top_right')
        actual,size=render_text_box(part,320,240)
        expected,other_size=render_text_box({**effective(part),'size_mode':'fixed'},320,240)
        self.assertEqual(size,other_size);self.assertTrue(np.array_equal(rgba(actual),rgba(expected)))

    @unittest.skipUnless(FFMPEG,'explicit real FFmpeg required')
    def test_native_cover_stretch_and_alpha_match_preview(self):
        for path,w,h in ((self.wide,80,120),(self.tall,120,80),(self.picture('transparent.png',120,40,128),80,120)):
            for mode in ('cover','stretch'):
                layer=self.layer(path,mode);expected=rgba(compose_layers({},[layer],w,h,0));actual=self.native_frame(layer,w,h)
                self.assertLessEqual(np.abs(expected.astype(float)-actual).mean(),1.5,(path.name,mode))
                self.assertLessEqual(np.abs(expected[:,:,3].astype(float)-actual[:,:,3]).max(),1)

    @unittest.skipUnless(FFMPEG,'explicit real FFmpeg required')
    def test_transparent_video_loop_dissolve_obeys_full_mode(self):
        source=self.root/'video.mov'
        picture=self.picture('video-source.png',120,40,128)
        result=subprocess.run([FFMPEG,'-v','error','-y','-loop','1','-i',str(picture),'-t','0.4','-r','10','-c:v','qtrle','-pix_fmt','argb',str(source)],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        for mode in ('cover','stretch'):
            layer=self.layer(source,mode,media_loop_transition='fade',media_loop_fade=.1)
            expected=rgba(compose_layers({},[self.layer(picture,mode)],80,120,0))
            actual=self.native_frame(layer,80,120,frame=5)
            self.assertLess(np.abs(expected.astype(float)-actual).mean(),2,(mode,actual[0,0].tolist()))

    @unittest.skipUnless(FFMPEG,'explicit real FFmpeg required')
    def test_alpha_trimming_and_full_modes_keep_the_same_subject_geometry(self):
        image=QtGui.QImage(200,100,QtGui.QImage.Format.Format_RGBA8888);image.fill(QtCore.Qt.GlobalColor.transparent)
        painter=QtGui.QPainter(image);painter.fillRect(QtCore.QRect(8,20,50,60),QtGui.QColor('green'));painter.end()
        path=self.root/'trimmed.png';self.assertTrue(image.save(str(path)))
        for mode in ('cover','stretch'):
            layer=self.layer(path,mode,media_crop_alpha=True)
            expected=rgba(compose_layers({},[layer],80,120,0));actual=self.native_frame(layer,80,120)
            self.assertLess(np.abs(expected.astype(float)-actual).mean(),3,mode)

    @unittest.skipUnless(FFMPEG,'explicit real FFmpeg required')
    def test_full_mode_entrance_starts_outside_canvas_and_preview_matches_render(self):
        layer=self.layer(mode='cover',visual_timing_enabled=True,visual_timing=dict(
            start=0,end=0,enter='left',exit='cut',enter_duration=.5,exit_duration=0,fade_in=0,fade_out=0))
        for frame in (0,3,7):
            expected=rgba(compose_layers({},[layer],80,120,0,editor_timing={'time':frame/10,'duration':1}))
            actual=self.native_frame(layer,80,120,frame=frame)
            if frame==0:self.assertEqual(int(actual[:,:,3].max()),0)
            self.assertLess(np.abs(expected.astype(float)-actual).mean(),4,frame)

    @unittest.skipUnless(FFMPEG,'explicit real FFmpeg required')
    def test_converted_visual_node_matches_native_full_canvas_export(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry,new_effect,prepare_bundle,EffectRuntime
        from model.node_workflow.library import WorkflowLibrary
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        layer=self.layer(mode='cover');registry=fallback_registry()
        state=fresh_state();state['settings'].update(layers=[layer],darkness=0,use_voice=False,music_enabled=False,
            ffmpeg_path=FFMPEG,aspect='custom',canvas_width=320,canvas_height=480,video_encoder='cpu',
            unvoiced_duration_mode='fixed',unvoiced_duration_seconds=.4,auto_crop_black_bars=False,
            output_dir=str(self.root/'outputs'),output_destination='directory')
        background=self.root/'background.png';image=QtGui.QImage(320,480,QtGui.QImage.Format.Format_RGB32);image.fill(QtCore.Qt.GlobalColor.black);image.save(str(background))
        job=dict(id='job',path=str(background),name='native',title='',body='',task_dir='');state['jobs']=[job]
        graph,_=component_graph(layer,state['settings'],registry)
        library=WorkflowLibrary(self.root/'workflows');saved=library.save(graph,'size test',registry)
        effect=new_effect();effect['workflow_id']=saved['workflow_id']
        outputs=[]
        for node in (False,True):
            private=copy.deepcopy(state);private['jobs'][0]['name']='node' if node else 'native'
            runtime=None
            if node:
                private['settings']['layers']=[effect]
                bundle,nodes,_=prepare_bundle(private,['job'],SimpleNamespace(available_nodes=lambda:registry,workflow_library=library))
                runtime=EffectRuntime(bundle,nodes)
            output,_=render_static_text(private['jobs'][0],private,self.root/'render',node_effects=runtime)
            decoded=subprocess.run([FFMPEG,'-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],capture_output=True,timeout=30)
            self.assertEqual(decoded.returncode,0,decoded.stderr.decode('utf8','replace'))
            outputs.append(np.frombuffer(decoded.stdout,np.uint8).reshape(480,320,3))
        self.assertLess(np.abs(outputs[0].astype(float)-outputs[1]).mean(),2)
        self.assertGreater(outputs[0][:,:,1].mean(),240)

    def native_frame(self,layer,width,height,frame=0):
        from app_plugins.builtin.batch_text_video.visual_media import add_plane
        from app_plugins.builtin.batch_text_video.engine import _run
        target=self.root/('native-'+str(len(list(self.root.glob('native-*'))))+'.png')
        args=[FFMPEG,'-y','-nostdin','-v','error','-filter_complex_threads','1','-threads','1','-f','lavfi','-i',f'color=c=black@0:s={width}x{height}:r=10:d=1,format=rgba']
        filters=[];add_plane(args,filters,layer,1,'0:v','out',width,height,1,Fraction(10),ffmpeg=FFMPEG)
        label='out'
        if frame:filters.append(f'[out]select=eq(n\\,{frame})[sample]');label='sample'
        args+=['-filter_complex',';'.join(filters),'-map','['+label+']','-frames:v','1','-threads','1',str(target)]
        _run(args,threading.Event(),timeout=30)
        image=QtGui.QImage(str(target));self.assertFalse(image.isNull());return rgba(image)
