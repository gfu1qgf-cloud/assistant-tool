"""Regression: a task title must not be paired with a rotated task body."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video import node_effects as fx
from app_plugins.builtin.batch_text_video.background import execute_batch
from app_plugins.builtin.batch_text_video.component_nodes import component_graph, SOURCE_KIND
from app_plugins.builtin.batch_text_video.component_ui import ContentSourceEditor
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources
from app_plugins.builtin.batch_text_video.copy_binding import bind_task_copy
from app_plugins.builtin.batch_text_video.layers import DEFAULT_LAYERS, normalize_layers
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from model.node_workflow.library import WorkflowLibrary

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def source_layer(**values):
    layer = new_text_component()
    layer.update(source='pool', pool_field='body', sequence_items=[
        {'id': f'copy-{i}', 'text': f'BODY-{i}'} for i in range(5)], sequence_cursor=3,
        font='Segoe UI', font_min=36, font_max=36, width=90, height=75)
    layer.update(values)
    return normalize_layers([layer])[0]


def job(index=0):
    return dict(id=f'job-{index}', copy_id=f'copy-{index}', title=f'TITLE-{index}',
                body=f'BODY-{index}', task_id=str(60000+index), requires_title=True)


class CopyBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_native_task_binding_ignores_stale_pool_cursor_and_never_advances(self):
        layer = source_layer()
        for index in (0, 4, 1, 0):
            with self.subTest(index=index):
                item = job(index)
                resolved, steps = resolve_sources([layer], item['title'], item['body'], job=item)
                self.assertEqual(resolved[0]['text'], item['body'])
                self.assertEqual(resolved[0]['source'], 'fixed')
                self.assertEqual(steps, [])
        self.assertEqual(layer['sequence_cursor'], 3)

    def test_title_body_and_full_are_from_one_frozen_copy(self):
        item = job(2)
        for field, expected in (('title', 'TITLE-2'), ('body', 'BODY-2'), ('full', 'TITLE-2\nBODY-2')):
            layer = source_layer(pool_field=field)
            self.assertEqual(bind_task_copy(layer, item)['text'], expected)

    def test_reimported_ids_reorder_and_removed_library_do_not_replace_queue_copy(self):
        item = job(0)
        for entries in ([], [dict(id='new-3', title='WRONG', body='WRONG')]):
            state = fresh_state(); state['copy_pool'] = entries
            layer = bind_pool_layers([source_layer()], state)[0]
            resolved, steps = resolve_sources([layer], item['title'], item['body'], job=item)
            self.assertEqual(resolved[0]['text'], 'BODY-0')
            self.assertEqual(steps, [])

    def test_empty_current_body_is_not_replaced_with_someone_elses_body(self):
        item = dict(job(), body='')
        self.assertEqual(bind_task_copy(source_layer(), item)['text'], '')

    def test_requires_title_import_is_bound_without_copy_id(self):
        item = job(); item.pop('copy_id')
        self.assertEqual(bind_task_copy(source_layer(), item)['text'], 'BODY-0')

    def test_explicit_rotation_custom_pool_and_unpaired_video_keep_rotation(self):
        for layer, item in ((source_layer(copy_binding='rotate'), job()),
                            (source_layer(pool_id='custom'), job()),
                            (source_layer(), {'title':'', 'body':''})):
            self.assertIsNone(bind_task_copy(layer, item))
            self.assertEqual(resolve_sources([layer], job=item)[0][0]['text'], 'BODY-3')

    def test_explicit_other_profile_is_not_rebound_to_current_task(self):
        self.assertIsNone(bind_task_copy(source_layer(), dict(job(), copy_profile_id='current'), source_profile='other'))

    def test_custom_sequence_and_fixed_file_are_never_rebound(self):
        for source in ('sequence', 'fixed', 'file'):
            self.assertIsNone(bind_task_copy(source_layer(source=source), job()))

    def test_old_independent_source_signatures_remain_compatible(self):
        import hashlib
        from app_plugins.builtin.batch_text_video.workflow_component_source import normalized, signature
        value = normalized(source_layer(source='sequence'))
        legacy = dict(value); legacy.pop('copy_binding')
        old_token = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(signature(value), old_token)

    def test_source_editor_preserves_explicit_choice_and_is_not_a_hidden_switch(self):
        editor = ContentSourceEditor()
        try:
            layer = source_layer(); editor.set_layer(layer)
            self.assertEqual(editor.copy_binding.currentData(), 'task')
            self.assertIn('同一条', editor.note.text())
            changed = []; editor.changed.connect(changed.append)
            editor.copy_binding.setCurrentIndex(editor.copy_binding.findData('rotate'))
            self.assertEqual(changed[-1]['copy_binding'], 'rotate')
            editor.set_layer(changed[-1])
            self.assertEqual(editor.copy_binding.currentData(), 'rotate')
        finally:
            editor.deleteLater()


class ConvertedCopyBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root/'source.mp4'; self.video.write_bytes(b'synthetic video identity')
        self.store = Store(self.root/'store')
        self.registry = fx.fallback_registry()
        self.library = WorkflowLibrary(self.root/'workflows')
        self.services = SimpleNamespace(available_nodes=lambda:self.registry, workflow_library=self.library)
        self.state = fresh_state()
        self.state['copy_pool'] = [dict(id=f'copy-{i}', title=f'TITLE-{i}', body=f'BODY-{i}') for i in range(5)]
        self.state['jobs'] = [dict(job(i), path=str(self.video), name=f'export-{i}',
            task_dir=str(self.root/f'task-{i}'), status='待生成') for i in range(5)]
        for item in self.state['jobs']: Path(item['task_dir']).mkdir()
        self.state = self.store.save(self.state, 0)

    def setup_graph(self, **values):
        layer = source_layer(**values)
        graph, effect = component_graph(layer, self.state['settings'], self.registry)
        graph = self.library.save(graph, 'synthetic body', self.registry)
        effect['workflow_id'] = graph['workflow_id']
        adapter = next((n for n in graph['nodes'] if n['kind'] == SOURCE_KIND), None)
        # Reproduce the state left by an earlier export: cursor 3, old item IDs.
        from app_plugins.builtin.batch_text_video.workflow_component_source import normalized, signature
        source = adapter['params']['source'] if adapter else layer
        effect['source_rotations'] = {adapter['id'] if adapter else 'obsolete-source': dict(signature=signature(normalized(source)),
                                                        cursor=3, next_id='copy-3')}
        self.state['settings']['layers'] = normalize_layers([dict(v, enabled=False) for v in DEFAULT_LAYERS]+[effect])
        return effect, graph, adapter

    def evaluate(self, bundle, effect, item, runtime=None):
        runtime = runtime or fx.EffectRuntime(bundle, self.registry)
        info = dict(duration=1., audio=False, video=dict(width=320, height=568))
        with runtime.evaluated(item, effect, self.root, info, '', FFMPEG) as (_, _, operations):
            return json.loads(dict(operations[0].options)['layer'])['text']

    def test_converted_pool_keeps_each_jobs_title_body_binding_despite_old_rotations(self):
        effect, graph, adapter = self.setup_graph()
        before = copy.deepcopy(self.state)
        bundle, _, _ = fx.prepare_bundle(self.state, [j['id'] for j in self.state['jobs']], self.services)
        runtime = fx.EffectRuntime(bundle, self.registry)
        for item in self.state['jobs']:
            runtime.start_job(item)
            self.assertEqual(self.evaluate(bundle, effect, item, runtime), item['body'])
            self.assertEqual(runtime.sequence_plan, [])
            self.assertIsNone(adapter)
            entry = next(n for n in graph['nodes'] if n['kind'] == fx.INPUT_KIND)
            text = next(n for n in graph['nodes'] if n['kind'] == 'batch_text_video.text_layer')
            self.assertIn([entry['id'], 'body', text['id'], 'content'], graph['connections'])
            self.assertFalse(any('task_copy_binding' in value for value in
                bundle['jobs'][item['id']][effect['workflow_id']]['prepared'].values()))
        self.assertEqual(self.state, before)
        self.assertEqual(self.library.load(graph['workflow_id'], self.registry), graph)

    def test_selected_out_of_order_and_retries_never_consume_someone_elses_body(self):
        effect, _, _ = self.setup_graph()
        ids = ['job-4', 'job-0', 'job-2']
        bundle, _, _ = fx.prepare_bundle(self.state, ids, self.services)
        for index in (4, 0, 2, 0, 4):
            self.assertEqual(self.evaluate(bundle, effect, self.state['jobs'][index]), f'BODY-{index}')

    def test_native_and_converted_sources_resolve_to_identical_copy(self):
        layer = source_layer()
        effect, _, _ = self.setup_graph()
        bundle, _, _ = fx.prepare_bundle(self.state, ['job-0'], self.services)
        item = self.state['jobs'][0]
        native = resolve_sources([layer], item['title'], item['body'], job=item)[0][0]['text']
        self.assertEqual(self.evaluate(bundle, effect, item), native)

    def test_removed_copy_library_does_not_break_already_paired_snapshot(self):
        effect, _, _ = self.setup_graph()
        self.state['copy_pool'] = []
        self.state = self.store.save(self.state, self.state['revision'])
        bundle, _, _ = fx.prepare_bundle(self.state, ['job-0'], self.services)
        self.assertEqual(self.evaluate(bundle, effect, self.state['jobs'][0]), 'BODY-0')

    def test_explicit_node_rotation_keeps_original_independent_pool_behaviour(self):
        effect, _, _ = self.setup_graph(copy_binding='rotate')
        bundle, _, _ = fx.prepare_bundle(self.state, ['job-0'], self.services)
        runtime = fx.EffectRuntime(bundle, self.registry)
        self.assertEqual(self.evaluate(bundle, effect, self.state['jobs'][0], runtime), 'BODY-3')
        self.assertEqual(len(runtime.sequence_plan), 1)

    def test_export_failure_retry_and_completed_rows_keep_pairing(self):
        effect, _, _ = self.setup_graph()
        bundle, _, _ = fx.prepare_bundle(self.state, [j['id'] for j in self.state['jobs']], self.services)
        effects = fx.EffectRuntime(bundle, self.registry)
        seen = []
        def renderer(item, state, directory, cancel, progress, message, **kw):
            runtime = kw['node_effects']; runtime.start_job(item)
            actual_effect = next(v for v in state['settings']['layers'] if v['kind'] == 'node_effect')
            text = self.evaluate(bundle, actual_effect, item, runtime)
            seen.append((item['id'], text))
            if item['id'] == 'job-1': raise RuntimeError('synthetic failure')
            output = self.root/(item['id']+'.mp4'); output.write_bytes(b'synthetic completed marker')
            return output, None
        result, state = execute_batch(self.store, self.state, [j['id'] for j in self.state['jobs']],
            threading.Event(), lambda *_:None, lambda *_:None, lambda *_:None,
            {'static_text':renderer}, node_effects=effects)
        self.assertEqual((result['completed'], result['failed']), (4,1))
        self.assertEqual(seen, [(f'job-{i}', f'BODY-{i}') for i in range(5)])
        fresh, _, _ = fx.prepare_bundle(state, ['job-1'], self.services)
        self.assertEqual(self.evaluate(fresh, effect, state['jobs'][1]), 'BODY-1')

    @unittest.skipUnless(FFMPEG, 'FFmpeg not configured')
    def test_real_export_compiles_each_bound_body_not_old_pool_cursor(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        result = subprocess.run([FFMPEG,'-y','-v','error','-f','lavfi','-i',
            'color=c=0x345e70:s=320x568:r=10:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(self.video)],
            capture_output=True, timeout=60, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode, 0, result.stderr)
        effect, _, _ = self.setup_graph()
        self.state['settings'].update(ffmpeg_path=FFMPEG, music_enabled=False, use_voice=False)
        bundle, _, _ = fx.prepare_bundle(self.state, ['job-0','job-4'], self.services)
        runtime = fx.EffectRuntime(bundle, self.registry)
        from dataclasses import replace
        definition = self.registry.definitions['batch_text_video.text_layer']
        compiler = definition.compile_operation
        compiled = []
        def compile_checked(operation, *args, **kw):
            if operation.name == 'batch_text_video.text_layer':
                compiled.append(json.loads(dict(operation.options)['layer'])['text'])
            return compiler(operation, *args, **kw)
        self.registry.definitions[definition.kind] = replace(definition, compile_operation=compile_checked)
        try:
            for index in (0,4):
                output, _ = render_static_text(self.state['jobs'][index], self.state, self.store.directory,
                    threading.Event(), node_effects=runtime)
                self.assertTrue(output.is_file())
        finally:
            self.registry.definitions[definition.kind] = definition
        self.assertEqual(compiled, ['BODY-0', 'BODY-4'])


if __name__ == '__main__':
    unittest.main()
