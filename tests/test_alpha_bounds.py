"""Fixed full-clip alpha bounds, real codecs, preview and native composition."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import alpha_bounds as crop, engine
from app_plugins.builtin.batch_text_video.layers import normalize_layers, compose_layers
from app_plugins.builtin.batch_text_video.visual_media import add_plane, VisualProperties
from app_plugins.builtin.batch_text_video.matting_live import Inputs

FF = os.environ.get('NODE_FFMPEG', '')
SAMPLES = Path(r'D:\统一下发粒子库（官网，网传个人都能用的）\00 重新转码\12 鸽子、鸟、梅花鹿')


class AlphaBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)

    def ff(self, args, data=None):
        result = subprocess.run([FF, '-nostdin', '-v', 'error', '-filter_complex_threads', '1', *args], input=data,
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8','replace')); return result.stdout

    def fixture(self, mode='alpha'):
        pixels = np.zeros((10, 48, 64, 4), np.uint8)
        # Native hidden RGB is nonzero: only alpha may define the bounds.
        pixels[:,:,:,:3] = [220,100,30]
        pixels[:,12:29,11:30,3] = 255
        pixels[9,4:12,37:51,3] = 255  # final-frame wing tip must survive
        if mode == 'empty': pixels[:,:,:,3] = 0
        if mode == 'opaque': pixels[:,:,:,3] = 255
        pixels[0,40,58,3] = 1 if mode == 'alpha' else pixels[0,40,58,3]
        path = self.root/(mode+" '鸟.mov")
        self.ff(['-y','-f','rawvideo','-pix_fmt','rgba','-s','64x48','-r','10','-i','pipe:0','-c:v','qtrle',str(path)], pixels.tobytes())
        return path, pixels

    def layer(self, path, **values):
        return normalize_layers([dict(id='bird',kind='image',path=str(path),width=100,height=100,
            matting_enabled=False,media_crop_alpha=True,media_crop_alpha_padding=0,**values)])[0]

    def test_full_clip_union_threshold_padding_and_small_cache(self):
        path,_=self.fixture()
        value=crop.bounds(path,ffmpeg=FF,padding=0)
        self.assertEqual([value[k] for k in ('x','y','w','h','frames')],[11,4,48,37,10])
        strong=crop.bounds(path,ffmpeg=FF,padding=0,threshold=2)
        self.assertEqual([strong[k] for k in ('x','y','w','h')],[11,4,40,25])
        padded=crop.bounds(path,ffmpeg=FF,padding=2,threshold=2)
        self.assertEqual([padded[k] for k in ('x','y','w','h')],[9,2,44,29])
        with patch.object(crop,'_scan',side_effect=AssertionError('must reuse JSON')):
            crop._MEMORY.clear(); self.assertEqual(crop.bounds(path,ffmpeg=FF,padding=0),value)
        self.assertLess(len(__import__('json').dumps(value)),500)

    def test_opaque_empty_disabled_cancel_and_changed_source(self):
        for mode in ('opaque','empty'):
            path,_=self.fixture(mode); value=crop.bounds(path,ffmpeg=FF,padding=0)
            self.assertEqual([value[k] for k in ('x','y','w','h')],[0,0,64,48])
        with patch.object(crop,'bounds',side_effect=AssertionError('disabled must not decode')):
            self.assertIsNone(crop.for_layer(dict(media_crop_alpha=False,path=str(path))))
        cancel=threading.Event(); cancel.set()
        with self.assertRaises(engine.Cancelled): crop.bounds(path,ffmpeg=FF,cancel=cancel)
        path,_=self.fixture(); first=crop.bounds(path,ffmpeg=FF)
        stat=path.stat(); os.utime(path, ns=(stat.st_atime_ns,stat.st_mtime_ns+1_000_000))
        self.assertNotEqual(crop.bounds(path,ffmpeg=FF)['key'],first['key'])

    def test_real_export_scales_fixed_union_preserves_alpha_and_no_per_frame_zoom(self):
        path,pixels=self.fixture(); layer=self.layer(path,media_crop_alpha_threshold=2)
        args=[FF,'-v','error','-filter_complex_threads','1','-f','lavfi','-i','color=black:s=80x50:r=10:d=1']
        graph=[]; add_plane(args,graph,layer,1,'0:v','bird',80,50,1,10,ffmpeg=FF)
        args += ['-filter_complex',';'.join(graph),'-map','[bird]','-t','1','-f','rawvideo','-pix_fmt','rgba','pipe:1']
        with Inputs(args) as inputs:
            result=subprocess.run(inputs.args,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,50,80,4)
        # Stationary body has the same placement/size even when wing appears.
        masks=(frames[:,:,:,:3].max(axis=3)>40)
        self.assertTrue(np.array_equal(masks[1],masks[8]))
        self.assertGreater(masks[-1].sum(),masks[1].sum())
        ys,xs=np.where(masks[-1]); self.assertLessEqual(xs.min(),1); self.assertGreaterEqual(xs.max(),78)
        self.assertTrue(np.all(frames[:,0,0,:3]==0))

    def test_ui_save_and_preview_share_union_not_first_frame(self):
        path,pixels=self.fixture(); layer=self.layer(path,media_crop_alpha_threshold=2)
        panel=VisualProperties(); panel.set_layer(layer)
        try:
            self.assertTrue(panel.controls['media_crop_alpha'].isChecked())
            changes=[]; panel.changed.connect(changes.append)
            panel.controls['media_crop_alpha_padding'].setValue(3)
            self.assertEqual(changes[-1]['media_crop_alpha_padding'],3)
            panel.controls['media_crop_alpha'].setChecked(False)
            self.assertFalse(panel.controls['media_crop_alpha_padding'].isEnabled())
        finally: panel.deleteLater()
        rgba=QtGui.QImage(pixels[0].tobytes(),64,48,256,QtGui.QImage.Format.Format_RGBA8888).copy()
        box=crop.for_layer(layer,ffmpeg=FF)
        image=crop.crop_image(rgba,box)
        self.assertEqual((image.width(),image.height()),(40,25))
        self.assertEqual(image.pixelColor(0,0).alpha(),0)  # union includes absent wing region
        calls=[]
        class PosterCache:
            def get(inner, path, size): calls.append((size.width(),size.height()));return rgba
        composed=compose_layers({},[layer],80,50,0,PosterCache())
        self.assertEqual(calls,[(64,48)])
        self.assertEqual((composed.width(),composed.height()),(80,50))

        from app_plugins.builtin.batch_text_video.store import Store, fresh_state
        store=Store(self.root/'project'); state=fresh_state();state['settings']['layers']=[layer]
        store.save(state,0);self.assertTrue(store.load()['settings']['layers'][0]['media_crop_alpha'])

    def test_dissolve_and_repeat_region_use_the_same_fixed_union(self):
        from app_plugins.builtin.batch_text_video.visual_loop_region import stamp
        path,pixels=self.fixture()
        rule=dict(path=str(path),start=.2,end=.8,duration=1.,**stamp(path))
        for transition in ('none','fade'):
            layer=self.layer(path,media_crop_alpha_threshold=2,media_loop_regions=[rule],
                             media_loop_transition=transition,media_loop_fade=.2)
            args=[FF,'-v','error','-filter_complex_threads','1','-f','lavfi','-i','color=black:s=80x50:r=10:d=2']
            graph=[];add_plane(args,graph,layer,1,'0:v','bird',80,50,2,10,ffmpeg=FF)
            args+=['-filter_complex',';'.join(graph),'-map','[bird]','-t','2','-f','rawvideo','-pix_fmt','rgba','pipe:1']
            with Inputs(args) as inputs:result=subprocess.run(inputs.args,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,50,80,4)
            self.assertEqual(len(frames),20)
            a=frames[0,:,:,:3].max(axis=2)>40;b=frames[15,:,:,:3].max(axis=2)>40
            self.assertTrue(np.array_equal(a,b), (transition,int(a.sum()),int(b.sum())))
            self.assertLessEqual(np.abs(frames[0].astype(int)-frames[15]).max(),2, (transition, frames[0,25,20].tolist(),frames[15,25,20].tolist()))

    def test_node_conversion_keeps_crop_and_matches_native_export(self):
        from render_test_fixtures import lossless_composition
        with lossless_composition():
            self._check_node_conversion_keeps_crop_and_matches_native_export()

    def _check_node_conversion_keeps_crop_and_matches_native_export(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry,new_effect,prepare_bundle,EffectRuntime
        from app_plugins.builtin.batch_text_video.store import DEFAULTS,fresh_state
        from model.node_workflow.library import WorkflowLibrary
        path,_=self.fixture();layer=self.layer(path,media_crop_alpha_threshold=2)
        registry=fallback_registry();graph,_=component_graph(layer,DEFAULTS,registry)
        library=WorkflowLibrary(self.root/'workflows');saved=library.save(graph,'crop',registry)
        effect=new_effect();effect['workflow_id']=saved['workflow_id']
        services=SimpleNamespace(available_nodes=lambda:registry,workflow_library=library)
        outputs=[]
        for mode in ('native','node'):
            state=fresh_state();state['settings'].update(aspect='custom',canvas_width=240,canvas_height=180,
                unvoiced_duration_seconds=1.,darkness=0,music_enabled=False,use_voice=False,ffmpeg_path=FF,
                video_encoder='cpu',output_dir=str(self.root/mode),layers=[effect if mode=='node' else layer])
            job=dict(id=mode,path='',name=mode,title='',body='');state['jobs']=[job]
            runtime=None
            if mode=='node':
                bundle,registry,_=prepare_bundle(state,[mode],services);runtime=EffectRuntime(bundle,registry)
            output,_=engine.render_static_text(job,state,self.root/'private',node_effects=runtime)
            outputs.append(self.ff(['-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','pipe:1']))
        self.assertEqual(outputs[0],outputs[1])

    @unittest.skipUnless(SAMPLES.is_dir(),'local example library unavailable')
    def test_actual_dnx_examples_native_alpha_and_full_clip_bounds(self):
        from app_plugins.builtin.batch_text_video import dnx_alpha
        rows=[]
        for path in sorted(SAMPLES.glob('*.mov')):
            native=dnx_alpha.describe(path,FF);self.assertIsNotNone(native)
            value=crop.bounds(path,ffmpeg=FF,padding=2)
            self.assertEqual((value['width'],value['height']),(2000,2000))
            self.assertGreater(value['frames'],20);self.assertLess(value['w']*value['h'],4_000_000)
            # Actual alpha decode is read-only, and boundary metadata is tiny.
            rows.append(dict(file=path.name,**value))
            if len(rows)==1 and os.environ.get('LZX_ALPHA_BOUNDS_REPORT'):
                image=dnx_alpha.poster(path,QtCore.QSize(720,720),ffmpeg=FF)
                trimmed=crop.crop_image(image,value)
                card=QtGui.QImage(960,430,QtGui.QImage.Format.Format_RGB32);card.fill(QtGui.QColor('#303238'))
                painter=QtGui.QPainter(card)
                try:
                    for x,title,frame in ((0,'原始画幅',image),(480,'固定边界裁切',trimmed)):
                        painter.setPen(QtGui.QColor('white'));painter.drawText(x+15,25,title)
                        size=frame.size().scaled(QtCore.QSize(450,380),QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                        painter.drawImage(QtCore.QRect(x+(480-size.width())//2,40+(380-size.height())//2,size.width(),size.height()),frame)
                finally:painter.end()
                card.save(str(Path(os.environ['LZX_ALPHA_BOUNDS_REPORT']).with_suffix('.png')))
        self.assertGreaterEqual(len(rows),3)
        report=os.environ.get('LZX_ALPHA_BOUNDS_REPORT')
        if report: Path(report).write_text(__import__('json').dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
