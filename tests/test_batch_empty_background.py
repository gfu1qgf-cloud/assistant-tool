"""A background library is optional; ordinary visual controls own the composition."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import engine, background_visual
from app_plugins.builtin.batch_text_video.audio_sources import resolve_voice
from app_plugins.builtin.batch_text_video.background import validate_batch_outputs
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.pools import pairing_plan, apply_pairs, new_copy
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, PreviewWorker
from app_plugins.builtin.batch_text_video.playback import PlaybackWorker, preview_key
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FFMPEG = os.environ.get('NODE_FFMPEG', '')
PREFIX = 'app_plugins.builtin.batch_text_video.'


class EmptyBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def state(self, directory):
        state = fresh_state()
        image = QtGui.QImage(320, 180, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#305080'))
        painter = QtGui.QPainter(image)
        painter.fillRect(100, 50, 120, 80, QtGui.QColor('#f0c030')); painter.end()
        path = Path(directory)/'photo.png'; self.assertTrue(image.save(str(path)))
        state['settings'].update(ffmpeg_path=FFMPEG, music_enabled=False, darkness=0, background_blur=0,
            output_dir=str(directory), overwrite=True, aspect='landscape', unvoiced_duration_seconds=1.2,
            layers=normalize_layers([dict(id='photo', kind='image', path=str(path), x=50, y=50,
                width=100, height=100, visual_animation='breathe', animation_strength=30, animation_period=1.2)]))
        return state

    def job(self, **values):
        return dict(id='composition', path='', name='only-controls', title='', body='', status='待生成', **values)

    def ffmpeg(self, *args):
        result = subprocess.run([FFMPEG, '-v', 'error', '-nostdin', '-y', *map(str, args)],
            capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def test_empty_pool_can_pair_every_copy_and_repeat_without_queue_duplicates(self):
        state = fresh_state(); state['copy_pool'] = [new_copy(), new_copy()]
        pairs = pairing_plan(state['copy_pool'], [])
        self.assertEqual([p['path'] for p in pairs], ['', ''])
        self.assertEqual(apply_pairs(state, pairs), (2, 0))
        self.assertEqual(apply_pairs(state, pairs), (0, 0))
        self.assertEqual([j['pair_key'][1] for j in state['jobs']], ['', ''])
        with self.assertRaises(ValueError): pairing_plan([], [], 'invalid')
        self.assertEqual(pairing_plan([], [], 'videos'), [{'copy': None, 'path': ''}])

    def test_empty_and_explicit_none_global_sources_do_not_resurrect_old_background(self):
        state = fresh_state(); job = self.job(); job['path'] = 'old.mp4'
        for source in ('none', 'builtin:background', 'photos'):
            state['resource_pools'] = [dict(id='photos', name='Photos', kind='image', items=[], next_id='')]
            state['settings']['background_pool_id'] = source
            before = copy.deepcopy((job, state))
            self.assertEqual(background_visual.background_job(job, state)['path'], '')
            self.assertEqual((job, state), before)
        state['settings']['background_pool_id'] = 'missing-pool'
        with self.assertRaises(ValueError): background_visual.background_job(job, state)

    def test_quick_export_without_source_has_stable_identity_and_name(self):
        state = fresh_state(); entry = {'path': '', 'title': '', 'body': ''}
        queued, identifier = prepare_current_export(state, entry)
        queued_again, same_id = prepare_current_export(queued, entry)
        self.assertEqual(identifier, same_id); self.assertEqual(len(queued_again['jobs']), 1)
        self.assertEqual(queued_again['jobs'][0]['pair_key'], ['', ''])
        self.assertEqual(queued_again['jobs'][0]['name'], '控件合成')
        with self.assertRaises(ValueError): prepare_current_export(state, dict(entry, path='missing.mp4'))

    def test_no_implicit_working_directory_output_or_voice_discovery(self):
        state = fresh_state(); job = self.job(); state['jobs'] = [job]
        with self.assertRaisesRegex(ValueError, '输出文件夹'): validate_batch_outputs(state, [job['id']])
        state['settings']['use_voice'] = True
        with patch(PREFIX+'matching.folder_entry', side_effect=AssertionError('Must not inspect cwd')):
            self.assertEqual(resolve_voice(job, state, discover=True), '')
        with tempfile.TemporaryDirectory() as directory:
            state['settings']['output_dir'] = directory
            validate_batch_outputs(state, [job['id']])
            duplicate = dict(job, id='second'); state['jobs'].append(duplicate)
            with self.assertRaisesRegex(ValueError, '同一成品'): validate_batch_outputs(state, [job['id'], 'second'])

    def test_pairing_dialog_has_explicit_empty_choice_and_can_generate_without_copy(self):
        dialog = PairingDialog([], [])
        try:
            self.assertEqual(dialog.pairs[0]['path'], '')
            self.assertEqual(dialog.table.cellWidget(0, 1).currentData(), '')
            self.assertIn('无底层背景', dialog.table.cellWidget(0, 1).currentText())
            self.assertTrue(dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled())
        finally: dialog.deleteLater()

    def test_blank_preview_has_finite_animation_clock_and_explicit_mode_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory); state['jobs'] = [self.job()]
            store = Store(Path(directory)/'private'); store.save(state, 0)
            with patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, '_start_overlay'):
                dialog = BatchTextVideoDialog(store=store)
                try:
                    dialog._show_job(state['jobs'][0]); dialog.update_preview()
                    self.assertEqual(dialog.animation_timeline.duration, 1.2)
                    self.assertIn('无底层背景', dialog.duration_status.text())
                    combo = dialog.controls['background_pool_id']
                    self.assertGreaterEqual(combo.findData('none'), 0)
                    combo.setCurrentIndex(combo.findData('none')); dialog.edit_timer.stop(); dialog.persist()
                    self.assertEqual(store.load()['settings']['background_pool_id'], 'none')
                finally: dialog.edit_timer.stop(); dialog.close(); dialog.deleteLater()

    @unittest.skipUnless(FFMPEG, 'Real FFmpeg required')
    def test_real_export_has_only_image_animation_at_100_percent_no_intermediate_video(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory); job = self.job(); state['jobs'] = [job]
            before = copy.deepcopy(state); original = Path(state['settings']['layers'][0]['path']).read_bytes()
            # Small canvas keeps the regression cheap while using the real compositor/encoder.
            with patch.object(engine, 'output_size', return_value=(320, 180)):
                with engine.prepare_static_text(job, state, Path(directory)/'private') as prepared:
                    self.assertIn('lavfi', prepared.args)
                    self.assertFalse(any(str(a).endswith('.mov') for a in prepared.args))
                    prepared.encode(); output = prepared.publish()
            info = engine.probe(output, str(Path(FFMPEG).with_name('ffprobe.exe')))
            self.assertAlmostEqual(info['duration'], 1.2, delta=.05); self.assertFalse(info['audio'])
            frames = np.frombuffer(self.ffmpeg('-i', output, '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'), np.uint8).reshape(-1, 180, 320, 3)
            self.assertEqual(len(frames), 36)
            self.assertGreater(np.abs(frames[0].astype(int)-frames[18]).mean(), 6)
            self.assertGreater(frames.max(axis=3).min(), 25, '100% visual must fill this canvas at every frame')
            self.assertEqual(Path(state['settings']['layers'][0]['path']).read_bytes(), original)
            self.assertEqual(state, before); self.assertFalse(list(Path(directory).glob('.text-video-*')))

    @unittest.skipUnless(FFMPEG, 'Real FFmpeg required')
    def test_voice_clock_poster_and_playback_work_without_background(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory); voice = Path(directory)/'voice.wav'
            self.ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.6', voice)
            state['settings']['use_voice'] = True
            job = self.job(voice_path=str(voice), voice_override=True); state['jobs'] = [job]
            results = []; worker = PreviewWorker('', FFMPEG, voice_path=str(voice)); worker.ready.connect(results.append); worker.run()
            self.assertNotIn('error', results[0]); self.assertIsNone(results[0]['media'])
            self.assertAlmostEqual(results[0]['voice_duration'], 1.6, delta=.02)
            before = copy.deepcopy((state, job))
            results = []; worker = PlaybackWorker(job, state, Path(directory)/'private', preview_key(job, state))
            worker.ready.connect(results.append)
            with patch.object(engine, 'output_size', return_value=(320, 180)): worker.run()
            self.assertNotIn('error', results[0]); self.assertAlmostEqual(results[0]['duration'], 1.6, delta=.05)
            self.assertEqual((state, job), before)

    @unittest.skipUnless(FFMPEG, 'Real FFmpeg required')
    def test_batch_without_backgrounds_commits_success_and_advances_only_then(self):
        import threading
        from app_plugins.builtin.batch_text_video.background import execute_batch
        from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory)
            image = state['settings']['layers'][0]
            state['image_pool'] = [dict(id='a', path=image['path']), dict(id='b', path=image['path'])]
            image.update(source='pool', sequence_cursor=0)
            state['settings']['layers'] = bind_pool_layers(state['settings']['layers'], state)
            apply_pairs(state, pairing_plan([new_copy(), new_copy()], []))
            # Make a later failure explicit. A deleted file must not become an empty canvas.
            state['jobs'][1]['path'] = str(Path(directory)/'deleted.mp4')
            store = Store(Path(directory)/'private'); state = store.save(state, 0)
            identifiers = [job['id'] for job in state['jobs']]
            with patch.object(engine, 'output_size', return_value=(320, 180)):
                result, _ = execute_batch(store, state, identifiers, threading.Event(), lambda *_: None,
                                         lambda *_: None, lambda *_: None)
            self.assertEqual((result['completed'], result['failed']), (1, 1), result)
            saved = store.load()
            self.assertEqual(saved['settings']['layers'][0]['sequence_cursor'], 1)
            self.assertEqual(saved['jobs'][0]['status'], '已完成')
            self.assertTrue(Path(saved['jobs'][0]['output']).is_file())
            self.assertEqual(saved['jobs'][1]['status'], '需处理')
            self.assertIn('背景素材不存在', saved['jobs'][1]['error'])

    @unittest.skipUnless(FFMPEG, 'Real FFmpeg required')
    def test_node_effects_use_generated_canvas_without_creating_a_source_video(self):
        from app_plugins.builtin.batch_text_video import node_effects as fx
        from app_plugins.builtin.batch_text_video.workflow_layers import layer_node
        from model.node_workflow.graph import empty_graph, new_node
        from model.node_workflow.library import WorkflowLibrary
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory); job = self.job(); state['jobs'] = [job]
            registry = fx.fallback_registry(); graph = empty_graph()
            graph.update(workflow_type=fx.TYPE_ID, workflow_type_version=1, execution_mode='line')
            entry = new_node(registry.definitions[fx.INPUT_KIND]); output = new_node(registry.definitions[fx.OUTPUT_KIND])
            node = new_node(layer_node('image'))
            node['params']['layer'] = copy.deepcopy(state['settings']['layers'][0])
            graph.update(nodes=[entry, node, output], output_node=output['id'], connections=[[node['id'], 'operations', output['id'], 'operations']])
            library = WorkflowLibrary(Path(directory)/'workflows')
            record = library.save(graph, 'Only image', registry)
            effect = fx.new_effect(); effect['workflow_id'] = record['workflow_id']; state['settings']['layers'] = normalize_layers([effect])
            from types import SimpleNamespace
            bundle, registry, _ = fx.prepare_bundle(state, [job['id']], SimpleNamespace(workflow_library=library, available_nodes=lambda: registry))
            runtime = fx.EffectRuntime(bundle, registry)
            with patch.object(engine, 'output_size', return_value=(320, 180)):
                target, _ = engine.render_static_text(job, state, Path(directory)/'private', node_effects=runtime)
            self.assertTrue(Path(target).is_file()); runtime.verify()
            self.assertFalse(any(p.name.startswith('background') and p.suffix in {'.mp4', '.mov'} for p in Path(directory).rglob('*')))


if __name__ == '__main__': unittest.main()
