"""Plugin contracts, one-pass composition, delivery identity and simulation."""
import copy
from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.graph import NodeRegistry, register_core, new_node, validate, evaluate_graph
from model.node_workflow.contracts import ResourceRef, RenderPlan, FilterOperation, VideoTimeline, VideoSection
from model.node_workflow.video_stream import VideoStream, EncodingSettings, normalize_plan
from model.node_workflow.workflow_types import WorkflowTypeDefinition, preview_available
from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.tasks import prepare_job
from model.node_workflow.preview import run_preview
from model.node_workflow.render import render_plan, probe
from app_plugins.builtin.task_delivery_workflow import register, example, filename, TYPE_ID, CONFIG_KEY, process_file, prepare
from app_plugins.builtin.batch_text_video.workflow_nodes import canvas_nodes

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def context():
    return TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)


def registry():
    value = NodeRegistry(); register_core(value)
    register(SimpleNamespace(register_node=lambda n: value.register('task_delivery', n),
        register_workflow_type=lambda t: value.register_workflow_type('task_delivery', t)))
    for definition in canvas_nodes(): value.register('batch_text_video', definition)
    return value


class PostprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.registry = registry()
        self.graph = example(self.registry)
        self.source = self.root / '管理员ABC-1011-01-原始任务.mp4'
        self.source.write_bytes(b'synthetic-only-unit-tests')
        self.ref = ResourceRef.capture(self.source, 'video')
        self.c = context(); self.c.workflow_input = {'video': self.ref, 'path': str(self.source)}
        self.c.prepared = {}; self.c.task = None

    def test_plugin_type_registration_namespace_duplicates_and_disable(self):
        self.assertIn(TYPE_ID, self.registry.workflow_types)
        with self.assertRaises(ValueError):
            self.registry.register_workflow_type('task_delivery', self.registry.workflow_types[TYPE_ID])
        with self.assertRaises(ValueError):
            self.registry.register_workflow_type('other', self.registry.workflow_types[TYPE_ID])
        disabled = self.registry.subset(lambda owner: owner != 'task_delivery')
        errors = validate(self.graph, disabled)[0]
        self.assertTrue(any('提供者不可用' in text for _, text in errors))
        self.assertNotIn(TYPE_ID, disabled.workflow_types)

    def test_plugin_workflow_without_preview_can_register_and_reports_preview_unavailable(self):
        value = NodeRegistry(); register_core(value)
        for kind, definition in self.registry.definitions.items():
            if self.registry.owners[kind] == 'task_delivery':
                value.register('task_delivery', definition)
        definition = replace(self.registry.workflow_types[TYPE_ID], preview_factory=None,
                             preview_plan_factory=None, preview_task_factory=None)
        value.register_workflow_type('task_delivery', definition)
        graph = example(value)
        self.assertEqual(validate(graph, value)[0], [])
        self.assertFalse(preview_available(graph, value))
        from PYUI.node_graph_canvas import NodeScene
        preview_node = new_node(value.definitions['core.preview'])
        graph['nodes'].append(preview_node)
        scene = NodeScene(graph, value)
        try:
            self.assertFalse(scene.nodes[preview_node['id']].can_preview())
        finally:
            scene.refresh_timer.stop()
            scene.deleteLater()
        from PYUI.node_workspace import NodeWorkspace
        owner = SimpleNamespace(graph=graph, registry=value)
        self.assertFalse(NodeWorkspace.can_preview_node(owner, preview_node))
        with self.assertRaisesRegex(ValueError, '尚未提供预览模拟'):
            run_preview({'graph': graph}, self.c, registry=value)

    def test_failed_plugin_registration_rolls_back_types_and_can_retry(self):
        from app_plugins.host import PluginHost
        host = PluginHost(SimpleNamespace(load_config=lambda: {}))
        original = set(host.node_registry.definitions)
        def broken(context):
            register(context)
            raise RuntimeError('synthetic registration failure')
        with self.assertRaisesRegex(RuntimeError, 'registration failure'):
            host.install(SimpleNamespace(plugin_id='task_delivery', register=broken))
        self.assertEqual(set(host.node_registry.definitions), original)
        self.assertEqual(host.node_registry.workflow_types, {})
        self.assertEqual(host.node_registry.workflow_type_owners, {})
        host.install(SimpleNamespace(plugin_id='task_delivery', register=register))
        self.assertIn(TYPE_ID, host.available_nodes().workflow_types)

    def test_special_entry_output_and_version_required(self):
        self.assertFalse(validate(self.graph, self.registry)[0])
        value = copy.deepcopy(self.graph); value['workflow_type_version'] = 3
        self.assertTrue(any('版本' in text for _, text in validate(value, self.registry)[0]))
        value = copy.deepcopy(self.graph); value['execution_mode'] = 'line'
        self.assertTrue(any('执行范围' in text for _, text in validate(value, self.registry)[0]))
        value = copy.deepcopy(self.graph); value.pop('workflow_type'); value.pop('workflow_type_version')
        self.assertTrue(validate(value, self.registry)[0])
        value = copy.deepcopy(self.graph); value['nodes'].append(new_node(self.registry.definitions['task_delivery.video_input']))
        self.assertTrue(any('只能有一个' in text for _, text in validate(value, self.registry)[0]))
        value = copy.deepcopy(self.graph); value['connections'].pop()
        self.assertTrue(any('视频流' in text for _, text in validate(value, self.registry)[0]))

    def test_evaluation_returns_deferred_video_and_basename_not_a_file(self):
        values = evaluate_graph(self.graph, self.registry, self.c)
        self.assertIsInstance(values['video'], VideoStream)
        self.assertEqual(values['video'].source, self.ref)
        self.assertEqual(set(values), {'video'})
        self.assertEqual(values['video'].encoding.crf, 30)
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_path_condition_controls_only_selected_person_compression(self):
        entry, compress, _output = self.graph['nodes']
        match = new_node(self.registry.definitions['core.path_match'])
        match['params'].update(pattern='管理员ABC', subject='name')
        self.graph['nodes'].append(match)
        self.graph['connections'].extend([[entry['id'], 'path', match['id'], 'path'],
                                         [match['id'], 'boolean', compress['id'], 'enabled']])
        self.assertFalse(validate(self.graph, self.registry)[0])
        result = evaluate_graph(self.graph, self.registry, self.c)
        self.assertEqual(result['video'].encoding.crf, 30)
        self.assertEqual(set(result), {'video'})
        match['params']['pattern'] = '其他管理员'
        result = evaluate_graph(self.graph, self.registry, self.c)
        self.assertIsNone(result['video'].encoding)
        self.assertEqual(result['video'].source, self.ref)
        self.assertEqual(set(result), {'video'})
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_real_input_required_but_user_selected_output_can_replace_picture(self):
        self.c.workflow_input = None
        with self.assertRaisesRegex(ValueError, '注入真实视频'):
            evaluate_graph(self.graph, self.registry, self.c)
        from app_plugins.builtin.task_delivery_workflow import build_output
        other = self.root / 'other.mp4'; other.write_bytes(b'other')
        self.c.workflow_input = {'video': self.ref, 'path': str(self.source)}
        other_ref = ResourceRef.capture(other, 'video')
        result = build_output({'video': other_ref}, {}, self.c)
        self.assertEqual(result['video'].source, other_ref)
        self.assertEqual(self.c.workflow_input['video'], self.ref)
        self.assertEqual(self.c.workflow_input['path'], str(self.source))

    def test_output_still_validates_video_type_and_changed_resource(self):
        from app_plugins.builtin.task_delivery_workflow import build_output
        with self.assertRaises(TypeError): build_output({'video': 'not a video'}, {}, self.c)
        other = self.root / 'other.mp4'; other.write_bytes(b'other')
        selected = ResourceRef.capture(other, 'video')
        other.write_bytes(b'changed after queue')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            build_output({'video': selected}, {}, self.c)

    def test_filename_blocks_traversal_windows_reserved_and_normalizes_mp4(self):
        for name in ('../a.mp4', '..\\a.mp4', 'C:\\x.mp4', 'folder/a.mp4', 'CON.mp4', 'CON .mp4', 'LPT1', '', 'bad\nname.mp4', 'abc.'):
            with self.subTest(name=name), self.assertRaises(ValueError): filename(name)
        self.assertEqual(filename('中文 标题.mov'), '中文 标题.mp4')
        self.assertEqual(filename('Logo成品'), 'Logo成品.mp4')

    def test_upload_processor_retains_versions_outside_job_cleanup_directory(self):
        from model.TaskResultOrganizer import file_identity
        from app_plugins.builtin.task_delivery_workflow import processor
        private = self.root / 'upload-job' / '0'; private.mkdir(parents=True)
        source = private / 'source.mp4'; source.write_bytes(b'synthetic')
        published = private.with_name('0-processed') / '[节点处理]source.mp4'
        published.parent.mkdir(); published.write_bytes(b'first synthetic result')
        plan = {'files': {file_identity(source): {'original': str(self.source), 'root': str(self.root / 'result')}}}
        process = processor(plan, {}, self.registry, self.c)
        with patch('app_plugins.builtin.task_delivery_workflow.process_file', return_value=published):
            self.assertEqual(process(source, private, ''), published)
            published.write_bytes(b'second synthetic result')
            process(source, private, '')
        first, second = map(Path, self.c.postprocessed_paths)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b'first synthetic result')
        self.assertEqual(second.read_bytes(), b'second synthetic result')
        self.assertTrue(first.is_relative_to(self.root / '后处理结果'))

    def test_library_type_filter_roundtrip_invalid_files_and_atomic_snapshots(self):
        store = WorkflowLibrary(self.root / 'library')
        saved = store.save(self.graph, 'Logo压缩', self.registry)
        selected = store.load(saved['workflow_id'])
        self.assertEqual(selected, saved)
        self.assertEqual(len(store.list(TYPE_ID)), 1)
        self.assertEqual(store.list('another.type'), [])
        (store.root / ('f'*32 + '.json')).write_text('{broken', encoding='utf8')
        corrupt = dict(saved, workflow_id='e'*32, workflow_name=['invalid-title'])
        (store.root / ('e'*32 + '.json')).write_text(json.dumps(corrupt), encoding='utf8')
        self.assertEqual(len(store.list()), 1)
        for key in ('../config', '', 'abc'):
            with self.assertRaises(ValueError): store.load(key)
        saved['nodes'][1]['params']['crf'] = 40
        self.assertEqual(store.load(saved['workflow_id'])['nodes'][1]['params']['crf'], 30)

    def test_queue_preparation_freezes_graph_and_rejects_other_type(self):
        store = WorkflowLibrary(self.root / 'library')
        saved = store.save(self.graph, '测试', self.registry)
        services = SimpleNamespace(available_nodes=lambda: self.registry, workflow_library=store)
        payload, provider, resources = prepare({CONFIG_KEY: saved['workflow_id']}, services)
        self.assertIs(provider, self.registry)
        saved['nodes'][1]['params']['crf'] = 32
        store.save(saved, '已改', self.registry)
        self.assertEqual(payload['graph']['nodes'][1]['params']['crf'], 30)
        self.assertEqual(prepare({}, services), (None, None, ()))

    def test_settler_and_compression_share_video_port_and_accumulate_operations(self):
        settle = self.registry.definitions['core.settle']
        value = settle.build({'video': self.ref, 'operations': ((FilterOperation('模拟效果', None, 'null'),),)}, {}, self.c)
        compressed = self.registry.definitions['core.compress'].build({'video': value['video']},
            {'codec': 'libx264', 'preset': 'fast', 'crf': 28, 'audio': 'aac', 'audio_kbps': 128}, self.c)['video']
        repeated = settle.build({'video': compressed, 'operations': ()}, {}, self.c)
        self.assertEqual(len(repeated['video'].operations), 1)
        self.assertEqual(repeated['plan'].encoding.crf, 28)
        self.assertEqual(normalize_plan(RenderPlan(compressed, (), '')).video, self.ref)

    def test_encoding_validation_and_no_raw_command_fields(self):
        for bad in ({'crf': True}, {'crf': 52}, {'preset': 'fast;evil'}, {'codec': '-i'}, {'audio_kbps': 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError): EncodingSettings(**bad)

    def test_plugin_combo_filters_refreshes_saves_only_its_config_key(self):
        from app_plugins.builtin.task_delivery_workflow_ui import PostprocessChoice
        store = WorkflowLibrary(self.root / 'library')
        saved = store.save(self.graph, '我的后处理', self.registry)
        config = {'keep': ['private-resource-list']}
        def save(updates, *, expected):
            self.assertEqual(expected, config)
            self.assertEqual(set(updates), {CONFIG_KEY}); config.update(updates); return True
        service = SimpleNamespace(load_config=lambda: dict(config), patch_config=save)
        with patch('app_plugins.builtin.task_delivery_workflow_ui.WorkflowLibrary', return_value=store):
            choice = PostprocessChoice(service)
            self.assertEqual(choice.count(), 2)
            choice.setCurrentIndex(choice.findData(saved['workflow_id']))
            self.assertEqual(config['keep'], ['private-resource-list'])
            self.assertEqual(config[CONFIG_KEY], saved['workflow_id'])
            store._path(saved['workflow_id']).unlink(); choice.refresh()
            self.assertIn('不可用', choice.currentText())
            self.assertEqual(choice.currentData(), saved['workflow_id'])
            with self.assertLogs('app_plugins.builtin.task_delivery_workflow_ui', level='ERROR'), \
                    patch.object(service, 'load_config', side_effect=ValueError('synthetic config failure')), \
                    patch.object(QtWidgets.QMessageBox, 'warning') as warning:
                self.assertFalse(choice.refresh())
                choice.persist_choice(0)
                warning.assert_called_once()
            self.assertEqual(config[CONFIG_KEY], saved['workflow_id'])
            choice.deleteLater(); self.app.processEvents()

    def test_processed_routing_preserves_task_and_review_mapping(self):
        from model.TaskResultOrganizer import compress_routed_batches, file_identity
        original = self.root / 'result'; original.mkdir()
        source = original / 'one.mp4'; source.write_bytes(b'source')
        task = SimpleNamespace(task_id='01')
        tasks, reviews = {}, {}
        def process(path, root, prefix):
            target = root.with_name(root.name + '-processed') / 'new-name.mp4'
            target.parent.mkdir(exist_ok=True); target.write_bytes(b'rendered'); return target
        with patch('model.TaskResultOrganizer.compress_video') as legacy:
            result = compress_routed_batches([(original, [source], Path('review/admin'))], {},
                task_by_file={file_identity(source): task}, upload_task_by_file=tasks,
                review_decisions={file_identity(source): 'review'}, upload_review_decisions=reviews,
                postprocessor=process)
        self.assertFalse(legacy.called)
        target = result[0][1][0]
        self.assertTrue(target.is_relative_to(result[0][0]))
        self.assertEqual(result[0][2], Path('review/admin'))
        self.assertIs(tasks[file_identity(target)], task)
        self.assertEqual(reviews[file_identity(target)], 'review')
        self.assertEqual(source.read_bytes(), b'source')

    def test_renamed_upload_keeps_original_logical_identity(self):
        from model.VideoUploadHistory import _record_from_upload, normalize_video_identity
        record = {'name': '完全不同的名字.mp4', 'id': 'synthetic-id', 'mimeType': 'video/mp4',
            'webViewLink': 'https://drive.google.com/file/d/synthetic-id/view',
            'logical_source_name': self.source.name, 'local_task_id': '01', 'local_task_row': 5}
        saved = _record_from_upload(record, date(2026, 10, 11), '03', datetime.now(timezone.utc), {})
        self.assertEqual(saved['logical_key'], normalize_video_identity(self.source.name))
        self.assertEqual(saved['file_name'], record['name'])
        self.assertEqual(saved['task']['id'], '01'); self.assertEqual(saved['task']['row'], 5)

    def test_workspace_type_library_and_simulation_are_accessible(self):
        from PYUI.node_workspace import NodeWorkspace
        from model.BackgroundTasks import BackgroundTaskManager
        manager = BackgroundTaskManager()
        window = QtWidgets.QWidget(); window.appendLog = Mock()
        host = SimpleNamespace(available_nodes=lambda: self.registry,
            background_tasks=manager, main_window=window)
        panel = NodeWorkspace(host); panel.resize(1100, 720); panel.show(); self.app.processEvents()
        with patch.object(QtWidgets.QInputDialog, 'getItem', return_value=('任务交付 · 视频后处理', True)):
            panel.new_workflow()
        self.assertEqual(panel.mode_combo.currentData(), TYPE_ID)
        self.assertEqual(panel.run_button.text(), '纯色模拟预览')
        self.assertTrue(panel.run_button.isEnabled())
        self.assertFalse(panel.task_button.isVisible())
        with patch.object(panel, 'preview_node') as preview:
            panel.run()
        preview.assert_called_once_with(panel.graph['output_node'])
        library = WorkflowLibrary(self.root / 'ui-library')
        with patch('PYUI.node_workspace.WorkflowLibrary', return_value=library), \
                patch.object(QtWidgets.QInputDialog, 'getText', return_value=('测试后处理', True)):
            panel.save_to_library()
        self.assertTrue(Path(panel.path).is_file())
        key = panel.graph['workflow_id']
        panel.graph['nodes'][1]['params']['crf'] = 31; panel.dirty = True
        with patch('PYUI.node_workspace.WorkflowLibrary', return_value=library):
            panel.save_file()
        self.assertEqual(library.load(key)['nodes'][1]['params']['crf'], 31)
        qa = os.environ.get('NODE_QA')
        if qa:
            Path(qa).mkdir(parents=True, exist_ok=True)
            panel.fit(); self.app.processEvents(); panel.grab().save(str(Path(qa) / '后处理节点面板.png'))
        panel.deleteLater(); window.deleteLater(); manager.shutdown(); manager.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实FFmpeg')
class NativePostprocessTests(PostprocessTests):
    def video(self):
        command = [FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=0x203040:s=240x160:r=20:d=2',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)]
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
        self.ref = ResourceRef.capture(self.source, 'video')

    def replacement_video(self):
        selected = self.root / '明确选择的替换素材.mp4'
        result = subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=80x120:r=20:d=0.6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            str(selected)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
        return selected

    def selected_video_graph(self, selected, *, stitch=False, injected_side='left', basis='left'):
        graph = copy.deepcopy(self.graph)
        entry, compressed, output = graph['nodes']
        imported = new_node(self.registry.definitions['core.import'])
        imported['params']['video'] = str(selected)
        graph['nodes'].append(imported)
        graph['connections'] = [[compressed['id'], 'video', output['id'], 'video']]
        if stitch:
            from app_plugins.builtin.video_stitch.workflow_nodes import stitch_node
            if 'video_stitch.horizontal' not in self.registry.definitions:
                self.registry.register('video_stitch', stitch_node())
            combined = new_node(self.registry.definitions['video_stitch.horizontal'])
            combined['params'].update(basis=basis)
            graph['nodes'].append(combined)
            other_side = 'right' if injected_side == 'left' else 'left'
            graph['connections'] += [[entry['id'], 'video', combined['id'], injected_side],
                [imported['id'], 'video', combined['id'], other_side],
                [combined['id'], 'video', compressed['id'], 'video']]
        else:
            graph['connections'].append([imported['id'], 'video', compressed['id'], 'video'])
        return graph

    def test_intentional_replacement_renders_selected_picture_with_original_delivery_name(self):
        self.video(); selected = self.replacement_video()
        before = [self.source.read_bytes(), selected.read_bytes()]
        graph = self.selected_video_graph(selected)
        payload, _ = prepare_job(graph, self.registry, None, SimpleNamespace())
        self.c.ffmpeg = FFMPEG
        target = process_file(self.source, self.source, self.root / 'replaced', payload, self.registry, self.c)
        self.assertEqual(target.name, '[节点处理]' + self.source.name)
        info = probe(target, str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertEqual((int(info['video']['width']), int(info['video']['height'])), (80, 120))
        self.assertAlmostEqual(info['duration'], .6, delta=.06)
        self.assertFalse(info['audio'])
        self.assertEqual(before, [self.source.read_bytes(), selected.read_bytes()])
        self.assertFalse(list((self.root / 'replaced').glob('.node-*')))

    def test_delivery_stitch_all_input_positions_and_bases_are_allowed(self):
        self.video(); selected = self.replacement_video()
        before = [self.source.read_bytes(), selected.read_bytes()]
        self.c.ffmpeg = FFMPEG
        from model.node_workflow import render
        for injected_side in ('left', 'right'):
            for basis in ('left', 'right'):
                with self.subTest(injected_side=injected_side, basis=basis):
                    graph = self.selected_video_graph(selected, stitch=True, injected_side=injected_side, basis=basis)
                    payload, _ = prepare_job(graph, self.registry, None, SimpleNamespace())
                    with patch.object(render, 'render_plan', wraps=render.render_plan) as encoder:
                        target = process_file(self.source, self.source,
                            self.root / f'stitched-{injected_side}-{basis}', payload, self.registry, self.c)
                    self.assertEqual(encoder.call_count, 1)
                    info = probe(target, str(Path(FFMPEG).with_name('ffprobe.exe')))
                    self.assertEqual((int(info['video']['width']), int(info['video']['height'])), (320, 160))
                    self.assertAlmostEqual(info['duration'], 2 if injected_side == basis else .6, delta=.06)
                    self.assertEqual(bool(info['audio']), injected_side == basis)
                    self.assertEqual(target.name, '[节点处理]' + self.source.name)
        self.assertEqual(before, [self.source.read_bytes(), selected.read_bytes()])

    def test_real_compression_logo_and_one_encode_preserve_audio_and_source(self):
        self.video(); before = self.source.read_bytes()
        picture = self.root / 'Logo.png'
        image = QtGui.QImage(48, 32, QtGui.QImage.Format.Format_RGBA8888); image.fill(QtGui.QColor('red')); image.save(str(picture))
        effect = new_node(self.registry.definitions['batch_text_video.image_layer'], (300, -180))
        effect['params']['layer'].update(path=str(picture), source='file', source_file=str(picture),
            width=25, height=25, x=20, y=20, visual_animation='pulse', animation_strength=10)
        settling = new_node(self.registry.definitions['core.settle'], (620, 0))
        output = self.graph['nodes'][-1]['id']; compressed = self.graph['nodes'][1]['id']
        self.graph['nodes'] += [effect, settling]
        self.graph['connections'] = [edge for edge in self.graph['connections'] if edge[2:] != [output, 'video']]
        self.graph['connections'] += [[compressed, 'video', settling['id'], 'video'],
            [effect['id'], 'operations', settling['id'], 'operations'], [settling['id'], 'video', output, 'video']]
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        self.c.ffmpeg = FFMPEG
        target = process_file(self.source, self.source, self.root / 'published', payload, self.registry, self.c)
        self.assertEqual(target.name, '[节点处理]' + self.source.name)
        ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))
        data = probe(target, ffprobe)
        self.assertTrue(data['audio']); self.assertAlmostEqual(data['duration'], 2, delta=.1)
        self.assertEqual(self.source.read_bytes(), before)
        result = subprocess.run([FFMPEG, '-v', 'error', '-i', str(target), '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], capture_output=True, timeout=30)
        frame = QtGui.QImage.fromData(result.stdout)
        self.assertGreater(frame.pixelColor(48, 32).red(), 150)
        self.assertFalse(list((self.root / 'published').glob('.node-*')))
        self.assertEqual(len(list((self.root / 'published').glob('*.mp4'))), 1)

    def test_pure_color_simulation_preview_does_not_need_real_task_or_save_output(self):
        self.graph['simulation'] = dict(width=240, height=160, duration=2, color='#cc3300')
        output = self.graph['output_node']
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace(), root_id=output)
        payload.update(preview_node=output, ffmpeg=FFMPEG)
        before = list(self.root.iterdir())
        result = run_preview(payload, self.c, registry=self.registry)
        self.assertTrue(result['simulation']); self.assertTrue(result['png'])
        self.assertIn('纯色模拟', result['title'])
        self.assertEqual(before, list(self.root.iterdir()))

    def test_real_video_stream_works_with_existing_export_and_timeline(self):
        self.video(); self.c.ffmpeg = FFMPEG; self.c.task = None; self.c.notes = []; self.c.generated_subtitles = None
        self.c.workflow_registry = self.registry
        for source in (self.ref, VideoTimeline((VideoSection(self.ref, 0, 1),), 240, 160, '20', 'test', True)):
            value = VideoStream(source, (), EncodingSettings(crf=30))
            output = self.root / ('timeline.mp4' if isinstance(source, VideoTimeline) else 'ordinary.mp4')
            result = render_plan(RenderPlan(value, (), str(output)), self.c, FFMPEG)
            self.assertEqual(result['video_encodings'], 1); self.assertEqual(result['intermediate_videos'], 0)
            self.assertTrue(output.is_file())

    def test_postprocess_memory_and_pipe_keep_composition_and_source_identity(self):
        self.video(); self.c.ffmpeg = FFMPEG
        for mode in ('memory', 'stream'):
            graph = copy.deepcopy(self.graph)
            entry, compressed, output = graph['nodes']
            cache = new_node(self.registry.definitions['core.transport'])
            cache['params']['mode'] = mode
            graph['nodes'].append(cache)
            graph['connections'] = [edge for edge in graph['connections'] if edge[2:] != [output['id'], 'video']]
            graph['connections'] += [[compressed['id'], 'video', cache['id'], 'video'],
                [cache['id'], 'video', output['id'], 'video']]
            payload, _ = prepare_job(graph, self.registry, None, SimpleNamespace())
            target = process_file(self.source, self.source, self.root / mode, payload, self.registry, self.c)
            self.assertTrue(target.is_file())
            self.assertTrue(probe(target, str(Path(FFMPEG).with_name('ffprobe.exe')))['audio'])

    def test_noop_file_memory_and_pipe_do_not_reencode_or_duplicate_output(self):
        self.video(); self.c.ffmpeg = FFMPEG
        before = self.source.read_bytes()
        for mode in ('file', 'memory', 'stream'):
            graph = copy.deepcopy(self.graph)
            entry, compressed, output = graph['nodes']
            graph['connections'] = [[entry['id'], 'video', output['id'], 'video']]
            if mode != 'file':
                cache = new_node(self.registry.definitions['core.transport'])
                cache['params']['mode'] = mode; graph['nodes'].append(cache)
                graph['connections'] = [[entry['id'], 'video', cache['id'], 'video'],
                    [cache['id'], 'video', output['id'], 'video']]
            payload, _ = prepare_job(graph, self.registry, None, SimpleNamespace())
            with patch('model.node_workflow.render.render_plan') as encoder:
                result = process_file(self.source, self.source, self.root/mode, payload, self.registry, self.c)
            encoder.assert_not_called(); self.assertEqual(result, self.source)
            self.assertFalse((self.root/mode).exists())
        self.assertEqual(before, self.source.read_bytes())

    def test_renamed_upload_preserves_task_review_replacement_and_pending_identity(self):
        from contextlib import ExitStack
        from model.TaskDeliveryBackground import snapshot_upload_plan, run_delivery_upload
        from model.TaskResultOrganizer import (file_identity, save_pending_changed_file_batches,
            load_pending_changed_file_batches)
        from model.VideoUploadHistory import normalize_video_identity
        self.video(); before = self.source.read_bytes()
        old = __import__('time').time() - 10; os.utime(self.source, (old, old))
        config = {'drive_parent_folder_id': 'synthetic-parent', 'open_result_dir': False,
            'task_result_pending_file': str(self.root / 'pending.json'),
            'upload_date_override': '2026-10-11', 'upload_slot_override': '03'}
        day = date(2026, 10, 11)
        task = SimpleNamespace(task_id='01', source_row=7, task_type='口播', admin='管理员', review_required=True)
        save_pending_changed_file_batches(config, self.root, [day], [(self.root, [self.source])])
        plan = snapshot_upload_plan([(self.root, [self.source], Path('review/admin'))],
            {file_identity(self.source): task}, {file_identity(self.source): 'review'}, config, {},
            self.root, [day], self.root / 'snapshot', True)
        plan['files'][file_identity(plan['batches'][0][1][0])]['root'] = str(self.root / 'result')
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        plan['postprocess'] = payload; self.c.ffmpeg = FFMPEG
        saved, review, submissions, replacements = [], [], [], []
        def upload(_service, batches, _parent, **kwargs):
            replacements.append(kwargs['preferred_file_ids'])
            return [{'local_file': str(path), 'root_dir': str(root), 'name': Path(path).name,
                'mimeType': 'video/mp4', 'id': 'existing-remote-id', 'remote_prefix': str(prefix),
                'webViewLink': 'https://drive.google.com/file/d/synthetic/view'}
                for root, paths, prefix in batches for path in paths]
        def capture(target, result):
            target.extend(copy.deepcopy(result)); return 0
        with ExitStack() as stack:
            module = 'model.TaskResultOrganizer.'
            stack.enter_context(patch(module+'load_drive_service', return_value=object()))
            stack.enter_context(patch(module+'preferred_drive_file_ids_by_identity',
                return_value={normalize_video_identity(self.source.name): 'existing-remote-id'}))
            stack.enter_context(patch(module+'upload_routed_changed_files_to_drive_batch', side_effect=upload))
            stack.enter_context(patch(module+'write_task_submission_links', side_effect=lambda _c,r,**kw: capture(submissions,r)))
            stack.enter_context(patch(module+'write_review_video_links', side_effect=lambda _c,r: capture(review,r)))
            stack.enter_context(patch(module+'record_video_uploads', side_effect=lambda _c,r,*a,**kw: capture(saved,r) or {}))
            stack.enter_context(patch(module+'pending_review_recovery_records', return_value=[]))
            stack.enter_context(patch(module+'print_person_folder_links', return_value={}))
            result = run_delivery_upload(plan, self.c, registry=self.registry)
        self.assertEqual(result['uploaded_file_count'], 1)
        self.assertEqual(result['upload_failed_files'], [])
        self.assertEqual(replacements, [{'[节点处理]' + self.source.name: 'existing-remote-id'}])
        self.assertEqual(load_pending_changed_file_batches(config, self.root, [day]), [])
        for record in (saved[0], review[0], submissions[0]):
            self.assertEqual(record['local_file'], str(self.source))
            self.assertEqual(record['local_task_id'], '01')
            self.assertEqual(record['final_review_decision'], 'review')
            self.assertEqual(record['logical_source_name'], self.source.name)
        self.assertEqual(before, self.source.read_bytes())
        kept = Path(result['postprocessed_files'][0])
        self.assertTrue(kept.is_relative_to(self.root / '后处理结果'))
        self.assertTrue(kept.is_file())
        from model.AppDataStorage import data_io
        data_io.rmtree(plan['work_dir'])
        self.assertTrue(kept.is_file())
        self.assertTrue(probe(kept, str(Path(FFMPEG).with_name('ffprobe.exe')))['audio'])

    def test_replacement_upload_uses_original_task_review_and_remote_identity(self):
        from contextlib import ExitStack
        from model.TaskDeliveryBackground import snapshot_upload_plan, run_delivery_upload
        from model.TaskResultOrganizer import file_identity
        from model.VideoUploadHistory import normalize_video_identity
        self.video(); selected = self.replacement_video()
        graph = self.selected_video_graph(selected)
        before = [self.source.read_bytes(), selected.read_bytes()]
        old = __import__('time').time() - 10; os.utime(self.source, (old, old))
        task = SimpleNamespace(task_id='01', source_row=7, task_type='口播', admin='原任务管理员', review_required=True)
        config = {'drive_parent_folder_id': 'synthetic-parent', 'open_result_dir': False,
            'task_result_pending_file': str(self.root / 'pending.json'),
            'upload_date_override': '2026-10-11', 'upload_slot_override': '03'}
        plan = snapshot_upload_plan([(self.root, [self.source], Path('.'))],
            {file_identity(self.source): task}, {file_identity(self.source): 'review'}, config, {},
            self.root, [date(2026, 10, 11)], self.root / 'snapshot', True)
        plan['files'][file_identity(plan['batches'][0][1][0])]['root'] = str(self.root / 'result')
        plan['postprocess'], _ = prepare_job(graph, self.registry, None, SimpleNamespace())
        self.c.ffmpeg = FFMPEG
        submissions, review, remote_ids = [], [], []
        def upload(_service, batches, _parent, **kwargs):
            remote_ids.append(kwargs['preferred_file_ids'])
            return [{'local_file': str(path), 'root_dir': str(root), 'name': Path(path).name,
                'mimeType': 'video/mp4', 'id': 'original-remote-id', 'remote_prefix': str(prefix),
                'webViewLink': 'https://drive.google.com/file/d/synthetic/view'}
                for root, paths, prefix in batches for path in paths]
        def capture(target, result): target.extend(copy.deepcopy(result)); return 0
        with ExitStack() as stack:
            module = 'model.TaskResultOrganizer.'
            stack.enter_context(patch(module+'load_drive_service', return_value=object()))
            stack.enter_context(patch(module+'preferred_drive_file_ids_by_identity',
                return_value={normalize_video_identity(self.source.name): 'original-remote-id'}))
            stack.enter_context(patch(module+'upload_routed_changed_files_to_drive_batch', side_effect=upload))
            stack.enter_context(patch(module+'write_task_submission_links', side_effect=lambda _c,r,**kw: capture(submissions,r)))
            stack.enter_context(patch(module+'write_review_video_links', side_effect=lambda _c,r: capture(review,r)))
            stack.enter_context(patch(module+'record_video_uploads', return_value={}))
            stack.enter_context(patch(module+'pending_review_recovery_records', return_value=[]))
            stack.enter_context(patch(module+'print_person_folder_links', return_value={}))
            result = run_delivery_upload(plan, self.c, registry=self.registry)
        self.assertEqual(result['uploaded_file_count'], 1)
        self.assertEqual(remote_ids, [{'[节点处理]'+self.source.name: 'original-remote-id'}])
        for record in (submissions[0], review[0]):
            self.assertEqual(record['local_file'], str(self.source))
            self.assertEqual(record['local_task_id'], '01')
            self.assertEqual(record['final_review_decision'], 'review')
            self.assertEqual(record['logical_source_name'], self.source.name)
        kept = Path(result['postprocessed_files'][0])
        self.assertEqual(int(probe(kept, str(Path(FFMPEG).with_name('ffprobe.exe')))['video']['width']), 80)
        self.assertEqual(before, [self.source.read_bytes(), selected.read_bytes()])

    def test_local_processing_and_cancellation_never_upload_or_modify_source(self):
        from model.TaskDeliveryBackground import snapshot_upload_plan, run_delivery_postprocess
        from model.TaskResultOrganizer import file_identity
        from model.BackgroundTasks import BackgroundTaskCanceled
        self.video(); before = self.source.read_bytes()
        old = __import__('time').time() - 10; os.utime(self.source, (old, old))
        plan = snapshot_upload_plan([(self.root, [self.source], Path('.'))], {}, {}, {}, {},
            self.root, [date(2026, 10, 11)], self.root / 'snapshot', True)
        # Mirror the real result root: <date>/result, output <date>/后处理结果.
        plan['files'][file_identity(plan['batches'][0][1][0])]['root'] = str(self.root / 'result')
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        plan['postprocess'] = payload; self.c.ffmpeg = FFMPEG
        with patch('model.TaskResultOrganizer.load_drive_service') as drive:
            result = run_delivery_postprocess(plan, self.c, registry=self.registry)
        drive.assert_not_called()
        self.assertEqual(result['processed'], 1)
        self.assertTrue(Path(result['paths'][0]).is_relative_to(self.root / '后处理结果'))
        self.assertEqual(self.source.read_bytes(), before)
        canceled = context(); canceled.ffmpeg = FFMPEG; canceled._canceled.set()
        with self.assertRaises(BackgroundTaskCanceled):
            process_file(self.source, self.source, self.root / 'canceled', payload, self.registry, canceled)
        self.assertFalse(list((self.root / 'canceled').glob('*.mp4')))

    def test_failed_processing_keeps_pending_and_never_uploads(self):
        from model.TaskDeliveryBackground import snapshot_upload_plan, run_delivery_upload
        from model.TaskResultOrganizer import save_pending_changed_file_batches, load_pending_changed_file_batches
        self.video(); before = self.source.read_bytes()
        old = __import__('time').time() - 10; os.utime(self.source, (old, old))
        day = date(2026, 10, 11)
        config = {'drive_parent_folder_id': 'synthetic-parent',
            'task_result_pending_file': str(self.root / 'pending.json')}
        save_pending_changed_file_batches(config, self.root, [day], [(self.root, [self.source])])
        plan = snapshot_upload_plan([(self.root, [self.source], Path('.'))], {}, {}, config, {},
            self.root, [day], self.root / 'snapshot', True)
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        plan['postprocess'] = payload; self.c.ffmpeg = FFMPEG
        with patch('model.TaskResultOrganizer.load_drive_service', return_value=object()), \
             patch('model.node_workflow.render.render_plan', side_effect=RuntimeError('synthetic disk failure')), \
             patch('model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch',
                   side_effect=lambda _s, batches, *_a, **_kw: list(batches)), \
             patch('model.GoogleDriveHelper.sync_file') as upload:
            with self.assertRaisesRegex(RuntimeError, 'disk failure'):
                run_delivery_upload(plan, self.c, registry=self.registry)
        upload.assert_not_called()
        self.assertEqual(load_pending_changed_file_batches(config, self.root, [day]), [(self.root, [self.source])])
        self.assertEqual(before, self.source.read_bytes())


if __name__ == '__main__': unittest.main()
