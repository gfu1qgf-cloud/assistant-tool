"""Real multi-frame equivalence and one-time colour-key work, including motion."""
import copy
from fractions import Fraction
import types
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

import numpy as np
from PIL import Image
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video import visual_media

FFMPEG=os.environ.get('NODE_FFMPEG','')
REFERENCE_SOURCE = r'''
def add_plane(args, filters, layer, input_index, background, label, width, height, duration, fps, *, timing=None, ffmpeg=''):
    """Alpha-correct normal/add/screen, with transparent clipping at canvas edges."""
    normalize_visual(layer)
    # An explicit timing node owns the window; never apply a native one twice.
    if timing is None:
        timing = show_window(layer, duration)
    local_duration = timing.source_duration(duration) if timing else duration
    path = layer["path"]
    if not Path(path).is_file() or Path(path).suffix.casefold() not in VISUAL_SUFFIXES:
        raise ValueError("视觉元素文件不存在或格式不支持：" + str(path))
    box_w, box_h = max(2, round(width*layer["width"]/100)), max(2, round(height*layer["height"]/100))
    if box_w * box_h > 64_000_000 or max(box_w, box_h) > 16384:
        raise ValueError('视觉图层放大后像素过大，请减小宽度／高度；未启动可能耗尽内存的缩放。')
    from .matting_live import dynamic, StreamInput
    from .visual_loop import enabled as loop_fade_enabled, LoopInput
    candidate = loop_fade_enabled(layer)
    executable = ''
    if dynamic(layer) or candidate:
        from .engine import resolve_tools
        executable = ffmpeg or (args[0] if args and not args[0].startswith('-') else resolve_tools()[0])
    loop_source = LoopInput(layer, local_duration, fps, executable, (box_w, box_h)) if candidate else None
    live_loop = loop_source is not None and loop_source.overlap > 0
    if dynamic(layer) or live_loop:
        source = (loop_source if live_loop else
                  StreamInput(layer['_matting_binding'], local_duration, layer['media_loop'], fps, executable))
        args += source.arguments()
    elif is_animated(path):
        if layer["media_loop"]:
            args += ["-stream_loop", "-1"]
        args += ["-i", path]
    else:
        args += ["-loop", "1", "-i", path]
    # Fraction gained float-style formatting after our Python 3.10 runtime.
    effect = [f"setpts=PTS-STARTPTS,fps={float(fps):.9f},format=rgba"]
    input_label = f"{input_index}:v"
    consumed = 1
    if layer["key_mode"] != "none" and not live_loop:
        # colorkey replaces (rather than multiplies) an existing alpha channel.
        # Keep that native mask separate so transparent PNG/GIF/MOV pixels do
        # not become visible when the keyed matte is made more opaque.
        input_label, consumed = add_keyed_source(args, filters, layer, input_index, label, fps, is_animated,
                                                duration=local_duration)
        effect = []
    effect += [f"scale={box_w}:{box_h}:force_original_aspect_ratio=decrease"]
    movement = "0"
    if layer["visual_animation"] != "none":
        from .visual_animation import expressions
        scale, movement = expressions(layer, local_duration)
        if scale != "1":
            # Fixed input dimensions, changing output dimensions; preserve the
            # source aspect and re-center at every frame, not at filter init.
            effect.append(f"scale=w='max(1,round(iw*({scale})))':h='max(1,round(ih*({scale})))':eval=frame")
    effect += [f"colorchannelmixer=aa={layer['opacity']/100:.8f}", f"trim=duration={local_duration:.9f}", "setsar=1"]
    filters.append(f"[{input_label}]" + ",".join(effect) + f"[{label}_source]")
    if timing:
        from .overlay_timing import add_timed_plane
        plane = add_timed_plane(filters, label+'_source', label + "_timed", width, height, duration, fps, timing,
                                center=(width*layer['x']/100, height*layer['y']/100),
                                raw_object=True, vertical_movement=f'{box_h}*({movement})')
    else:
        filters.append(f"color=c=black@0:s={width}x{height}:r={float(fps):.9f}:d={local_duration:.9f},format=rgba[{label}_canvas]")
        filters.append(f"[{label}_canvas][{label}_source]overlay=x='{width*layer['x']/100:.4f}-overlay_w/2':"
            f"y='{height*layer['y']/100:.4f}-overlay_h/2+{box_h}*({movement})':eval=frame:"
            f"format=auto:eof_action=pass:repeatlast=0[{label}_plane]")
        plane = label + "_plane"
    if layer["blend_mode"] == "normal":
        filters.append(f"[{background}][{plane}]overlay=0:0:format=auto:eof_action=pass[{label}]")
    else:
        mode = "addition" if layer["blend_mode"] == "add" else "screen"
        filters += [f"[{background}]split=2[{label}_base][{label}_original]",
            f"[{plane}]split=2[{label}_rgb][{label}_alpha_source]",
            f"[{label}_alpha_source]alphaextract[{label}_alpha]",
            f"[{label}_base]format=gbrp[{label}_base_rgb]",
            f"[{label}_rgb]format=gbrp[{label}_source_rgb]",
            f"[{label}_base_rgb][{label}_source_rgb]blend=all_mode={mode}[{label}_blended]",
            f"[{label}_blended][{label}_alpha]alphamerge[{label}_masked]",
            f"[{label}_original][{label}_masked]overlay=0:0:format=auto[{label}]"]
    return consumed
'''


