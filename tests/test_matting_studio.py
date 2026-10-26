"""Material-specific matting; deterministic masks, real PNG/MOV pipelines."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import matting_assets as assets
from app_plugins.builtin.batch_text_video import matting_render as render
from app_plugins.builtin.batch_text_video.matting_settings import normalize, refine, brush_mask
from app_plugins.builtin.batch_text_video.matting_models import MattingCancelled
from app_plugins.builtin.batch_text_video.components import resolve_sources, commit_sequences
from app_plugins.builtin.batch_text_video.layers import normalize_layers, import_images

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class FakePredictor:
    calls = 0
    def __init__(self, *args, **kwargs): self.device = 'CPU'; self.frame = 0
    def predict(self, rgb):
        type(self).calls += 1; self.frame += 1
        a = np.zeros(rgb.shape[:2], np.float32)
        a[8:-8, 8:-8] = .5 if self.frame % 2 else 1.
        return rgb.copy(), a


class MattingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.catalogue = assets.Catalogue(self.root/'private')
        self.default = patch.object(assets, 'default_root', return_value=self.catalogue.root); self.default.start(); self.addCleanup(self.default.stop)
        self.source = self.root/'source.png'
        self.pixels = np.full((48, 64, 4), [100, 180, 70, 255], np.uint8)
        self.pixels[0, 0, 3] = 0
        Image.fromarray(self.pixels).save(self.source)
        self.ai = patch.object(render, 'Predictor', FakePredictor); self.ai.start(); self.addCleanup(self.ai.stop)
        FakePredictor.calls = 0

    def layer(self, **values):
        return normalize_layers([dict(id='picture', kind='image', name='测试图', path=str(self.source), **values)])[0]

    def publish(self, source=None, **parameters):
        return render.save(source or self.source, parameters, self.catalogue)

    def test_neutral_alpha_exact_and_integer_opacity_does_not_overflow(self):
        alpha = np.tile(np.linspace(0, 1, 64, dtype=np.float32), (48, 1))
        data = refine(self.pixels[:, :, :3], alpha, self.pixels[:, :, 3], {'opacity': 100})
        np.testing.assert_array_equal(data[:, :, 3], np.rint(alpha*self.pixels[:, :, 3]).astype(np.uint8))
        np.testing.assert_array_equal(data[:, :, :3], self.pixels[:, :, :3])
        half = refine(self.pixels[:, :, :3], alpha, self.pixels[:, :, 3], {'opacity': 50})
        np.testing.assert_array_equal(half[:, :, 3], np.rint(data[:, :, 3].astype(float)*.5))

    def test_black_white_clips_clean_black_and_white(self):
        a = np.full((48, 64), .5, np.float32)
        def result(**p): return refine(self.pixels[:, :, :3], a, self.pixels[:, :, 3], p)[20, 20, 3]
        self.assertEqual(result(black_clip=50), 0); self.assertEqual(result(white_clip=50), 255)
        self.assertLess(result(clean_black=100), result()); self.assertGreater(result(clean_white=100), result())

    def test_shape_refinement_and_transparent_source_never_revived(self):
        a = np.zeros((48, 64), np.float32); a[16:32, 20:44] = 1
        def result(**p): return refine(self.pixels[:, :, :3], a, self.pixels[:, :, 3], p)[:, :, 3]
        self.assertGreater(np.count_nonzero(result(grow=2)), np.count_nonzero(result()))
        self.assertLess(np.count_nonzero(result(grow=-2)), np.count_nonzero(result()))
        outer, inner = result(blur=2, in_out=100), result(blur=2, in_out=-100)
        self.assertGreater(np.sum(outer), np.sum(inner))
        for p in ({'invert': True}, {'grow': 20}, {'blur': 5}, {'smooth': 100}, {'clean_white': 100}):
            with self.subTest(p=p): self.assertEqual(result(**p)[0, 0], 0)
        a[3, 3] = 1
        self.assertEqual(result(denoise=1)[3, 3], 0)

    def test_brush_order_original_alpha_cleanup_and_despill(self):
        strokes = [dict(mode='keep', radius=.025, points=[[.5, .5]]), dict(mode='remove', radius=.01, points=[[.5, .5]])]
        mask = brush_mask((48, 64), strokes); self.assertEqual(mask[24, 32], 0)
        a = np.zeros((48, 64), np.float32); a[12:40, 8:32] = 1; a[3:5, 45:47] = 1
        result = refine(self.pixels[:, :, :3], a, self.pixels[:, :, 3], {'cleanup': True})
        self.assertEqual(result[3, 45, 3], 0); self.assertEqual(result[24, 16, 3], 255)
        corrected = refine(self.pixels[:, :, :3], a, self.pixels[:, :, 3], {'despill': 'green', 'despill_luma': 0})
        self.assertLess(corrected[12, 8, 1], self.pixels[12, 8, 1])
        np.testing.assert_array_equal(corrected[:, :, 3], np.rint(a*255).astype(np.uint8))
        kept = refine(self.pixels[:, :, :3], a*0, self.pixels[:, :, 3], {'strokes': strokes[:1]})
        self.assertEqual(kept[24, 32, 3], 255)

    def test_invalid_parameters_and_independent_defaults(self):
        for p in ([1], {'opacity': True}, {'white_clip': 40, 'black_clip': 50}, {'denoise': 1.5}, {'blur': float('nan')}, {'unknown': 1}):
            with self.subTest(p=p), self.assertRaises(ValueError): normalize(p)
        first = normalize(); first['strokes'].append('bad'); self.assertEqual(normalize()['strokes'], [])

    def test_cpu_runtime_rejects_explicit_cuda_and_reports_actual_cpu(self):
        import sys
        from app_plugins.builtin.batch_text_video import matting_models as models
        fake = SimpleNamespace(get_available_providers=lambda: ['CPUExecutionProvider'], SessionOptions=SimpleNamespace,
            InferenceSession=lambda *args, **kwargs: SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider']))
        with patch.dict(sys.modules, onnxruntime=fake), patch.object(models, 'ensure_model', return_value=self.source):
            with self.assertRaisesRegex(RuntimeError, 'CUDA'): models.session('rvm', 'cuda')
            _, actual = models.session('rvm', 'auto'); self.assertEqual(actual, 'CPU')

    def test_tampered_model_and_cancelled_download_never_start_inference(self):
        from app_plugins.builtin.batch_text_video import matting_models as models
        folder = self.root/'models'; folder.mkdir(); (folder/models.MODELS['rvm']['filename']).write_bytes(b'bad model')
        with self.assertRaisesRegex(ValueError, '模型损坏'): models.ensure_model('rvm', folder)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(MattingCancelled): models.ensure_model('isnet', folder, cancel)

    def test_real_canvas_brush_event_uses_material_coordinates(self):
        from PyQt6.QtTest import QTest
        from app_plugins.builtin.batch_text_video.matting_canvas import MattingCanvas
        canvas = MattingCanvas(); self.addCleanup(canvas.deleteLater); canvas.resize(320, 240)
        canvas.set_images(render.qimage(self.pixels)); canvas.mode = 'keep'; canvas.brush_size = 3; canvas.show()
        strokes = []; canvas.strokeAdded.connect(strokes.append)
        QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=canvas.image_rect().center().toPoint())
        self.assertEqual(len(strokes), 1); self.assertEqual(strokes[0]['mode'], 'keep')
        self.assertAlmostEqual(strokes[0]['points'][0][0], .5, delta=.01)
        canvas.editable = False
        QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=canvas.image_rect().center().toPoint())
        self.assertEqual(len(strokes), 1); canvas.close()

    def test_save_png_parameters_immutable_versions_and_reuse_without_inference(self):
        first = self.publish(black_clip=10, blur=1)
        result = np.array(Image.open(first['result']['path']))
        self.assertEqual(result.shape, self.pixels.shape); self.assertEqual(result[0, 0, 3], 0)
        self.assertEqual(first['parameters']['black_clip'], 10)
        before = Path(first['result']['path']).read_bytes()
        second = self.publish(opacity=0)
        self.assertNotEqual(first['id'], second['id']); self.assertEqual(Path(first['result']['path']).read_bytes(), before)
        with patch.object(render, 'Predictor', side_effect=AssertionError('pool must not run AI')):
            resolved = resolve_sources([self.layer(key_mode='green', use_saved_matte=True)])[0][0]
            self.assertEqual(resolved['path'], second['result']['path']); self.assertEqual(resolved['key_mode'], 'none')
        np.testing.assert_array_equal(np.array(Image.open(self.source)), self.pixels)

    def test_pool_per_material_parameters_rotation_plan_and_opt_out(self):
        one = self.publish(opacity=20)
        other = self.root/'other.png'; Image.fromarray(self.pixels[::-1].copy()).save(other)
        two = self.publish(other, opacity=80)
        layer = self.layer(source='sequence', sequence_items=[dict(id='one', path=str(self.source)), dict(id='two', path=str(other))])
        resolved, plan = resolve_sources([layer]); self.assertEqual(resolved[0]['path'], one['result']['path'])
        next_layers = commit_sequences([layer], plan)
        self.assertEqual(next_layers[0]['sequence_cursor'], 1)
        self.assertEqual(resolve_sources(next_layers)[0][0]['path'], two['result']['path'])
        layer['use_saved_matte'] = False
        self.assertEqual(resolve_sources([layer])[0][0]['path'], str(self.source))
        self.assertEqual(layer['sequence_cursor'], 0)

    def test_repeated_resolution_and_republish_keep_job_binding_frozen(self):
        first = self.publish(); layer = self.layer(source='file', source_file=str(self.source))
        resolved = resolve_sources([layer])[0]; self.publish(opacity=20)
        self.assertEqual(resolve_sources(resolved)[0][0]['path'], first['result']['path'])
        self.assertNotEqual(resolve_sources([layer])[0][0]['path'], first['result']['path'])

    def test_snapshot_freezes_absence_and_result(self):
        before = assets.snapshot_layers([self.layer()]); one = self.publish()
        self.assertEqual(resolve_sources(before)[0][0]['path'], str(self.source))
        frozen = assets.snapshot_layers([self.layer()]); self.publish(opacity=10)
        self.assertEqual(resolve_sources(frozen)[0][0]['path'], one['result']['path'])

    def test_unused_stale_fallback_does_not_block_connected_source(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import prepare_layer, build_layer
        saved = self.publish(); Path(saved['result']['path']).unlink()
        other = self.root/'valid.png'; Image.new('RGB', (16, 16), 'blue').save(other)
        params = dict(layer=self.layer(source='file', source_file=str(self.source)))
        c = SimpleNamespace(node_prepared=prepare_layer(None, params, None))
        with self.assertRaisesRegex(ValueError, '抠像结果'): build_layer({}, params, c, media='visual')
        operation = build_layer({'content': str(other)}, params, c, media='visual')['operations'][0]
        self.assertEqual(json.loads(dict(operation.options)['layer'])['path'], str(other))

    def test_missing_result_changed_source_and_changed_result_fail_clearly(self):
        saved = self.publish(); path = Path(saved['result']['path']); original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(FileNotFoundError, '抠像结果'): self.catalogue.lookup(self.source)
        path.write_bytes(original+b'changed')
        with self.assertRaisesRegex(ValueError, '已改变'): self.catalogue.lookup(self.source)
        path.write_bytes(original); self.pixels[20, 20, 0] = 1; Image.fromarray(self.pixels).save(self.source)
        with self.assertRaisesRegex(ValueError, '原素材已改变'): self.catalogue.lookup(self.source)
        self.assertEqual(resolve_sources([self.layer(use_saved_matte=False)])[0][0]['path'], str(self.source))

    def test_disabled_source_does_not_inherit_same_content_again(self):
        saved = self.publish(); copied = self.root/'copy.png'; copied.write_bytes(self.source.read_bytes())
        self.assertEqual(self.catalogue.lookup(copied)['id'], saved['id'])
        self.catalogue.set_enabled(copied, False); self.assertIsNone(self.catalogue.lookup(copied))
        self.assertIsNotNone(self.catalogue.lookup(self.source))
        self.catalogue.set_enabled(copied, True); self.assertIsNotNone(self.catalogue.lookup(copied))

    def test_imported_copy_does_not_silently_revert_when_saved_result_missing(self):
        saved = self.publish(); copied = self.root/'copy.png'; copied.write_bytes(self.source.read_bytes())
        Path(saved['result']['path']).unlink()
        with self.assertRaisesRegex(FileNotFoundError, '抠像结果'): self.catalogue.lookup(copied)

    def test_jpeg_import_conversion_matches_saved_original_by_decoded_pixels(self):
        jpeg = self.root/'original.jpg'; Image.fromarray(self.pixels[:, :, :3]).save(jpeg, quality=92)
        saved = self.publish(jpeg)
        imported = import_images([str(jpeg)], self.root/'imported')['layers'][0]
        self.assertNotEqual(assets.fingerprint(jpeg)['sha256'], assets.fingerprint(imported['path'])['sha256'])
        self.assertEqual(self.catalogue.lookup(imported['path'])['id'], saved['id'])

    def test_atomic_catalogue_failure_keeps_previous_result(self):
        saved = self.publish(); before = self.catalogue.path.read_bytes()
        with patch.object(assets.Catalogue, 'load', side_effect=OSError('test failure')):
            with self.assertRaises(OSError): self.publish(opacity=10)
        self.assertEqual(self.catalogue.path.read_bytes(), before)
        self.assertEqual(len(list((self.catalogue.root/'assets').glob('*.png'))), 1)
        self.assertEqual(self.catalogue.lookup(self.source)['id'], saved['id'])

    def test_cancel_preserves_previous_and_cleans_own_temp(self):
        saved = self.publish(); cancel = threading.Event(); cancel.set()
        with self.assertRaises(MattingCancelled): render.save(self.source, {}, self.catalogue, cancel=cancel)
        self.assertEqual(self.catalogue.lookup(self.source)['id'], saved['id'])
        self.assertFalse(list((self.catalogue.root/'assets').glob('render-*')))

    def test_cannot_publish_stale_base_or_external_temp(self):
        base = render.infer(self.source, {})
        self.pixels[25, 25, 0] += 1; Image.fromarray(self.pixels).save(self.source)
        with self.assertRaisesRegex(ValueError, '已改变'): render.save(self.source, {}, self.catalogue, base=base)
        external = self.root/'external.png'; external.write_bytes(b'result')
        with self.assertRaisesRegex(ValueError, '管理目录'):
            self.catalogue.publish(self.source, external, {}, expected=assets.fingerprint(self.source))
        self.assertTrue(external.exists())

    def test_export_copy_retains_rgba_and_protects_all_sources_results(self):
        from app_plugins.builtin.batch_text_video.matting_export import export_copy
        saved = self.publish(); target = export_copy(self.catalogue, saved, self.root/'exported')
        self.assertEqual(target.suffix, '.png')
        self.assertEqual(target.read_bytes(), Path(saved['result']['path']).read_bytes())
        for path in (self.source, saved['result']['path'], self.catalogue.root/'other.png'):
            with self.subTest(path=path), self.assertRaises(ValueError): export_copy(self.catalogue, saved, path)

    def test_node_fixed_file_snapshot_retains_old_result_and_both_resource_refs(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import prepare_layer, build_layer
        saved = self.publish(); params = dict(layer=self.layer(source='file', source_file=str(self.source)))
        prepared = prepare_layer(None, params, None); self.publish(opacity=20)
        operation = build_layer({}, params, SimpleNamespace(node_prepared=prepared), media='visual')['operations'][0]
        layer = json.loads(dict(operation.options)['layer']); self.assertEqual(layer['path'], saved['result']['path'])
        self.assertIn(str(self.source.resolve()), [v.path for _, v in operation.assets])
        self.assertIn(saved['result']['path'], [v.path for _, v in operation.assets])

    def test_visual_inspector_saved_switch_round_trip(self):
        from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
        panel = VisualProperties(); self.addCleanup(panel.deleteLater)
        panel.set_layer(self.layer()); self.assertTrue(panel.controls['use_saved_matte'].isChecked())
        changes = []; panel.changed.connect(changes.append)
        panel.controls['use_saved_matte'].setChecked(False)
        self.assertFalse(changes[-1]['use_saved_matte'])

    def test_component_source_freezes_saved_file_and_verifies_original(self):
        from app_plugins.builtin.batch_text_video.workflow_component_source import prepare_source, build_source
        from app_plugins.builtin.batch_text_video.node_effects import TYPE_ID
        saved = self.publish()
        params = dict(source=self.layer(source='sequence', sequence_items=[dict(id='one', path=str(self.source))]))
        prepared = prepare_source(None, params, None); self.publish(opacity=0)
        c = SimpleNamespace(node_prepared=prepared, workflow_input=dict(type=TYPE_ID), node_id='source')
        result = build_source({}, params, c)
        self.assertEqual(result['content'], saved['result']['path'])
        self.assertEqual({r.path for r in c.component_source_refs}, {str(self.source.resolve()), saved['result']['path']})
        self.pixels[2, 2, 0] = 1; Image.fromarray(self.pixels).save(self.source)
        with self.assertRaises(ValueError): build_source({}, params, c)

    def test_node_pool_snapshot_retains_per_material_results(self):
        from app_plugins.builtin.batch_text_video.workflow_pools import prepare_pool, build_pool
        from app_plugins.builtin.batch_text_video.workflow_layers import build_layer
        saved = self.publish()
        services = SimpleNamespace(workflow_runtime_context=lambda _: dict(name='测试项目', data=dict(image_pool=[dict(id='one', path=str(self.source))])))
        prepared = prepare_pool(None, {'pool_id': 'builtin:image'}, services)
        pool = build_pool({}, {}, SimpleNamespace(node_prepared=prepared))['pool']
        self.publish(opacity=0)
        params = dict(layer=self.layer())
        operation = build_layer({'pool': pool}, params, SimpleNamespace(node_prepared={}), media='visual')['operations'][0]
        self.assertEqual(json.loads(dict(operation.options)['layer'])['path'], saved['result']['path'])
        params['layer']['use_saved_matte'] = False
        original = build_layer({'pool': pool}, params, SimpleNamespace(node_prepared={}), media='visual')['operations'][0]
        self.assertEqual(json.loads(dict(original.options)['layer'])['path'], str(self.source))

    def test_playback_key_changes_with_new_saved_result_and_disable(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        from app_plugins.builtin.batch_text_video.store import fresh_state
        state = fresh_state(); state['settings'].update(layers=[self.layer()], use_voice=False, music_enabled=False)
        job = dict(id='job', title='', body='')
        original = preview_key(job, state); self.publish(); first = preview_key(job, state)
        self.publish(opacity=0); second = preview_key(job, state)
        self.assertEqual(len({original, first, second}), 3)
        self.catalogue.set_enabled(self.source, False); self.assertEqual(preview_key(job, state), original)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_actual_export_playback_and_converted_nodes_reuse_same_rgba(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        from app_plugins.builtin.batch_text_video.store import fresh_state, DEFAULTS
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry, new_effect, prepare_bundle, EffectRuntime
        from model.node_workflow.library import WorkflowLibrary
        self.publish(); background = self.root/'background.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=240x160:r=10:d=0.4',
                        '-c:v', 'libx264', str(background)], check=True, capture_output=True)
        layer = self.layer(key_mode='green', width=100, height=100, use_saved_matte=True)
        registry = fallback_registry(); graph, _ = component_graph(layer, DEFAULTS, registry)
        library = WorkflowLibrary(self.root/'workflows'); stored = library.save(graph, 'matting test', registry)
        effect = new_effect(); effect['workflow_id'] = stored['workflow_id']
        services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
        outputs = []
        for variant in ('export', 'playback', 'node'):
            state = fresh_state(); state['settings'].update(aspect='source', darkness=0, music_enabled=False, use_voice=False,
                ffmpeg_path=FFMPEG, output_dir=str(self.root/'out'), layers=[effect if variant == 'node' else layer])
            job = dict(id='job', path=str(background), name=variant, title='', body='', task_dir=''); state['jobs'] = [job]
            runtime = None
            if variant == 'node':
                bundle, registry, _ = prepare_bundle(state, ['job'], services); runtime = EffectRuntime(bundle, registry)
            with patch.object(render, 'Predictor', side_effect=AssertionError('exports must not infer')):
                output, _ = render_static_text(job, state, self.root/'private-render', node_effects=runtime, preview=variant == 'playback')
            decoded = subprocess.run([FFMPEG, '-v', 'error', '-i', str(output), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], check=True, capture_output=True)
            outputs.append(np.frombuffer(decoded.stdout, np.uint8).reshape(160, 240, 3))
        for other in outputs[1:]: self.assertLess(float(abs(outputs[0].astype(float)-other).mean()), 1.5)
        self.assertGreater(outputs[0][5, 5, 2], 240)
        np.testing.assert_allclose(outputs[0][80, 120], [50, 90, 162], atol=8)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_rotated_video_decodes_and_saves_at_oriented_dimensions(self):
        base, rotated = self.root/'base.mp4', self.root/'rotated.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x48:r=5:d=0.4', '-c:v', 'libx264', str(base)], check=True, capture_output=True)
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-display_rotation:v:0', '90', '-i', str(base), '-c', 'copy', str(rotated)], check=True, capture_output=True)
        self.assertEqual(render.load_source(rotated, ffmpeg=FFMPEG)['source'].shape, (64, 48, 4))
        saved = render.save(rotated, {'model': 'rvm'}, self.catalogue, ffmpeg=FFMPEG)
        self.assertEqual(render.load_source(saved['result']['path'], ffmpeg=FFMPEG)['source'].shape, (64, 48, 4))

    def test_cancel_after_atomic_save_still_reports_committed_record(self):
        from app_plugins.builtin.batch_text_video.matting_ui import MattingWorker
        request = dict(serial=1, action='save', path=str(self.source), values=normalize())
        worker = MattingWorker(request, self.catalogue); results = []; worker.ready.connect(results.append)
        real_save = render.save
        def committed(*args, **kwargs):
            record = real_save(*args, **kwargs); worker.cancel.set(); return record
        with patch('app_plugins.builtin.batch_text_video.matting_ui.save', side_effect=committed): worker.run()
        self.assertTrue(results[0].get('record')); self.assertFalse(results[0].get('cancelled'))

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_video_preserves_frame_alpha_timing_and_recurrent_predictor(self):
        video = self.root/'source.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x48:r=5:d=0.6', '-c:v', 'libx264', str(video)], check=True, capture_output=True)
        record = render.save(video, {'model': 'rvm'}, self.catalogue, ffmpeg=FFMPEG)
        self.assertEqual(render.load_source(video, ffmpeg=FFMPEG)['source'].shape, (48, 64, 4))
        self.assertEqual(render.load_source(record['result']['path'], ffmpeg=FFMPEG)['source'].shape, (48, 64, 4))
        self.assertEqual(record['model_info']['frames'], 3)
        result = subprocess.run([FFMPEG, '-v', 'error', '-i', record['result']['path'], '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'], check=True, capture_output=True)
        frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, 48, 64, 4)
        self.assertEqual(frames[:, 0, 0, 3].tolist(), [0, 0, 0])
        self.assertEqual(frames[:, 20, 20, 3].tolist(), [128, 255, 128])
        self.assertEqual(FakePredictor.calls, 3)
        self.assertFalse(list((self.catalogue.root/'assets').glob('*.log')))

    def wait(self, predicate, seconds=10):
        end = time.monotonic()+seconds
        while not predicate() and time.monotonic() < end: self.app.processEvents(); time.sleep(.005)
        self.assertTrue(predicate(), 'Qt worker timed out')

    def test_dialog_async_generate_refine_save_reopen_parameters_and_invalid_guard(self):
        from app_plugins.builtin.batch_text_video.matting_ui import MattingDialog
        dialog = MattingDialog([str(self.source)], catalogue=self.catalogue)
        self.addCleanup(dialog.deleteLater); self.wait(lambda: dialog.worker is None)
        dialog.submit('infer'); self.wait(lambda: dialog.worker is None)
        self.assertIsNotNone(dialog.base); self.assertIn('CPU', dialog.status.text())
        dialog.controls['black_clip'].setValue(10); dialog.controls['blur'].setValue(1)
        self.wait(lambda: not dialog.timer.isActive() and dialog.worker is None)
        dialog.submit('save'); self.wait(lambda: dialog.worker is None)
        self.assertEqual(self.catalogue.lookup(self.source)['parameters']['black_clip'], 10)
        dialog.controls['white_clip'].setValue(5)
        self.assertFalse(dialog.save_button.isEnabled()); serial = dialog._serial; dialog.submit('save'); self.assertEqual(serial, dialog._serial)
        dialog.close()
        reopened = MattingDialog([str(self.source)], catalogue=self.catalogue); self.addCleanup(reopened.deleteLater)
        self.wait(lambda: reopened.worker is None)
        self.assertEqual(reopened.controls['black_clip'].value(), 10)
        self.assertFalse(reopened.canvas.cutout.isNull()); reopened.close()

    def test_main_menu_current_trial_original_and_saved_callback(self):
        from app_plugins.builtin.batch_text_video.matting_controller import MattingController
        class Owner(QtWidgets.QWidget, MattingController): pass
        owner = Owner(); self.addCleanup(owner.deleteLater); owner.menu_bar = QtWidgets.QMenuBar(owner)
        owner.ffmpeg = QtWidgets.QLineEdit(); owner.init_matting_menu()
        self.assertEqual(owner.menu_bar.actions()[0].text(), '抠像')
        dialog = owner.open_matting([str(self.source)])
        self.wait(lambda: dialog.worker is None)
        self.assertIs(owner.open_matting([str(self.source)]), dialog)
        dialog.close()

    def test_busy_escape_cancels_and_workspace_can_reopen(self):
        from app_plugins.builtin.batch_text_video.matting_ui import MattingDialog
        dialog = MattingDialog([str(self.source)], catalogue=self.catalogue)
        self.addCleanup(dialog.deleteLater); dialog.show(); dialog.reject()
        self.assertTrue(dialog.worker.cancel.is_set())
        self.wait(lambda: dialog.worker is None)
        self.assertFalse(dialog.isVisible())
        dialog.show(); dialog.submit('infer'); self.wait(lambda: dialog.worker is None)
        self.assertTrue(dialog.isVisible()); self.assertIsNotNone(dialog.base); dialog.close()


class MattingHostLifecycleTests(unittest.TestCase):
    from test_batch_review_regressions import BatchReviewRegressions as Fixture
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    wait = Fixture.wait

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def test_plugin_waits_for_matting_worker_and_cancels_only_local_work(self):
        path = Path(self.temp.name)/'picture.png'; Image.new('RGB', (64, 48), 'red').save(path)
        studio = self.dialog.open_matting([str(path)])
        self.assertIsNotNone(studio.worker)
        self.assertFalse(self.plugin.can_close()[0]); self.assertTrue(studio.worker.cancel.is_set())
        self.wait(lambda: studio.worker is None)
        self.assertTrue(self.plugin.can_close()[0]); studio.close()
