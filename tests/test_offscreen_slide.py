"""Directional slides begin beyond the canvas, independent of alpha fades."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers, compose_layers
from app_plugins.builtin.batch_text_video.visual_media import add_plane
from app_plugins.builtin.batch_text_video.visual_animation import preview_transform
from app_plugins.builtin.batch_text_video.overlay_timing import resolve_window, transform_at, add_timed_plane

FF = os.environ.get('NODE_FFMPEG', '')
W,H,FPS = 160,240,20


def rgba(image):
    image=image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()),np.uint8).reshape(
        image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4).copy()


def box(data):
    y,x=np.where(data[:,:,3]>3)
    return None if not len(x) else np.array([x.min(),y.min(),x.max()+1,y.max()+1])


class OffscreenSlideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.path=self.root/'person.png'
        pixels=np.full((120,80,4),[230,40,20,255],np.uint8)
        pixels[80:,:,:3]=[20,40,230]
        self.image=QtGui.QImage(pixels.tobytes(),80,120,320,QtGui.QImage.Format.Format_RGBA8888).copy()
        self.assertTrue(self.image.save(str(self.path)))

    def layer(self,mode='left',**values):
        layer=dict(id='person',kind='image',path=str(self.path),source='fixed',x=50,y=50,width=100,height=100,
                   matting_enabled=False,visual_timing_enabled=True,
                   visual_timing=dict(start=0,end=2,enter=mode,exit=mode,enter_duration=.6,exit_duration=.5,
                                      fade_in=0,fade_out=0,distance=35,clock='restart'))
        layer.update(values);return normalize_layers([layer])[0]

    def render(self,layer):
        from app_plugins.builtin.batch_text_video.matting_live import Inputs
        args=[FF,'-nostdin','-v','error','-filter_complex_threads','1','-f','lavfi','-i',
              f'color=c=black@0:s={W}x{H}:r={FPS}:d=2.2,format=rgba']
        graph=[];add_plane(args,graph,layer,1,'0:v','result',W,H,2.2,FPS,ffmpeg=FF)
        args+=['-filter_complex',';'.join(graph),'-map','[result]','-t','2.2','-an','-f','rawvideo','-pix_fmt','rgba','pipe:1']
        with Inputs(args) as inputs: result=subprocess.run(inputs.args,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,(layer.get('visual_animation'),result.stderr.decode('utf8','replace'),';'.join(graph)))
        return np.frombuffer(result.stdout,np.uint8).reshape(-1,H,W,4)

    def parity(self,layer,frames):
        for at in (0,.1,.3,.6,1.,1.7,1.9,2.):
            preview=rgba(compose_layers({},[layer],W,H,0,editor_timing=dict(duration=2.2,time=at)))
            actual,expected=box(frames[round(at*FPS)]),box(preview)
            self.assertEqual(actual is None,expected is None,(layer['visual_timing'],at))
            if actual is not None:self.assertLessEqual(np.abs(actual-expected).max(),3,(at,actual,expected))

    def test_numeric_all_directions_clear_edges_even_for_zero_distance_and_oversized_bounds(self):
        for distance in (0,12,35,80):
            for bounds in ((0,0,1,1),(-.7,-.2,1.6,2.),(.8,.7,.95,.95)):
                for mode in ('left','right','up','down'):
                    window=resolve_window(dict(enter=mode,exit=mode,enter_duration=.6,exit_duration=.5,distance=distance),2)
                    alpha,scale,dx,dy=transform_at(window,0,bounds=bounds,margin=(2/W,2/H))
                    self.assertEqual(alpha,1)
                    l,t,r,b=bounds
                    if mode=='left':self.assertLess(r+dx,0)
                    elif mode=='right':self.assertGreater(l+dx,1)
                    elif mode=='up':self.assertLess(b+dy,0)
                    else:self.assertGreater(t+dy,1)
                    self.assertEqual(transform_at(window,1,bounds=bounds),(1.,1.,0.,0.))

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_actual_first_frame_and_preview_all_directions_positions_and_sizes(self):
        for mode in ('left','right','up','down'):
            for x,y,width,height in ((50,50,100,100),(85,75,140,180),(10,15,30,40)):
                with self.subTest(mode=mode,geometry=(x,y,width,height)):
                    layer=self.layer(mode,x=x,y=y,width=width,height=height)
                    frames=self.render(layer)
                    self.assertIsNone(box(frames[0]),'first frame must be entirely outside, with fade disabled')
                    self.assertIsNotNone(box(frames[20]))
                    self.assertIsNone(box(frames[40]))
                    self.parity(layer,frames)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_zoom_and_float_use_the_current_unclipped_object_bounds(self):
        for motion in ('zoom_out','float'):
            for opacity in (100,40):
                layer=self.layer('up',x=80,y=85,width=140,height=180,opacity=opacity,visual_animation=motion,
                                 animation_strength=80,animation_zoom_seconds=2,animation_period=1.1)
                frames=self.render(layer);self.assertIsNone(box(frames[0]));self.parity(layer,frames)
                self.assertLessEqual(int(frames[:,:,:,3].max()),round(255*opacity/100)+1)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_transparent_video_auto_crop_is_used_before_slide_geometry(self):
        pixels=np.zeros((6,120,80,4),np.uint8);pixels[:,30:85,20:60]=[230,40,20,255]
        path=self.root/'alpha.mov'
        result=subprocess.run([FF,'-nostdin','-v','error','-y','-f','rawvideo','-pix_fmt','rgba',
            '-s','80x120','-r','20','-i','pipe:0','-c:v','qtrle',str(path)],input=pixels.tobytes(),capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        for mode in ('left','down'):
            layer=self.layer(mode,path=str(path),x=80,y=25,media_crop_alpha=True,media_crop_alpha_padding=0)
            frames=self.render(layer);self.assertIsNone(box(frames[0]));self.parity(layer,frames)

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_full_canvas_planes_also_start_outside_without_changing_base(self):
        for mode in ('left','right','up','down'):
            window=resolve_window(dict(enter=mode,exit=mode,enter_duration=.6,exit_duration=.5),2.2)
            graph=[];add_timed_plane(graph,'0:v','timed',W,H,2.2,FPS,window,center=(W*.85,H*.75))
            args=[FF,'-nostdin','-v','error','-filter_complex_threads','1','-f','lavfi','-i',
                  f'color=red:s={W}x{H}:r={FPS}:d=2.2,format=rgba','-filter_complex',';'.join(graph),
                  '-map','[timed]','-f','rawvideo','-pix_fmt','rgba','pipe:1']
            result=subprocess.run(args,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,H,W,4)
            self.assertIsNone(box(frames[0]));self.assertIsNotNone(box(frames[20]))

    def test_fade_and_scale_parameters_remain_independent_and_source_is_unchanged(self):
        layer=self.layer();before=copy.deepcopy(layer)
        layer['visual_timing'].update(enter='small',distance=35)
        scale,dx,dy,alpha=preview_transform(layer,W,H,dict(duration=2.2,time=0),object_size=(80,120))
        self.assertAlmostEqual(scale,.65);self.assertEqual((dx,dy,alpha),(0,0,1))
        layer=before;layer['visual_timing']['fade_in']=.2
        self.assertEqual(preview_transform(layer,W,H,dict(duration=2.2,time=0))[-1],0)
        self.assertEqual(preview_transform(layer,W,H,dict(duration=2.2,time=.3))[-1],1)