@unittest.skipUnless(Path(FFMPEG).is_file(),'Explicit real FFmpeg required')
class StaticVisualOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # Frozen 1.43 compositor: independent reference for full-frame output,
        # kept in this test so future test runs need no external stage or Git.
        namespace=dict(vars(visual_media))
        exec(compile(REFERENCE_SOURCE,'<1.43-compositor>','exec'),namespace)
        cls.before=types.SimpleNamespace(add_plane=namespace['add_plane'])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        rgba=np.full((96,72,4),[0,255,0,255],np.uint8)
        rgba[12:85,18:55]=[220,180,140,255]
        rgba[20:70,16:18]=[100,180,80,160]
        rgba[25:40,28:42]=[0,255,0,255]
        rgba[5:10,3:8]=[180,80,20,0]
        rgba[45:70,40:50,3]=100
        self.image=self.root/'source.png';Image.fromarray(rgba).save(self.image)
        trimap=np.full((96,72,4),[128,128,128,255],np.uint8)
        trimap[27:35,30:40]=[255,255,255,255]
        trimap[65:75,20:28]=[0,0,0,255]
        self.mask=self.root/'trimap.png';Image.fromarray(trimap).save(self.mask)

    def layer(self,**kw):
        return normalize_layers([dict(id='v',kind='image',path=str(self.image),source='fixed',
            use_saved_matte=False,width=90,height=95,x=50,y=68,opacity=73,
            key_mode='green',key_method='normal',key_similarity=.1,key_softness=.12,
            key_despill=True,**kw)])[0]

    def render(self,layer,renderer,fps=Fraction(10),duration=1.2,trace=False):
        args=[FFMPEG,'-nostdin','-hide_banner','-loglevel','info' if trace else 'error',
            '-filter_complex_threads','1','-f','lavfi','-i',f'color=c=#335599:s=96x128:r={fps}:d={duration}']
        filters=['[0:v]format=rgba[base]']
        renderer.add_plane(args,filters,copy.deepcopy(layer),1,'base','out',96,128,duration,fps)
        if trace:
            # Instrument actual keyed output before looping, not the loop count.
            filters=[v.replace('alphamerge=shortest=1,format=rgba[out_key_source]',
                'alphamerge=shortest=1,format=rgba,showinfo@keywork[out_key_source]') for v in filters]
        args+=['-filter_complex',';'.join(filters),'-map','[out]','-t',str(duration),'-an',
            '-f','rawvideo','-pix_fmt','rgba','-threads','1','pipe:1']
        start=time.perf_counter()
        result=subprocess.run(args,capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,128,96,4)
        return frames,result.stderr.decode('utf-8','replace'),time.perf_counter()-start

    def test_normal_basic_edge_cleanup_and_fractional_grow_match_every_baseline_frame(self):
        for options in ({},{'key_despill_mode':'edge','key_despill_softness':70},
                {'key_cleanup':True,'key_cleanup_size':2,'key_matte_grow':-.5}):
            with self.subTest(options=options):
                layer=self.layer(**options)
                expected,_,_=self.render(layer,self.before)
                actual,log,_=self.render(layer,visual_media,trace=True)
                np.testing.assert_array_equal(actual,expected)
                self.assertEqual(len(actual),12)
                self.assertEqual(len(re.findall(r'showinfo@keywork[^\n]*\bn:\s*\d+',log)),1,log[-3000:])

    def test_enhanced_static_trimap_protection_cleanup_and_source_alpha_match_baseline(self):
        layer=self.layer();layer.update(key_method='enhanced',key_mask_path=str(self.mask),
            key_protect_inside=True,key_cleanup=True,key_despill_mode='edge')
        expected,_,_=self.render(layer,self.before)
        actual,log,_=self.render(layer,visual_media,trace=True)
        np.testing.assert_array_equal(actual,expected)
        self.assertEqual(len(re.findall(r'showinfo@keywork[^\n]*\bn:\s*\d+',log)),1)

    def test_zoom_fades_entrance_and_blend_modes_keep_all_baseline_frames(self):
        for blend in ('normal','add','screen'):
            with self.subTest(blend=blend):
                layer=self.layer(blend_mode=blend,visual_animation='breathe',animation_strength=12,
                    animation_period=.6,visual_timing_enabled=True,
                    visual_timing=dict(start=.1,end=0,enter='up',exit='down',enter_duration=.3,
                        exit_duration=.3,fade_in=.2,fade_out=.2,distance=30,clock='auto'))
                expected,_,_=self.render(layer,self.before)
                actual,_,_=self.render(layer,visual_media)
                np.testing.assert_array_equal(actual,expected)
                self.assertFalse(np.array_equal(actual[2],actual[6]))

    def test_plain_transparent_image_and_fractional_clock_match_baseline(self):
        layer=self.layer();layer.update(key_mode='none',key_despill=False)
        for fps in (Fraction(5),Fraction(24000,1001)):
            with self.subTest(fps=fps):
                expected,_,_=self.render(layer,self.before,fps=fps)
                actual,_,_=self.render(layer,visual_media,fps=fps)
                np.testing.assert_array_equal(actual,expected)

    def test_moving_trimap_is_not_frozen_on_still_source(self):
        mask=self.root/'mask.mkv'
        result=subprocess.run([FFMPEG,'-y','-v','error','-f','lavfi','-i',
            'color=c=black:s=72x96:r=10:d=0.5','-f','lavfi','-i',
            'color=c=white:s=72x96:r=10:d=0.7','-filter_complex',
            '[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','ffv1',str(mask)],
            capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        layer=self.layer();layer.update(key_method='enhanced',key_mask_path=str(mask),key_protect_inside=False)
        expected,_,_=self.render(layer,self.before)
        actual,_,_=self.render(layer,visual_media)
        np.testing.assert_array_equal(actual,expected)
        self.assertFalse(np.array_equal(actual[1],actual[9]))
