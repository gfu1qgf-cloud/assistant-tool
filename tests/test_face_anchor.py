"""Face-centred geometry, first-frame cache, UI semantics and real composition."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import face_anchor as face, engine
from app_plugins.builtin.batch_text_video.layers import normalize_layers, compose_layers
from app_plugins.builtin.batch_text_video.visual_media import add_plane
from app_plugins.builtin.batch_text_video.geometry_ui import GeometryEditor
from app_plugins.builtin.batch_text_video.matting_live import Inputs

FF=os.environ.get('NODE_FFMPEG','')
PREFIX='app_plugins.builtin.batch_text_video.'
W,H,FPS=240,320,20
POINT=dict(found=1,ax=.25,ay=.2,aspect=2/3)


def rgba(image):
    image=image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()),np.uint8).reshape(
        image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4).copy()


def mark(data):
    y,x=np.where((data[:,:,0]>180)&(data[:,:,2]>180)&(data[:,:,1]<80)&(data[:,:,3]>20))
    return None if not len(x) else ((x.min()+x.max()+1)/2,(y.min()+y.max()+1)/2)


class FaceAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.path=self.root/'person.png'
        self.pixels=np.full((240,160,4),[40,80,100,255],np.uint8)
        self.pixels[43:53,35:45]=[255,0,255,255]
        self.image=QtGui.QImage(self.pixels.tobytes(),160,240,640,QtGui.QImage.Format.Format_RGBA8888).copy()
        self.image.save(str(self.path))

    def layer(self,**values):
        data=dict(id='person',kind='image',source='fixed',path=str(self.path),x=50,y=50,width=100,height=100,
                  matting_enabled=False,position_reference='face');data.update(values)
        return normalize_layers([data])[0]

    def render(self,layer):
        args=[FF,'-nostdin','-v','error','-filter_complex_threads','1','-f','lavfi','-i',
              f'color=black@0:s={W}x{H}:r={FPS}:d=2,format=rgba']
        graph=[];add_plane(args,graph,layer,1,'0:v','result',W,H,2,FPS,ffmpeg=FF)
        args+=['-filter_complex',';'.join(graph),'-map','[result]','-t','2','-an','-f','rawvideo','-pix_fmt','rgba','pipe:1']
        with Inputs(args) as inputs:result=subprocess.run(inputs.args,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        return np.frombuffer(result.stdout,np.uint8).reshape(-1,H,W,4)

    def test_face_unit_is_per_visual_layer_and_xy_are_offsets_sizes_stay_percent(self):
        editor=GeometryEditor();self.addCleanup(editor.deleteLater)
        layer=self.layer(position_reference='canvas',x=20,y=30)
        editor.bind(layer,QtCore.QSize(W,H),'pixels');changes=[];units=[]
        editor.changed.connect(changes.append);editor.unitChanged.connect(units.append)
        editor.units.setCurrentIndex(editor.units.findData('face'))
        self.assertEqual(changes[-1]['position_reference'],'face');self.assertEqual((editor.layer['x'],editor.layer['y']),(50,50))
        self.assertEqual((editor.properties['x'].value(),editor.properties['y'].value()),(0,0))
        self.assertEqual(editor.properties['width'].suffix(),' %');self.assertFalse(editor.anchor_pair.isEnabled())
        self.assertEqual(units,[])
        editor.properties['x'].setValue(12);editor.properties['y'].setValue(-18)
        self.assertEqual((editor.layer['x'],editor.layer['y']),(62,32))
        editor.bind(editor.layer,QtCore.QSize(1080,1920),'pixels')
        self.assertEqual(editor.units.currentData(),'face')
        editor.snap_to_edges('center','center');self.assertEqual((editor.layer['x'],editor.layer['y']),(50,50))
        editor.units.setCurrentIndex(editor.units.findData('pixels'))
        self.assertEqual(editor.layer['position_reference'],'canvas');self.assertEqual(units[-1],'pixels')
        from app_plugins.builtin.batch_text_video.components import new_text_component
        editor.bind(new_text_component(),QtCore.QSize(W,H),'percent');self.assertEqual(editor.units.findData('face'),-1)
        imported=self.layer(position_anchor_x='left',position_anchor_y='top')
        editor.bind(imported,QtCore.QSize(W,H),'percent')
        editor.properties['width'].setValue(150)
        self.assertEqual((editor.layer['x'],editor.layer['y']),(50,50))


    def test_editor_face_mode_saves_undoes_and_publishes_effective_handles(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts();store=Store(self.root/'editor');state=fresh_state()
        state['settings'].update(layers=[self.layer(position_reference='canvas',x=20,y=30)],
            aspect='custom',canvas_width=W,canvas_height=H,ffmpeg_path=FF,music_enabled=False)
        store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(face,'locate',return_value=dict(POINT)):
            dialog=BatchTextVideoDialog(store=store)
            try:
                dialog.layer_panel.select_layer_id('person')
                editor=dialog.layer_panel.editor.geometry_editor
                editor.units.setCurrentIndex(editor.units.findData('face'))
                self.assertEqual(dialog.layer_panel.current()['position_reference'],'face')
                self.assertEqual(dialog.layer_panel.units.currentData(),'percent')
                self.assertTrue(dialog.persist())
                self.assertEqual(store.load()['settings']['layers'][0]['position_reference'],'face')
                self.assertTrue(dialog.undo_edit());self.assertEqual(dialog.layer_panel.current()['position_reference'],'canvas')
                self.assertTrue(dialog.redo_edit());self.assertEqual(dialog.layer_panel.current()['position_reference'],'face')
                editor=dialog.layer_panel.editor.geometry_editor
                editor.properties['y'].setValue(-15);self.assertTrue(dialog.persist())
                self.assertEqual(dialog.layer_panel.current()['y'],35)
                deadline=time.monotonic()+15
                while dialog.is_busy() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
                self.assertFalse(dialog.is_busy());self.assertIn('person',dialog.preview.face_layouts)
                self.assertIn('已定位',editor.face_status.text())
            finally:
                dialog.edit_timer.stop();dialog.close()
                deadline=time.monotonic()+15
                while dialog.is_busy() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
                self.assertFalse(dialog.is_busy());dialog.deleteLater();self.app.processEvents()

    def test_editor_face_mode_saves_undoes_and_publishes_effective_handles(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts();store=Store(self.root/'editor');state=fresh_state()
        state['settings'].update(layers=[self.layer(position_reference='canvas',x=20,y=30)],
            aspect='custom',canvas_width=W,canvas_height=H,ffmpeg_path=FF,music_enabled=False)
        store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(face,'locate',return_value=dict(POINT)):
            dialog=BatchTextVideoDialog(store=store)
            try:
                dialog.layer_panel.select_layer_id('person')
                editor=dialog.layer_panel.editor.geometry_editor
                editor.units.setCurrentIndex(editor.units.findData('face'))
                self.assertEqual(dialog.layer_panel.current()['position_reference'],'face')
                self.assertEqual(dialog.layer_panel.units.currentData(),'percent')
                self.assertTrue(dialog.persist())
                self.assertEqual(store.load()['settings']['layers'][0]['position_reference'],'face')
                self.assertTrue(dialog.undo_edit());self.assertEqual(dialog.layer_panel.current()['position_reference'],'canvas')
                self.assertTrue(dialog.redo_edit());self.assertEqual(dialog.layer_panel.current()['position_reference'],'face')
                editor=dialog.layer_panel.editor.geometry_editor
                editor.properties['y'].setValue(-15);self.assertTrue(dialog.persist())
                self.assertEqual(dialog.layer_panel.current()['y'],35)
                deadline=time.monotonic()+15
                while dialog.is_busy() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
                self.assertFalse(dialog.is_busy());self.assertIn('person',dialog.preview.face_layouts)
                self.assertIn('已定位',editor.face_status.text())
            finally:
                dialog.edit_timer.stop();dialog.close()
                deadline=time.monotonic()+15
                while dialog.is_busy() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
                self.assertFalse(dialog.is_busy());dialog.deleteLater();self.app.processEvents()

    def test_disabled_never_decodes_and_first_frame_geometry_cache_invalidates_by_source(self):
        with patch.object(face,'locate',side_effect=AssertionError('disabled must not decode')):
            self.assertIsNone(face.for_layer(self.layer(position_reference='canvas')))
        from app_plugins.builtin.batch_text_video import preview_cache
        with patch.object(preview_cache,'cache_root',return_value=self.root/'cache'),patch.object(face,'detect',side_effect=lambda *args,**kwargs:dict(POINT)) as detect:
            first=face.locate(self.path,FF);self.assertEqual(detect.call_count,1)
            face._MEMORY.clear();self.assertEqual(face.locate(self.path,FF),first);self.assertEqual(detect.call_count,1)
            stat=self.path.stat();os.utime(self.path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
            self.assertNotEqual(face.locate(self.path,FF)['key'],first['key']);self.assertEqual(detect.call_count,2)
        cancel=threading.Event();cancel.set()
        with self.assertRaises(engine.Cancelled):face.locate(self.path,FF,cancel)

    def test_real_opencv_models_work_offline_and_no_face_is_explicit(self):
        blank=QtGui.QImage(200,200,QtGui.QImage.Format.Format_RGBA8888);blank.fill(QtCore.Qt.GlobalColor.transparent)
        value=face.detect(blank);self.assertEqual(value['found'],0);self.assertIn('未检测到',face.status(value))
        self.assertEqual((value['ax'],value['ay']),(.5,.5))

    def test_multiple_faces_choose_the_largest_and_explain_selection(self):
        from types import SimpleNamespace
        model=SimpleNamespace(detectMultiScale=lambda *a,**k:np.array([[10,10,20,20],[70,20,80,80]],np.int32))
        image=QtGui.QImage(200,200,QtGui.QImage.Format.Format_RGBA8888);image.fill(QtCore.Qt.GlobalColor.white)
        with patch.object(face,'_models',return_value=(model,None)):
            value=face.detect(image)
        self.assertEqual(value['found'],2);self.assertAlmostEqual(value['ax'],.55);self.assertAlmostEqual(value['ay'],.3)
        self.assertIn('最大',face.status(value))

    def test_multiple_faces_choose_the_largest_and_explain_selection(self):
        from types import SimpleNamespace
        model=SimpleNamespace(detectMultiScale=lambda *a,**k:np.array([[10,10,20,20],[70,20,80,80]],np.int32))
        image=QtGui.QImage(200,200,QtGui.QImage.Format.Format_RGBA8888);image.fill(QtCore.Qt.GlobalColor.white)
        with patch.object(face,'_models',return_value=(model,None)):
            value=face.detect(image)
        self.assertEqual(value['found'],2);self.assertAlmostEqual(value['ax'],.55);self.assertAlmostEqual(value['ay'],.3)
        self.assertIn('最大',face.status(value))

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_png_and_video_first_frame_anchor_match_preview_at_offsets_and_sizes(self):
        video=self.root/'person.mov'
        output=subprocess.run([FF,'-nostdin','-y','-v','error','-loop','1','-i',str(self.path),'-t','0.4',
            '-r','20','-c:v','qtrle',str(video)],capture_output=True,timeout=30)
        self.assertEqual(output.returncode,0,output.stderr.decode('utf8','replace'))
        with patch.object(face,'locate',return_value=dict(POINT)):
            for path in (self.path,video):
                for x,y,width,height in ((50,50,100,100),(62,32,50,80),(50,50,140,180)):
                    layer=self.layer(path=str(path),x=x,y=y,width=width,height=height)
                    frames=self.render(layer)
                    expected=mark(rgba(compose_layers({},[layer],W,H,0)))
                    actual=mark(frames[0]);self.assertIsNotNone(actual)
                    np.testing.assert_allclose(actual,(W*x/100,H*y/100),atol=2)
                    np.testing.assert_allclose(actual,expected,atol=2)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_zoom_keeps_face_fixed_and_slide_still_starts_outside(self):
        with patch.object(face,'locate',return_value=dict(POINT)):
            for motion in ('zoom_out','breathe'):
                layer=self.layer(visual_animation=motion,animation_strength=60,animation_zoom_seconds=2,animation_period=1)
                frames=self.render(layer)
                for at in (0,.2,.6,1.,1.8):
                    actual=mark(frames[round(at*FPS)]);self.assertIsNotNone(actual)
                    np.testing.assert_allclose(actual,(W/2,H/2),atol=2)
                    expected=mark(rgba(compose_layers({},[layer],W,H,0,editor_timing=dict(duration=2,time=at))))
                    np.testing.assert_allclose(actual,expected,atol=2)
            layer=self.layer(visual_timing_enabled=True,visual_timing=dict(enter='left',enter_duration=.6,fade_in=0))
            frames=self.render(layer);self.assertEqual(frames[0,:,:,3].max(),0)
            np.testing.assert_allclose(mark(frames[20]),(W/2,H/2),atol=2)

    def test_alpha_crop_remaps_original_face_point_and_no_face_falls_back(self):
        layer=self.layer(media_crop_alpha=True,media_crop_alpha_padding=0)
        crop=dict(width=160,height=240,x=20,y=24,w=100,h=160)
        with patch.object(face,'locate',return_value=dict(POINT)):
            value=face.for_layer(layer,alpha_box=crop)
        self.assertAlmostEqual(value['ax'],.2);self.assertAlmostEqual(value['ay'],.15)
        self.assertAlmostEqual(value['aspect'],100/160)
        with patch.object(face,'locate',return_value=dict(POINT,found=0)):
            value=face.for_layer(layer,alpha_box=crop)
        self.assertEqual((value['ax'],value['ay']),(.5,.5));self.assertIn('未检测到',face.status(value))
        with patch.object(face,'locate',return_value=dict(POINT)):
            value=face.for_layer(layer,alpha_box=dict(crop,x=100))
        self.assertEqual(value['found'],0);self.assertIn('保留范围',face.status(value))

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_real_transparent_video_crop_keeps_the_original_face_at_target(self):
        frames=np.zeros((6,240,160,4),np.uint8)
        frames[:,24:184,20:120]=self.pixels[24:184,20:120]
        video=self.root/'trimmed-alpha.mov'
        output=subprocess.run([FF,'-nostdin','-v','error','-y','-f','rawvideo','-pix_fmt','rgba','-s','160x240','-r','20',
            '-i','pipe:0','-c:v','qtrle',str(video)],input=frames.tobytes(),capture_output=True,timeout=30)
        self.assertEqual(output.returncode,0,output.stderr.decode('utf8','replace'))
        with patch.object(face,'locate',return_value=dict(POINT)):
            layer=self.layer(path=str(video),media_crop_alpha=True,media_crop_alpha_padding=0)
            actual=self.render(layer)
            np.testing.assert_allclose(mark(actual[0]),(W/2,H/2),atol=2)
            expected=mark(rgba(compose_layers({},[layer],W,H,0)))
            np.testing.assert_allclose(mark(actual[0]),expected,atol=2)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_real_transparent_video_crop_keeps_the_original_face_at_target(self):
        frames=np.zeros((6,240,160,4),np.uint8)
        frames[:,24:184,20:120]=self.pixels[24:184,20:120]
        video=self.root/'trimmed-alpha.mov'
        output=subprocess.run([FF,'-nostdin','-v','error','-y','-f','rawvideo','-pix_fmt','rgba','-s','160x240','-r','20',
            '-i','pipe:0','-c:v','qtrle',str(video)],input=frames.tobytes(),capture_output=True,timeout=30)
        self.assertEqual(output.returncode,0,output.stderr.decode('utf8','replace'))
        with patch.object(face,'locate',return_value=dict(POINT)):
            layer=self.layer(path=str(video),media_crop_alpha=True,media_crop_alpha_padding=0)
            actual=self.render(layer)
            np.testing.assert_allclose(mark(actual[0]),(W/2,H/2),atol=2)
            expected=mark(rgba(compose_layers({},[layer],W,H,0)))
            np.testing.assert_allclose(mark(actual[0]),expected,atol=2)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_video_detects_only_frame_zero_and_does_not_track_later_motion(self):
        from app_plugins.builtin.batch_text_video import preview_cache
        video=self.root/'first-only.mov'
        frames=np.repeat(self.pixels[None,:,:,:],6,axis=0)
        frames[1:]=[40,80,100,255];frames[1:,115:125,75:85]=[255,0,255,255]
        output=subprocess.run([FF,'-nostdin','-v','error','-y','-f','rawvideo','-pix_fmt','rgba','-s','160x240','-r','20',
            '-i','pipe:0','-c:v','qtrle',str(video)],input=frames.tobytes(),capture_output=True,timeout=30)
        self.assertEqual(output.returncode,0,output.stderr.decode('utf8','replace'))
        with patch.object(preview_cache,'cache_root',return_value=self.root/'cache'),patch.object(face,'detect',side_effect=lambda *a:dict(POINT)) as detect:
            layer=self.layer(path=str(video));actual=self.render(layer)
            self.assertEqual(detect.call_count,1)
            self.render(layer);self.assertEqual(detect.call_count,1)
        np.testing.assert_allclose(mark(actual[0]),(W/2,H/2),atol=2)
        self.assertGreater(mark(actual[1])[0],W/2+30)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_pool_members_keep_their_own_face_point_without_advancing_cursor(self):
        from app_plugins.builtin.batch_text_video.components import resolve_sources
        second=self.root/'other.png';pixels=self.pixels.copy();pixels[:]=[40,80,100,255];pixels[139:149,115:125]=[255,0,255,255]
        QtGui.QImage(pixels.tobytes(),160,240,640,QtGui.QImage.Format.Format_RGBA8888).copy().save(str(second))
        layer=self.layer(source='pool',sequence_cursor=0,sequence_items=[dict(id='one',path=str(self.path)),dict(id='two',path=str(second))])
        original=copy.deepcopy(layer)
        def locate(path,*a,**k):return dict(POINT) if Path(path)==self.path else dict(POINT,ax=.75,ay=.6)
        with patch.object(face,'locate',side_effect=locate):
            for cursor in (0,1):
                resolved,steps=resolve_sources([dict(layer,sequence_cursor=cursor)])
                actual=self.render(resolved[0]);np.testing.assert_allclose(mark(actual[0]),(W/2,H/2),atol=2)
        self.assertEqual(layer,original)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_video_detects_only_frame_zero_and_does_not_track_later_motion(self):
        from app_plugins.builtin.batch_text_video import preview_cache
        video=self.root/'first-only.mov'
        frames=np.repeat(self.pixels[None,:,:,:],6,axis=0)
        frames[1:]=[40,80,100,255];frames[1:,115:125,75:85]=[255,0,255,255]
        output=subprocess.run([FF,'-nostdin','-v','error','-y','-f','rawvideo','-pix_fmt','rgba','-s','160x240','-r','20',
            '-i','pipe:0','-c:v','qtrle',str(video)],input=frames.tobytes(),capture_output=True,timeout=30)
        self.assertEqual(output.returncode,0,output.stderr.decode('utf8','replace'))
        with patch.object(preview_cache,'cache_root',return_value=self.root/'cache'),patch.object(face,'detect',side_effect=lambda *a:dict(POINT)) as detect:
            layer=self.layer(path=str(video));actual=self.render(layer)
            self.assertEqual(detect.call_count,1)
            self.render(layer);self.assertEqual(detect.call_count,1)
        np.testing.assert_allclose(mark(actual[0]),(W/2,H/2),atol=2)
        self.assertGreater(mark(actual[1])[0],W/2+30)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_pool_members_keep_their_own_face_point_without_advancing_cursor(self):
        from app_plugins.builtin.batch_text_video.components import resolve_sources
        second=self.root/'other.png';pixels=self.pixels.copy();pixels[:]=[40,80,100,255];pixels[139:149,115:125]=[255,0,255,255]
        QtGui.QImage(pixels.tobytes(),160,240,640,QtGui.QImage.Format.Format_RGBA8888).copy().save(str(second))
        layer=self.layer(source='pool',sequence_cursor=0,sequence_items=[dict(id='one',path=str(self.path)),dict(id='two',path=str(second))])
        original=copy.deepcopy(layer)
        def locate(path,*a,**k):return dict(POINT) if Path(path)==self.path else dict(POINT,ax=.75,ay=.6)
        with patch.object(face,'locate',side_effect=locate):
            for cursor in (0,1):
                resolved,steps=resolve_sources([dict(layer,sequence_cursor=cursor)])
                actual=self.render(resolved[0]);np.testing.assert_allclose(mark(actual[0]),(W/2,H/2),atol=2)
        self.assertEqual(layer,original)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_node_render_and_native_render_have_matching_face_anchor(self):
        for threads in (1,4):
            with self.subTest(filter_threads=threads):
                outputs=self._render_face_pair(filter_threads=threads)
                # Keep exact pixel equality: only eliminate lossy rate control
                # and host-dependent scheduling from this geometry fixture.
                self.assertEqual(outputs[0],outputs[1])

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_face_anchor_pixel_comparison_detects_a_real_position_change(self):
        outputs=self._render_face_pair(node_x_offset=2)
        self.assertNotEqual(outputs[0],outputs[1])

    def _render_face_pair(self, *, filter_threads=1, node_x_offset=0):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry,new_effect,prepare_bundle,EffectRuntime
        from app_plugins.builtin.batch_text_video.store import DEFAULTS,fresh_state
        from model.node_workflow.library import WorkflowLibrary
        layer=self.layer(x=62,y=35,visual_animation='breathe',animation_strength=25,animation_period=1)
        registry=fallback_registry();graph,_=component_graph(layer,DEFAULTS,registry)
        operation=next(item for item in graph['nodes'] if item['kind']=='batch_text_video.visual_layer')
        operation['params']['layer']['x']+=node_x_offset
        library=WorkflowLibrary(self.root/'workflows');saved=library.save(graph,'face',registry)
        effect=new_effect();effect['workflow_id']=saved['workflow_id']
        services=SimpleNamespace(available_nodes=lambda:registry,workflow_library=library)
        outputs=[]
        from render_test_fixtures import lossless_composition
        with patch.object(face,'locate',return_value=dict(POINT)), lossless_composition(filter_threads=filter_threads):
            for mode in ('native','node'):
                state=fresh_state();state['settings'].update(aspect='custom',canvas_width=W,canvas_height=H,
                    unvoiced_duration_seconds=1,darkness=0,music_enabled=False,use_voice=False,ffmpeg_path=FF,
                    video_encoder='cpu',output_dir=str(self.root/mode),layers=[effect if mode=='node' else layer])
                job=dict(id=mode,path='',name=mode,title='',body='');state['jobs']=[job]
                runtime=None
                if mode=='node':
                    bundle,registry,_=prepare_bundle(state,[mode],services);runtime=EffectRuntime(bundle,registry)
                output,_=engine.render_static_text(job,state,self.root/'private',node_effects=runtime)
                result=subprocess.run([FF,'-v','error','-threads','1','-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','pipe:1'],capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));outputs.append(result.stdout)
        return outputs

    def test_preview_handles_use_worker_geometry_without_reading_files(self):
        from app_plugins.builtin.batch_text_video.preview import Preview
        preview=Preview();self.addCleanup(preview.deleteLater);preview.resize(W,H);preview.canvas_size=QtCore.QSize(W,H)
        layer=self.layer();preview.set_layers([layer],'person');preview.set_face_layouts({'person':dict(POINT)})
        with patch('pathlib.Path.stat',side_effect=AssertionError('paint geometry cannot stat or decode')):
            rect=preview.layer_rect(layer)
        expected=face.center_for(layer,W,H,POINT);np.testing.assert_allclose((rect.center().x(),rect.center().y()),expected,atol=.01)
        values=[];preview.geometryEdited.connect(lambda key,value:values.append(value))
        preview.align_selected('right','center');self.assertEqual((values[-1]['x'],values[-1]['y']),(100,50))

    def test_store_node_conversion_and_packaging_keep_face_mode(self):
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state,DEFAULTS
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        state=fresh_state();layer=self.layer(x=62,y=32);state['settings']['layers']=[layer]
        store=Store(self.root/'project');store.save(state,0);loaded=store.load()
        self.assertEqual(loaded['settings']['layers'][0]['position_reference'],'face');self.assertEqual(loaded['settings']['geometry_units'],'percent')
        graph,_=component_graph(layer,DEFAULTS,fallback_registry())
        operation=next(v for v in graph['nodes'] if v['kind']=='batch_text_video.visual_layer')
        self.assertEqual(operation['params']['layer']['position_reference'],'face')
        spec=Path(__file__).resolve().parents[1]/'packaging'/'AssistantTool.spec'
        source=spec.read_text(encoding='utf8');self.assertIn('haarcascade_frontalface_default.xml',source);self.assertIn('haarcascade_profileface.xml',source)

    def test_packaging_collects_the_actual_two_installed_classifier_files(self):
        spec=Path(__file__).resolve().parents[1]/'packaging'/'AssistantTool.spec'
        source=spec.read_text(encoding='utf8')
        start=source.index('from importlib.util import find_spec',source.index('# Offline first-frame face'))
        end=source.index('app_icon =',start)
        namespace=dict(Path=Path,datas=[],hiddenimports=[])
        exec(compile(source[start:end],str(spec),'exec'),namespace)
        self.assertEqual(namespace['hiddenimports'],['cv2.data'])
        files=namespace['datas'];self.assertEqual(len(files),2)
        self.assertEqual({Path(src).name for src,dst in files},{'haarcascade_frontalface_default.xml','haarcascade_profileface.xml'})
        self.assertTrue(all(Path(src).is_file() and dst=='cv2/data' for src,dst in files))

    def test_packaging_collects_the_actual_two_installed_classifier_files(self):
        spec=Path(__file__).resolve().parents[1]/'packaging'/'AssistantTool.spec'
        source=spec.read_text(encoding='utf8')
        start=source.index('from importlib.util import find_spec',source.index('# Offline first-frame face'))
        end=source.index('app_icon =',start)
        namespace=dict(Path=Path,datas=[],hiddenimports=[])
        exec(compile(source[start:end],str(spec),'exec'),namespace)
        self.assertEqual(namespace['hiddenimports'],['cv2.data'])
        files=namespace['datas'];self.assertEqual(len(files),2)
        self.assertEqual({Path(src).name for src,dst in files},{'haarcascade_frontalface_default.xml','haarcascade_profileface.xml'})
        self.assertTrue(all(Path(src).is_file() and dst=='cv2/data' for src,dst in files))

    @unittest.skipUnless(os.environ.get('LZX_FACE_FIXTURE'),'explicit read-only face example required')
    def test_read_only_real_face_example(self):
        path=Path(os.environ['LZX_FACE_FIXTURE']);before=(path.stat().st_size,path.stat().st_mtime_ns)
        value=face.locate(path,FF);self.assertGreater(value['found'],0)
        self.assertGreater(value['ay'],0);self.assertLess(value['ay'],1)
        self.assertEqual((path.stat().st_size,path.stat().st_mtime_ns),before)
        report=os.environ.get('LZX_FACE_QA')
        if report:
            Path(report).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
            layer=self.layer(path=str(path))
            before_image=compose_layers({},[dict(layer,position_reference='canvas')],320,480,0)
            after_image=compose_layers({},[layer],320,480,0)
            card=QtGui.QImage(640,510,QtGui.QImage.Format.Format_RGB32);card.fill(QtGui.QColor('#30343a'))
            painter=QtGui.QPainter(card)
            try:
                painter.drawImage(0,30,before_image);painter.drawImage(320,30,after_image)
                painter.setPen(QtGui.QColor('white'));painter.drawText(12,22,'素材中心');painter.drawText(332,22,'人脸中心 · 偏移 0 / 0')
                painter.setPen(QtGui.QColor('#00d9ff'));painter.drawLine(320+152,270,320+168,270);painter.drawLine(480,262,480,278)
            finally:painter.end()
            card.save(str(Path(report).with_suffix('.png')))
            editor=GeometryEditor();editor.bind(layer,QtCore.QSize(1080,1920),'percent');editor.resize(500,250);editor.show();self.app.processEvents()
            editor.grab().save(str(Path(report).with_name('face-unit-ui.png')));editor.close();editor.deleteLater()
            layer=self.layer(path=str(path))
            before_image=compose_layers({},[dict(layer,position_reference='canvas')],320,480,0)
            after_image=compose_layers({},[layer],320,480,0)
            card=QtGui.QImage(640,510,QtGui.QImage.Format.Format_RGB32);card.fill(QtGui.QColor('#30343a'))
            painter=QtGui.QPainter(card)
            try:
                painter.drawImage(0,30,before_image);painter.drawImage(320,30,after_image)
                painter.setPen(QtGui.QColor('white'));painter.drawText(12,22,'素材中心');painter.drawText(332,22,'人脸中心 · 偏移 0 / 0')
                painter.setPen(QtGui.QColor('#00d9ff'));painter.drawLine(320+152,270,320+168,270);painter.drawLine(480,262,480,278)
            finally:painter.end()
            card.save(str(Path(report).with_suffix('.png')))
            editor=GeometryEditor();editor.bind(layer,QtCore.QSize(1080,1920),'percent');editor.resize(500,250);editor.show();self.app.processEvents()
            editor.grab().save(str(Path(report).with_name('face-unit-ui.png')));editor.close();editor.deleteLater()
