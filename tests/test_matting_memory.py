"""Real named-pipe FFmpeg inputs, bounded frame iteration, frozen AI settings."""
import copy
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video import matting_assets as assets, matting_render as render
from app_plugins.builtin.batch_text_video import matting_live as live, engine
from app_plugins.builtin.batch_text_video.components import resolve_sources
from app_plugins.builtin.batch_text_video.matting_settings import normalize
import test_matting_studio as fixture
FakePredictor, FFMPEG = fixture.FakePredictor, fixture.FFMPEG


class MemoryMattingTests(unittest.TestCase):
    setUpClass = classmethod(fixture.MattingTests.setUpClass.__func__)
    setUp = fixture.MattingTests.setUp
    layer = fixture.MattingTests.layer
    wait = fixture.MattingTests.wait

    def save(self, source=None, **parameters):
        return render.save_dynamic(source or self.source, parameters, self.catalogue, ffmpeg=FFMPEG)

    def video(self):
        path = self.root/'source.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x48:r=5:d=0.6',
                        '-c:v', 'libx264', str(path)], check=True, capture_output=True)
        return path

    def encode(self, spec, name='result.mov'):
        target = self.root/name
        args = [FFMPEG, '-y', '-v', 'error', *spec.arguments(), '-an', '-c:v', 'qtrle', '-pix_fmt', 'argb', str(target)]
        engine._run(args, timeout=30, cwd=self.root)
        decoded = subprocess.run([FFMPEG, '-v', 'error', '-i', str(target), '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
                                 check=True, capture_output=True)
        return np.frombuffer(decoded.stdout, np.uint8).reshape(-1, spec.height, spec.width, 4)

    def test_save_is_only_metadata_and_freezes_pool_parameters(self):
        one = self.save(opacity=25)
        self.assertEqual(one['mode'], 'dynamic'); self.assertNotIn('result', one)
        self.assertFalse((self.catalogue.root/'assets').exists())
        frozen = assets.snapshot_layers([self.layer()])
        self.save(opacity=80)
        resolved = resolve_sources(frozen)[0][0]
        self.assertEqual(resolved['path'], str(self.source.resolve()))
        self.assertEqual(resolved['_matting_binding']['parameters']['opacity'], 25)
        self.assertEqual(resolve_sources([self.layer()])[0][0]['_matting_binding']['parameters']['opacity'], 80)
        copied = self.root/'copied.png'; copied.write_bytes(self.source.read_bytes())
        self.assertEqual(self.catalogue.lookup(copied)['parameters']['opacity'], 80)
        self.catalogue.set_enabled(self.source, False)
        self.assertNotIn('_matting_binding', resolve_sources([self.layer()])[0][0])

    def test_first_frame_preview_is_matted_bounded_cached_and_parameter_sensitive(self):
        one = self.save(opacity=25)
        bounds = QtCore.QSize(64, 48)
        first = live.poster(one, bounds)
        calls = FakePredictor.calls
        second = live.poster(one, bounds)
        self.assertEqual(FakePredictor.calls, calls)
        np.testing.assert_array_equal(render.image_array(first), render.image_array(second))
        two = self.save(opacity=80)
        newer = live.poster(two, bounds)
        self.assertGreater(render.image_array(newer)[20, 20, 3], render.image_array(first)[20, 20, 3])
        with patch.object(live, 'POSTER_LIMIT', 1): live.poster(two, QtCore.QSize(32, 24))
        self.assertLessEqual(sum(v.sizeInBytes() for v in live._POSTERS.values()), 1)

    def test_dynamic_workbench_reopen_edit_and_undo_keep_visible_preview(self):
        from app_plugins.builtin.batch_text_video.matting_ui import MattingDialog
        self.save(opacity=70)
        dialog = MattingDialog([str(self.source)], catalogue=self.catalogue, ffmpeg=FFMPEG)
        self.addCleanup(dialog.deleteLater)
        def idle(): self.wait(lambda: dialog.worker is None and not dialog.timer.isActive())
        idle(); self.assertFalse(dialog.canvas.cutout.isNull()); self.assertEqual(dialog.values['opacity'], 70)
        dialog.controls['opacity'].setValue(25); idle()
        np.testing.assert_array_equal(render.image_array(dialog.canvas.cutout), render.refined(dialog.base, dialog.values))
        dialog.undo_edit(); idle(); self.assertEqual(dialog.values['opacity'], 70)
        dialog.submit('save'); idle(); self.assertNotIn('result', self.catalogue.lookup(self.source))
        self.assertFalse((self.catalogue.root/'assets').exists()); dialog.close()

    def test_explicit_export_keeps_preview_and_does_not_create_managed_cache(self):
        from app_plugins.builtin.batch_text_video.matting_ui import MattingDialog
        record = self.save(opacity=50); before = self.catalogue.path.read_bytes()
        dialog = MattingDialog([str(self.source)], catalogue=self.catalogue, ffmpeg=FFMPEG)
        self.addCleanup(dialog.deleteLater); self.wait(lambda: dialog.worker is None)
        pixels = render.image_array(dialog.canvas.cutout)
        target = self.root/'explicit-export.png'
        dialog.submit('export', record=record, destination=str(target)); self.wait(lambda: dialog.worker is None)
        self.assertTrue(target.is_file(), dialog.status.text())
        np.testing.assert_array_equal(render.image_array(dialog.canvas.cutout), pixels)
        np.testing.assert_array_equal(np.array(Image.open(target)), pixels)
        self.assertEqual(self.catalogue.path.read_bytes(), before)
        self.assertFalse((self.catalogue.root/'assets').exists()); dialog.close()

    def test_fixed_node_binds_dynamic_parameters_and_original_resource(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import prepare_layer, build_layer
        from types import SimpleNamespace
        one = self.save(opacity=20)
        params = dict(layer=self.layer(source='file', source_file=str(self.source)))
        prepared = prepare_layer(None, params, None); self.save(opacity=90)
        result = build_layer({}, params, SimpleNamespace(node_prepared=prepared), media='image')
        operation = result['operations'][0]
        value = json.loads(dict(operation.options)['layer'])
        self.assertEqual(value['_matting_binding']['id'], one['id'])
        self.assertEqual(value['_matting_binding']['parameters']['opacity'], 20)
        self.assertIn(str(self.source.resolve()), [v.path for _, v in operation.assets])

    def test_source_node_output_carries_frozen_parameters_to_visual_node(self):
        from app_plugins.builtin.batch_text_video.workflow_component_source import prepare_source, build_source
        from app_plugins.builtin.batch_text_video.workflow_layers import build_layer
        from app_plugins.builtin.batch_text_video.node_effects import TYPE_ID
        from types import SimpleNamespace
        self.save(opacity=20)
        params = dict(source=self.layer(source='file', source_file=str(self.source)))
        context = SimpleNamespace(node_prepared=prepare_source(None, params, None), workflow_input=dict(type=TYPE_ID), node_id='source')
        self.save(opacity=90)
        source = build_source({}, params, context)['content']
        result = build_layer(dict(content=source), dict(layer=self.layer()), SimpleNamespace(node_prepared={}), media='image')
        value = json.loads(dict(result['operations'][0].options)['layer'])
        self.assertEqual(value['_matting_binding']['parameters']['opacity'], 20)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_actual_final_playback_and_converted_node_render_use_same_dynamic_matte(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.store import fresh_state, DEFAULTS
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry, new_effect, prepare_bundle, EffectRuntime
        from model.node_workflow.library import WorkflowLibrary
        self.save(opacity=50)
        background = self.root/'background.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=240x160:r=10:d=0.6',
                        '-c:v', 'libx264', str(background)], check=True, capture_output=True)
        layer = self.layer(key_mode='green', width=100, height=100)
        registry = fallback_registry(); graph, _ = component_graph(layer, DEFAULTS, registry)
        library = WorkflowLibrary(self.root/'workflows'); stored = library.save(graph, 'dynamic matting', registry)
        effect = new_effect(); effect['workflow_id'] = stored['workflow_id']
        services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
        frames = []
        for variant in ('final', 'playback', 'node'):
            state = fresh_state(); state['settings'].update(aspect='source', darkness=0, music_enabled=False, use_voice=False,
                ffmpeg_path=FFMPEG, output_dir=str(self.root/'out'), layers=[effect if variant == 'node' else layer])
            job = dict(id='job', path=str(background), name=variant, title='', body='', task_dir=''); state['jobs'] = [job]
            runtime = None
            if variant == 'node':
                bundle, registry, _ = prepare_bundle(state, ['job'], services); runtime = EffectRuntime(bundle, registry)
            output, _ = engine.render_static_text(job, state, self.root/'private-render', node_effects=runtime, preview=variant == 'playback')
            data = subprocess.run([FFMPEG, '-v', 'error', '-i', str(output), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                  check=True, capture_output=True).stdout
            frames.append(np.frombuffer(data, np.uint8).reshape(160, 240, 3))
        for frame in frames[1:]: self.assertLess(np.abs(frames[0].astype(float)-frame).mean(), 2)
        self.assertGreater(frames[0][5, 5, 2], 240)
        self.assertGreater(frames[0][80, 120, 1], 30)
        self.assertFalse(list(self.catalogue.root.rglob('*.mov')))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_rotated_dynamic_video_uses_display_dimensions(self):
        path = self.video(); rotated = self.root/'rotated.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-display_rotation:v:0', '90', '-i', str(path), '-c', 'copy', str(rotated)],
                        check=True, capture_output=True)
        spec = live.StreamInput(self.save(rotated, model='rvm'), .6, False, 5, FFMPEG)
        self.assertEqual((spec.width, spec.height), (48, 64))
        self.assertEqual(self.encode(spec).shape, (3, 64, 48, 4))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_node_editor_single_frame_preview_and_settler_render_dynamic_input(self):
        import base64
        from dataclasses import replace
        from model.BackgroundTasks import TaskRunContext
        from model.node_workflow.contracts import RenderPlan, ResourceRef
        from model.node_workflow.render import render_plan
        from model.node_workflow.preview import frame_preview
        from app_plugins.builtin.batch_text_video.workflow_layers import prepare_layer, build_layer
        self.save(opacity=50)
        context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        context.ffmpeg = FFMPEG
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        context.workflow_registry = fallback_registry()
        context.node_prepared = prepare_layer(None, dict(layer=self.layer()), None)
        operation = build_layer({}, dict(layer=self.layer(width=100, height=100)), context, media='image')['operations'][0]
        operation = replace(operation, video=None)
        video = ResourceRef.capture(self.video(), 'video')
        operation = replace(operation, video=video)
        plan = RenderPlan(video, (operation,), str(self.root/'node-final.mp4'))
        preview = self.root/'node-preview'; preview.mkdir()
        encoded, at = frame_preview(plan, .2, context, preview)
        image = render.image_array(__import__('qt_compat').QtGui.QImage.fromData(base64.b64decode(encoded)))
        self.assertGreater(image[20, 20, 1], image[0, 0, 1]+20)
        result = render_plan(plan, context, FFMPEG)
        self.assertTrue(Path(result['video']).is_file())
        self.assertEqual(result['intermediate_videos'], 0)
        self.assertFalse(list(self.catalogue.root.rglob('*.mov')))

    def test_cancel_or_changed_source_never_replaces_metadata(self):
        self.save(); before = self.catalogue.path.read_bytes()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(render.MattingCancelled): self.save_cancel(cancel)
        self.assertEqual(self.catalogue.path.read_bytes(), before)
        base = render.infer(self.source, {})
        self.pixels[3, 3, 0] += 1; Image.fromarray(self.pixels).save(self.source)
        with self.assertRaisesRegex(ValueError, '已改变'):
            render.save_dynamic(self.source, {}, self.catalogue, base=base)

    def save_cancel(self, cancel): return render.save_dynamic(self.source, {}, self.catalogue, cancel=cancel)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_real_pipe_image_alpha_and_repeat_encoder_attempt(self):
        record = self.save(opacity=50)
        spec = live.StreamInput(record, .6, True, 5, FFMPEG)
        one = self.encode(spec)
        two = self.encode(spec, 'again.mov')
        self.assertEqual(one.shape, (3, 48, 64, 4))
        np.testing.assert_array_equal(one, two)
        self.assertEqual(one[:, 0, 0, 3].tolist(), [0, 0, 0])
        self.assertEqual(one[:, 20, 20, 3].tolist(), [64, 64, 64])
        self.assertFalse((self.catalogue.root/'assets').exists())

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_real_pipe_video_loop_temporal_state_and_no_loop_eof(self):
        path = self.video(); record = self.save(path, model='rvm')
        frames = self.encode(live.StreamInput(record, 1.2, True, 25, FFMPEG))
        self.assertEqual(frames[:, 20, 20, 3].tolist(), [128, 255, 128, 128, 255, 128])
        frames = self.encode(live.StreamInput(record, 1.2, False, 25, FFMPEG), 'no-loop.mov')
        self.assertEqual(len(frames), 3)
        self.assertFalse(list(self.catalogue.root.rglob('*.mov')))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_real_compositor_two_dynamic_inputs_keep_transparency(self):
        from app_plugins.builtin.batch_text_video.visual_media import add_plane
        from fractions import Fraction
        self.save(opacity=100)
        layer = resolve_sources([self.layer(x=50, y=50, width=100, height=100)])[0][0]
        args = [FFMPEG, '-y', '-v', 'error', '-filter_complex_threads', '1', '-f', 'lavfi', '-i', 'color=c=blue:s=64x48:r=5:d=0.6']
        filters = []
        add_plane(args, filters, layer, 1, '0:v', 'first', 64, 48, .6, Fraction(5))
        add_plane(args, filters, layer, 2, 'first', 'second', 64, 48, .6, Fraction(5))
        target = self.root/'composite.mov'
        args += ['-filter_complex', ';'.join(filters), '-map', '[second]', '-t', '0.6', '-c:v', 'qtrle', str(target)]
        engine._run(args, timeout=30)
        pixels = render.load_source(target, ffmpeg=FFMPEG)['source']
        self.assertGreater(pixels[0, 0, 2], 200)
        self.assertGreater(pixels[20, 20, 1], pixels[0, 0, 1]+50)

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_cancel_closes_pipe_and_decoder_without_waiting_for_full_clip(self):
        record = self.save(self.video(), model='rvm')
        spec = live.StreamInput(record, 600, True, 5, FFMPEG)
        cancel = threading.Event()
        timer = threading.Timer(.4, cancel.set); timer.start(); self.addCleanup(timer.cancel)
        target = self.root/'cancel.mov'
        started = time.monotonic()
        with self.assertRaises(engine.Cancelled):
            engine._run([FFMPEG, '-y', '-v', 'error', *spec.arguments(), '-c:v', 'qtrle', str(target)], cancel, timeout=30)
        self.assertLess(time.monotonic()-started, 8)
        self.assertFalse(any(t.name == 'matting-frame-feed' for t in threading.enumerate()))

    @unittest.skipUnless(os.name == 'nt' and FFMPEG, 'Windows FFmpeg required')
    def test_inference_failure_exits_and_reports_error(self):
        spec = live.StreamInput(self.save(), 2, True, 5, FFMPEG)
        with patch.object(render, 'Predictor', side_effect=ValueError('测试推理失败')):
            with self.assertRaisesRegex(RuntimeError, '测试推理失败'):
                engine._run([FFMPEG, '-y', '-v', 'error', *spec.arguments(), '-c:v', 'qtrle', str(self.root/'bad.mov')], timeout=10)
        self.assertFalse(any(t.name == 'matting-frame-feed' for t in threading.enumerate()))

    def test_ai_entry_forwards_current_layer_without_editing_values(self):
        from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
        panel = LayerPanel([self.layer()], self.root); self.addCleanup(panel.deleteLater)
        panel.inspector.matting_available = True
        index = next(i for i in range(panel.list.count()) if panel.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == 'picture')
        panel.list.setCurrentRow(index); panel.select_layer(index)
        editor = panel.inspector.active.visual_properties
        self.assertFalse(editor.matting_button.isHidden())
        selected = []; panel.mattingRequested.connect(selected.append)
        before = copy.deepcopy(panel._layers)
        editor.matting_button.click()
        self.assertEqual(selected, ['picture']); self.assertEqual(panel._layers, before)
