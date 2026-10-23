"""Actual loop seam frames, transparent joins, AI and native/node composition."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import engine, matting_live as live, matting_render as render
from app_plugins.builtin.batch_text_video import matting_assets as assets, visual_loop as loops
from app_plugins.builtin.batch_text_video.visual_media import VisualProperties, normalize_visual, add_plane
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.components import resolve_sources
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from test_matting_studio import FakePredictor

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class LoopDissolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)

    def layer(self, **values):
        layer = dict(id='visual', kind='image', path=str(self.root/'source.mp4'), source='fixed',
            width=100, height=100, media_loop_transition='fade', media_loop_fade=.4)
        layer.update(values)
        return normalize_layers([layer])[0]

    def media(self, keyed=False):
        path = self.root/'source.mp4'
        pixels = np.zeros((20, 48, 64, 4), np.uint8); pixels[:, :, :, 3] = 255
        if keyed:
            pixels[:, :, :, 1] = 255
            pixels[:10, 12:36, 4:24, :3] = [255, 0, 0]
            pixels[10:, 12:36, 40:60, :3] = [255, 0, 0]
        else:
            pixels[:10, :, :, 0] = 255; pixels[10:, :, :, 2] = 255
        result = subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', '64x48',
            '-r', '10', '-i', 'pipe:0', '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', str(path)],
            input=pixels.tobytes(), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return path

    def encode(self, spec, target='processed.mov'):
        target = self.root/target
        engine._run([FFMPEG, '-y', '-v', 'error', *spec.arguments(), '-c:v', 'qtrle', '-pix_fmt', 'argb', str(target)], timeout=30)
        data = subprocess.run([FFMPEG, '-v', 'error', '-i', str(target), '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
                              capture_output=True, check=True).stdout
        return np.frombuffer(data, np.uint8).reshape(-1, spec.height, spec.width, 4)

    def test_controls_below_loop_and_saved_values_are_independent_from_background_settings(self):
        panel = VisualProperties(); self.addCleanup(panel.deleteLater)
        layer = self.layer(); panel.set_layer(layer)
        values = []; panel.changed.connect(lambda v: values.append(copy.deepcopy(v)))
        panel.controls['media_loop_fade'].setValue(.7)
        self.assertEqual(values[-1]['media_loop_fade'], .7)
        self.assertTrue(panel.controls['media_loop_transition'].isEnabled())
        panel.controls['media_loop'].setChecked(False)
        self.assertFalse(panel.controls['media_loop_fade'].isEnabled())
        self.assertFalse(panel.controls['media_loop_transition'].isEnabled())
        panel.controls['media_loop'].setChecked(True)
        panel.controls['media_loop_transition'].setCurrentIndex(0)
        self.assertFalse(panel.controls['media_loop_fade'].isEnabled())
        state = fresh_state(); state['settings']['layers'] = [layer]
        store = Store(self.root/'store'); store.save(state, 0)
        self.assertEqual(store.load()['settings']['layers'][0]['media_loop_fade'], .4)
        self.assertEqual(state['settings']['transition_seconds'], .4)
        for bad in (-.1, 4, True, float('nan')):
            with self.assertRaises(ValueError): normalize_visual({**layer, 'media_loop_fade': bad})

    def test_blending_uses_premultiplied_alpha(self):
        tail = bytes([0, 255, 0, 0]); head = bytes([255, 0, 0, 255])
        self.assertEqual(list(loops.blend(tail, head, .5, 1, 1)), [255, 0, 0, 128])
        self.assertEqual(list(loops.blend(tail, tail, .5, 1, 1)), [0, 0, 0, 0])
        pixels = np.array([[[0, 255, 0, 0], [255, 0, 0, 255]]], np.uint8)
        self.assertEqual(loops.resize_rgba(pixels, 1, 1)[0, 0].tolist(), [255, 0, 0, 128])

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_native_playback_and_converted_node_dissolve_match(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry, new_effect, prepare_bundle, EffectRuntime
        from app_plugins.builtin.batch_text_video.store import DEFAULTS
        from model.node_workflow.library import WorkflowLibrary
        self.media(keyed=True)
        background = self.root/'background.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=240x160:r=10:d=4',
                        '-c:v', 'libx264', str(background)], capture_output=True, check=True)
        layer = self.layer(key_mode='green', key_similarity=.28, key_softness=.2,
            visual_timing_enabled=True, visual_timing=dict(enter='left', exit='right', enter_duration=.2, exit_duration=.2, fade_in=0, fade_out=0))
        registry = fallback_registry(); graph, _ = component_graph(layer, DEFAULTS, registry)
        library = WorkflowLibrary(self.root/'workflows'); stored = library.save(graph, 'loop test', registry)
        effect = new_effect(); effect['workflow_id'] = stored['workflow_id']
        services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
        outputs = []
        for variant in ('native', 'playback', 'node'):
            state = fresh_state(); state['settings'].update(aspect='source', darkness=0, music_enabled=False, use_voice=False,
                ffmpeg_path=FFMPEG, output_dir=str(self.root/'out'), layers=[effect if variant == 'node' else layer])
            job = dict(id='job', path=str(background), name=variant, title='', body='', task_dir=''); state['jobs']=[job]
            runtime = None
            if variant == 'node':
                bundle, registry, _ = prepare_bundle(state, ['job'], services); runtime = EffectRuntime(bundle, registry)
            output, _ = engine.render_static_text(job, state, self.root/'render-data', preview=variant == 'playback', node_effects=runtime)
            data = subprocess.run([FFMPEG, '-v', 'error', '-i', str(output), '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                  capture_output=True, check=True).stdout
            outputs.append(np.frombuffer(data, np.uint8).reshape(-1, 160, 240, 3))
        self.assertEqual(len(outputs[0]), 40)
        for frames in outputs[1:]: self.assertLess(np.abs(outputs[0].astype(float)-frames).mean(), 3)
        self.assertTrue(30 < outputs[0][17, 80, 54, 0] < 160)
        self.assertTrue(30 < outputs[0][17, 80, 180, 0] < 220)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_actual_plain_loop_dissolves_and_repeats_without_fading_to_black(self):
        self.media(); spec = loops.LoopInput(self.layer(), 4.8, 10, FFMPEG, (64, 48))
        self.assertEqual(spec.overlap, 4)
        frames = self.encode(spec)
        self.assertEqual(len(frames), 48)
        samples = frames[16:20, 24, 32]
        self.assertGreater(samples[-1, 0], 190); self.assertGreater(samples[0, 2], 190)
        self.assertGreater(samples[0, 0], 30); self.assertGreater(samples[-1, 2], 30)
        self.assertTrue(all(samples[i+1, 0] > samples[i, 0]+40 for i in range(3)))
        self.assertTrue(all(samples[:, 0].astype(int)+samples[:, 2].astype(int) > 245))
        self.assertTrue(np.all(samples[:, 3] == 255))
        np.testing.assert_array_equal(frames[16:20], frames[32:36])

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_actual_gif_loop_dissolves(self):
        source = self.media(); gif = self.root/'source.gif'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-i', str(source), '-loop', '0', str(gif)], capture_output=True, check=True)
        spec = loops.LoopInput(self.layer(path=str(gif)), 4, 10, FFMPEG, (64, 48))
        frames = self.encode(spec)
        seam = round(spec.clip_duration*float(spec.fps))-spec.overlap+spec.overlap//2
        pixel = frames[seam, 24, 32]
        self.assertTrue(30 < pixel[0] < 240 and 30 < pixel[2] < 240, pixel)
        self.assertEqual(pixel[3], 255)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_node_editor_single_frame_preview_reads_dissolved_loop(self):
        import base64
        from dataclasses import replace
        from model.BackgroundTasks import TaskRunContext
        from model.node_workflow.contracts import RenderPlan, ResourceRef
        from model.node_workflow.preview import frame_preview
        from app_plugins.builtin.batch_text_video.workflow_layers import prepare_layer, build_layer
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        self.media()
        background = self.root/'background.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=240x160:r=10:d=4',
                        '-c:v', 'libx264', str(background)], capture_output=True, check=True)
        context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        context.ffmpeg = FFMPEG; context.workflow_registry = fallback_registry()
        params = dict(layer=self.layer())
        context.node_prepared = prepare_layer(None, params, None)
        operation = build_layer({}, params, context, media='animated')['operations'][0]
        video = ResourceRef.capture(background, 'video')
        plan = RenderPlan(video, (replace(operation, video=video),), '')
        folder = self.root/'preview'; folder.mkdir()
        encoded, at = frame_preview(plan, 1.7, context, folder)
        image = QtGui.QImage.fromData(base64.b64decode(encoded))
        pixel = image.pixelColor(120, 80)
        self.assertTrue(40 < pixel.red() < 200 and 40 < pixel.blue() < 200, pixel.name())
        self.assertFalse(any(t.name == 'matting-frame-feed' for t in threading.enumerate()))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_actual_green_key_is_applied_before_dissolving_alpha_edges(self):
        self.media(keyed=True)
        spec = loops.LoopInput(self.layer(key_mode='green', key_similarity=.28, key_softness=.2), 4, 10, FFMPEG, (64, 48))
        frames = self.encode(spec)
        self.assertTrue(np.all(frames[:, 0, 0, 3] == 0))
        self.assertTrue(50 < frames[17, 24, 12, 3] < 150)
        self.assertTrue(130 < frames[17, 24, 50, 3] < 220)
        for x in (12, 50):
            self.assertGreater(frames[17, 24, x, 0], 220)
            self.assertLess(frames[17, 24, x, 1], 20)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_enhanced_key_auxiliary_mask_is_used_and_frozen_for_loop_input(self):
        self.media(keyed=True)
        mask = QtGui.QImage(64, 48, QtGui.QImage.Format.Format_RGBA8888); mask.fill(QtGui.QColor(128, 128, 128))
        painter = QtGui.QPainter(mask); painter.fillRect(28, 20, 8, 8, QtGui.QColor('white')); painter.end()
        path = self.root/'keep.png'; self.assertTrue(mask.save(str(path)))
        layer = self.layer(key_mode='green', key_method='enhanced', key_protect_inside=False,
            key_mask_path=str(path), key_similarity=.28, key_softness=.2)
        spec = loops.LoopInput(layer, 4, 10, FFMPEG, (64, 48))
        frames = self.encode(spec)
        self.assertTrue(np.all(frames[:, 0, 0, 3] == 0))
        self.assertTrue(np.all(frames[:, 24, 32, 3] == 255))
        self.assertGreater(frames[17, 24, 32, 1], 240)
        mask.fill(QtGui.QColor('black')); self.assertTrue(mask.save(str(path)))
        with self.assertRaisesRegex(RuntimeError, '辅助遮罩.*已改变'):
            self.encode(spec, 'changed-mask.mov')

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_actual_ai_matte_loops_in_ram_without_saved_transparent_video(self):
        path = self.media(); catalogue = assets.Catalogue(self.root/'matting')
        with patch.object(render, 'Predictor', FakePredictor), patch.object(assets, 'default_root', return_value=catalogue.root):
            render.save_dynamic(path, {'model': 'rvm'}, catalogue, ffmpeg=FFMPEG)
            layer = resolve_sources([self.layer()])[0][0]
            frames = self.encode(loops.LoopInput(layer, 4, 10, FFMPEG, (64, 48)))
        self.assertEqual(len(frames), 40)
        self.assertTrue(np.all(frames[:, 0, 0, 3] == 0))
        self.assertGreater(frames[17, 24, 32, 0], 50)
        self.assertGreater(frames[17, 24, 32, 2], 100)
        self.assertFalse(list(catalogue.root.rglob('*.mov')))
        self.assertFalse(list(catalogue.root.rglob('*.png')))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_overlap_buffers_are_bounded_independent_of_render_length(self):
        self.media()
        with patch.object(loops, 'BUFFER_LIMIT', 64*48*4*4):
            short = loops.LoopInput(self.layer(), 4, 10, FFMPEG, (64, 48))
            long = loops.LoopInput(self.layer(), 600, 10, FFMPEG, (64, 48))
        self.assertEqual(short.overlap, 2); self.assertEqual(long.overlap, 2)
        self.assertEqual(short.width*short.height*8*short.overlap, 64*48*4*4)
        within = loops.LoopInput(self.layer(), 1, 10, FFMPEG, (64, 48))
        self.assertEqual(within.overlap, 0)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_cancel_releases_decoder_and_pipe_during_loop(self):
        self.media(); spec = loops.LoopInput(self.layer(), 600, 10, FFMPEG, (64, 48))
        cancel = threading.Event(); timer = threading.Timer(.3, cancel.set)
        timer.start(); self.addCleanup(timer.cancel)
        with self.assertRaises(engine.Cancelled):
            engine._run([FFMPEG, '-y', '-v', 'error', *spec.arguments(), '-c:v', 'qtrle', str(self.root/'cancel.mov')], cancel, timeout=30)
        self.assertFalse(any(t.name == 'matting-frame-feed' for t in threading.enumerate()))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_switch_off_and_nonlooping_still_use_normal_input(self):
        from fractions import Fraction
        self.media()
        for extra in ({'media_loop': False}, {'media_loop_transition': 'none'}, {'media_loop_fade': 0}):
            args, filters = [FFMPEG], []
            layer = self.layer(); layer.update(extra)
            add_plane(args, filters, layer, 0, 'base', 'out', 64, 48, 4, Fraction(10))
            self.assertFalse(any(isinstance(a, loops.LoopInput) for a in args))
