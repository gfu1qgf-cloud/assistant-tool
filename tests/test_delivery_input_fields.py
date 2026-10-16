"""Every TaskData field reaches its own delivery port, from a per-file snapshot."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.OdsHelper import TaskData, ReadTaskOds2, format_task_table_report
from model.TaskDataSnapshot import TASK_DATA_FIELDS, snapshot_task_data
from model.TaskDeliveryBackground import snapshot_upload_plan, task_snapshot
from model.TaskResultOrganizer import file_identity
from model.node_workflow.contracts import ResourceRef
from model.node_workflow.graph import NodeRegistry, evaluate_graph, new_node, register_core, validate
from model.node_workflow.tasks import prepare_job
from app_plugins.builtin import task_delivery_workflow as workflow


def row(identifier='001', **changes):
    values = {'admin': '管理员', 'creator': '制作人', 'task_id': identifier,
        'task_name': '原始标题/不可裁剪\n' + '完整文字' * 30, 'task_date': '2026-10-13',
        'task_type': '口播', 'submission_task_type': 'FL长口播',
        'task_audio_text': 'Nadpis\nĎakujem, že si tu. 🌷', 'task_audio_type': 'ElevenLabs',
        'task_audio_voice_id': 'synthetic-voice-id', 'task_reference_link': 'https://example.com/reel',
        'task_requirements': '20秒后添加Logo\n需要字幕，保留人声。', 'task_notes': '测试备注，不是朗读正文。',
        'task_video_prompt': 'Soft light, a peaceful garden.', 'task_resolve_requirement': '不需要',
        'review_required': '需要审核', 'subcategory': '祷告/晚间',
        'daily_stat_sheet': '2026-10', 'daily_stat_category': '口播', '_source_row': 19,
        '_defaulted_fields': {'submission_task_type'},
        '_raw': {'未来新增列': '自定义内容', '文案': '原文\nĽúbim ťa.'}}
    values.update(changes)
    return TaskData(values)


class DeliveryInputFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        workflow.register(SimpleNamespace(
            register_node=lambda n: self.registry.register('task_delivery', n),
            register_workflow_type=lambda t: self.registry.register_workflow_type('task_delivery', t)))
        self.graph = workflow.example(self.registry)
        self.entry = self.graph['nodes'][0]
        self.source = self.root / 'video.mp4'
        self.source.write_bytes(b'synthetic media for field tests')
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.context.workflow_input = {'video': ResourceRef.capture(self.source, 'video'),
            'path': str(self.source), 'task': task_snapshot(row())}

    def values(self):
        return evaluate_graph(self.graph, self.registry, self.context, root_id=self.entry['id'])

    def test_schema_covers_all_actual_taskdata_attributes(self):
        task = row()
        keys = [key for key, _title, _kind in TASK_DATA_FIELDS]
        self.assertEqual(set(keys), set(vars(task)))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(task_snapshot(task)), set(vars(task)))

    def read_registered_table(self, headers, values, schema=None):
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table, TableRow, TableCell
        from odf.text import P
        document = OpenDocumentSpreadsheet(); table = Table(name='tasks')
        document.spreadsheet.addElement(table)
        for row_values in (headers, values):
            table_row = TableRow()
            for value in row_values:
                cell = TableCell(); cell.addElement(P(text=value)); table_row.addElement(cell)
            table.addElement(table_row)
        path = self.root/'synthetic.ods'; document.save(str(path))
        return ReadTaskOds2(path, schema=schema or {'fields': {
            'task_id': {'aliases': ['序号']}, 'task_type': {'aliases': ['类型']},
            'task_audio_text': {'aliases': ['任务语音']}}}, return_report=True)

    def test_actual_table_header_shape_reaches_ports_after_column_reorder(self):
        headers = ['管理员','制作人员名称','任务名称','任务语音','序号','类型','每日统计分页',
            '每日统计类别','子类别','日期','任务参考链接','任务需求','语音类型','是否需要审核',
            '语音ID','备注','sora2提示词','XXX','需要达芬奇吗？','任务类型']
        values = ['','','','Ďakujem, že si tu.','001','reels','','','','','','20秒后加Logo\n不要压缩',
                  '','','','仅供制作参考','Soft light, peaceful garden.','','不需要','']
        before = copy.deepcopy(values)
        for reverse in (False,True):
            with self.subTest(reverse=reverse):
                columns = list(zip(headers,values))
                if reverse: columns.reverse()
                tasks, report = self.read_registered_table([key for key,_ in columns],[value for _,value in columns])
                fields = task_snapshot(tasks[0])
                self.context.workflow_input['task'] = fields
                outputs = self.values()
                self.assertEqual(outputs['task_requirements'],'20秒后加Logo\n不要压缩')
                self.assertEqual(outputs['task_notes'],'仅供制作参考')
                self.assertEqual(outputs['task_video_prompt'],'Soft light, peaceful garden.')
                self.assertEqual(outputs['task_resolve_requirement'],'不需要')
                self.assertEqual(outputs['task_audio_text'],'Ďakujem, že si tu.')
                self.assertIn('XXX',outputs['raw']); self.assertEqual(outputs['raw']['XXX'],'')
                self.assertEqual(set(outputs['raw']),set(headers))
                self.assertEqual(report['matched_headers']['task_requirements'],['任务需求'])
                self.assertIn('XXX',report['extra_headers'])
                self.assertIn('其他列保留在原始表格字段',format_task_table_report(report))
        self.assertEqual(values,before)

    def test_optional_fields_do_not_change_old_table_or_existing_aliases(self):
        from model.TaskTableSchema import normalize_task_table_schema
        schema = {'fields': {'task_id': {'aliases':['序号']},'task_type':{'default':'oral'},
                             'task_audio_text': {'aliases':['任务语音']}}}
        before = copy.deepcopy(schema)
        tasks, _ = self.read_registered_table(['序号','任务语音'],['99','Hello'],schema)
        for name in ('task_requirements','task_notes','task_video_prompt','task_resolve_requirement'):
            self.assertEqual(getattr(tasks[0],name),'')
            self.assertEqual(task_snapshot(tasks[0])[name],'')
        self.assertEqual(schema,before)
        normalized = normalize_task_table_schema(schema)
        self.assertEqual(normalized['fields']['task_audio_text']['aliases'],['任务语音'])

    def test_new_fields_support_custom_aliases_without_business_interpretation(self):
        schema = {'fields': {'task_id': {'aliases':['id']},'task_type':{'default':'oral'},
            'task_requirements': {'aliases':['制作说明']}, 'task_notes': {'aliases':['内部备注']},
            'task_video_prompt': {'aliases':['prompt']},'task_resolve_requirement':{'aliases':['剪辑需求']}}}
        task = self.read_registered_table(['id','制作说明','内部备注','prompt','剪辑需求'],
            ['01','压缩由节点自行决定','不要朗读','Test prompt','不确定，人工决定'],schema)[0][0]
        self.assertEqual(task.task_requirements,'压缩由节点自行决定')
        self.assertEqual(task.task_notes,'不要朗读')
        self.assertEqual(task.task_video_prompt,'Test prompt')
        self.assertEqual(task.task_resolve_requirement,'不确定，人工决定')
        self.assertIsNone(task.review_required)

    def test_note_only_rows_do_not_become_phantom_tasks(self):
        schema = {'fields': {'task_type': {'default':'oral'}}}
        tasks, report = self.read_registered_table(['任务需求','备注'],['内部说明','这不是一个任务'],schema)
        self.assertEqual(tasks,[]); self.assertEqual(report['task_count'],0)

    def test_requirement_port_can_drive_existing_compression_condition(self):
        test = new_node(self.registry.definitions['core.text_test'])
        test['params']['needle'] = '20秒'
        self.graph['nodes'].append(test)
        self.graph['connections'].append([self.entry['id'],'task_requirements',test['id'],'text'])
        result = evaluate_graph(self.graph,self.registry,self.context,root_id=test['id'])
        self.assertTrue(result['boolean'])

    def test_every_field_is_an_individual_typed_port_and_preserves_original_values(self):
        values = self.values()
        fields = self.context.workflow_input['task']
        ports = self.registry.definitions['task_delivery.video_input'].outputs
        self.assertEqual(set(values), {p.key for p in ports})
        self.assertEqual({p.key: p.data_type for p in ports if p.key in fields},
                         {key: kind for key, _title, kind in TASK_DATA_FIELDS})
        for key in fields:
            self.assertEqual(values[key], fields[key], key)
        self.assertNotIn('task', values)
        self.assertTrue(values['has_task'])
        self.assertTrue(values['review_required_set'])
        self.assertEqual(values['task_id'], '001')
        self.assertEqual(values['task_id_number'], 1)
        self.assertGreater(len(values['_full_task_name']), 100)
        self.assertLessEqual(len(values['task_name']), 100)

    def test_snapshot_detaches_nested_data_and_does_not_copy_application_attributes(self):
        task = row()
        before = copy.deepcopy(vars(task))
        task.api_key = 'synthetic-secret-not-a-task-field'
        captured = task_snapshot(task)
        self.assertNotIn('api_key', captured)
        self.assertEqual(captured['defaulted_fields'], ['submission_task_type'])
        self.assertEqual(json.loads(json.dumps(captured, ensure_ascii=False)), captured)
        task.raw['未来新增列'] = 'changed'
        task.defaulted_fields.clear()
        self.assertEqual(captured['raw'], before['raw'])
        self.assertEqual(captured['defaulted_fields'], sorted(before['defaulted_fields']))
        copied = snapshot_task_data(captured)
        copied['raw'].clear()
        copied['defaulted_fields'].clear()
        self.assertTrue(captured['raw'])
        self.assertTrue(captured['defaulted_fields'])

    def test_downstream_outputs_cannot_mutate_the_submission_snapshot(self):
        values = self.values()
        values['raw']['未来新增列'] = 'changed by downstream'
        values['defaulted_fields'].clear()
        again = self.values()
        self.assertEqual(again['raw']['未来新增列'], '自定义内容')
        self.assertEqual(again['defaulted_fields'], ['submission_task_type'])

    def test_review_retains_three_states_and_is_not_the_final_review_decision(self):
        for text, expected in [('需要审核', True), ('无需审核', False), ('', None), ('不确定', None)]:
            with self.subTest(text=text):
                self.context.workflow_input['task'] = task_snapshot(row(review_required=text))
                self.context.workflow_input['decision'] = 'skip'
                values = self.values()
                self.assertIs(values['review_required'], expected)
                self.assertEqual(values['review_required_text'], text)
                self.assertEqual(values['review_required_set'], expected is not None)

    def test_missing_task_has_explicit_flag_and_no_invented_numbers_or_review(self):
        self.context.workflow_input.pop('task')
        values = self.values()
        self.assertFalse(values['has_task'])
        self.assertFalse(values['review_required_set'])
        self.assertIsNone(values['review_required'])
        self.assertIsNone(values['source_row'])
        self.assertIsNone(values['task_id_number'])
        self.assertEqual(values['task_audio_text'], '')
        self.assertEqual(values['task_requirements'], '')
        self.assertEqual(values['task_notes'], '')
        self.assertEqual(values['raw'], {})
        self.assertEqual(values['defaulted_fields'], [])
        self.assertFalse(validate(self.graph, self.registry)[0])
        self.assertEqual(self.entry['version'], 1)  # Additive ports keep existing wires valid.

    def test_non_numeric_task_id_stays_text_and_never_turns_into_zero(self):
        for identifier, expected in [('ABC-5', None), ('0', 0), ('1000', 1000)]:
            with self.subTest(identifier=identifier):
                self.context.workflow_input['task'] = task_snapshot(row(identifier))
                values = self.values()
                self.assertEqual(values['task_id'], identifier)
                self.assertEqual(values['task_id_number'], expected)

    def test_custom_column_is_readable_by_existing_python_dict_get_node(self):
        read = new_node(self.registry.definitions['core.py_dict_get'])
        read['params']['name'] = '未来新增列'
        self.graph['nodes'].append(read)
        self.graph['connections'].append([self.entry['id'], 'raw', read['id'], 'items'])
        values = evaluate_graph(self.graph, self.registry, self.context, root_id=read['id'])
        self.assertEqual(values['value'].value, '自定义内容')

    def test_upload_snapshot_freezes_complete_rows_for_each_video(self):
        from datetime import date
        import time
        other = self.root / 'other.mp4'
        other.write_bytes(b'second synthetic video')
        old = time.time() - 10
        for source in (self.source, other): os.utime(source, (old, old))
        tasks = [row('01', creator='第一位', task_requirements='第一项要求'),
                 row('02', creator='第二位', task_requirements='第二项要求', review_required='无需审核')]
        plan = snapshot_upload_plan([(self.root, [self.source, other], Path('.'))],
            {file_identity(p): task for p, task in zip((self.source, other), tasks)}, {},
            {'upload_date_override': '2026-10-13', 'upload_slot_override': '03'}, {}, self.root,
            [date(2026, 10, 13)], self.root / 'snapshot', True)
        for item, task in zip(plan['files'].values(), tasks):
            self.assertEqual(item['task'], task_snapshot(task))
            task.creator = 'changed after submission'
            task.task_requirements = 'changed after submission'
            task.raw.clear()
        self.assertEqual([item['task']['creator'] for item in plan['files'].values()], ['第一位', '第二位'])
        self.assertTrue(all(item['task']['raw'] for item in plan['files'].values()))
        self.assertEqual([item['task']['task_requirements'] for item in plan['files'].values()],['第一项要求','第二项要求'])

    def test_processor_passes_the_matched_row_in_local_and_upload_modes(self):
        source = self.root / 'snapshot' / '0' / 'video.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'queued media')
        fields = self.context.workflow_input['task']
        plan = {'files': {file_identity(source): {'original': str(self.source),
            'root': str(self.root / 'result'), 'task': fields}}}
        for local in (True, False):
            with self.subTest(local=local):
                output = source.parent.with_name('0-processed') / '[节点处理]video.mp4'
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(b'synthetic result')
                with patch.object(workflow, 'process_file', return_value=output) as process:
                    workflow.processor(plan, {}, self.registry, self.context, local=local)(source, source.parent, '.')
                self.assertEqual(process.call_args.kwargs['task_fields'], fields)
                self.assertEqual(process.call_args.kwargs['local'], local)

    def test_process_file_injects_row_and_restores_context_after_success_or_error(self):
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        original = self.context.workflow_input
        old_task = self.context.task = object()
        seen = []
        def render(plan, context, _ffmpeg):
            seen.append(workflow.build_input({}, {}, context))
            return {'video': plan.output}
        for error in (False, True):
            with self.subTest(error=error), \
                 patch('model.node_workflow.render.resolve_tools', return_value=('synthetic-ffmpeg', 'synthetic-ffprobe')), \
                 patch('model.node_workflow.render.render_plan', side_effect=RuntimeError('synthetic render failure') if error else render):
                if error:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic render failure'):
                        workflow.process_file(self.source, self.source, self.root / 'result', payload,
                            self.registry, self.context, task_fields=original['task'])
                else:
                    workflow.process_file(self.source, self.source, self.root / 'result', payload,
                        self.registry, self.context, task_fields=original['task'])
                self.assertIs(self.context.workflow_input, original)
                self.assertIs(self.context.task, old_task)
        self.assertEqual(seen[0]['task_audio_text'], original['task']['task_audio_text'])
        for field in ('task_requirements','task_notes','task_video_prompt','task_resolve_requirement'):
            self.assertEqual(seen[0][field],original['task'][field])
        self.assertEqual(seen[0]['raw'], original['task']['raw'])

    def test_consecutive_videos_use_their_own_tasks_with_the_same_graph_and_context(self):
        second = self.root / 'second.mp4'
        second.write_bytes(b'second synthetic media')
        fields = [task_snapshot(row('01', creator='甲', review_required='需要审核')),
                  task_snapshot(row('02', creator='乙', review_required='无需审核'))]
        plan = {'files': {file_identity(source): {'original': str(source),
            'root': str(self.root / 'result'), 'task': task}
            for source, task in zip((self.source, second), fields)}}
        payload, _ = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        process = workflow.processor(plan, payload, self.registry, self.context, local=True)
        original = self.context.workflow_input
        seen = []
        def render(output, context, _ffmpeg):
            seen.append(evaluate_graph(self.graph, self.registry, context, root_id=self.entry['id']))
            return {'video': output.output}
        with patch('model.node_workflow.render.resolve_tools', return_value=('synthetic-ffmpeg', 'synthetic-ffprobe')), \
             patch('model.node_workflow.render.render_plan', side_effect=render):
            process(self.source, self.root, '.')
            process(second, self.root, '.')
        self.assertEqual([item['task_id'] for item in seen], ['01', '02'])
        self.assertEqual([item['creator'] for item in seen], ['甲', '乙'])
        self.assertEqual([item['review_required'] for item in seen], [True, False])
        self.assertEqual([item['path'] for item in seen], list(map(str, (self.source, second))))
        self.assertIs(self.context.workflow_input, original)
        self.assertEqual(payload['task'], None)

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Real FFmpeg is not configured')
    def test_synthetic_preview_populates_all_fields_without_using_real_task_data(self):
        self.context.ffmpeg = os.environ['NODE_FFMPEG']
        self.graph['simulation'] = {'width': 64, 'height': 96, 'duration': 1, 'color': '#334155'}
        simulated = workflow.preview_input(self.context, self.root, self.graph)
        self.context.workflow_input = simulated
        values = self.values()
        self.assertEqual(set(simulated['task']), {key for key, _title, _kind in TASK_DATA_FIELDS})
        self.assertEqual(values['task_id'], '1000')
        self.assertEqual(values['task_id_number'], 1000)
        self.assertEqual(values['creator'], '模拟制作人')
        self.assertIn('模拟要求', values['task_requirements'])
        self.assertTrue(values['task_notes']); self.assertTrue(values['task_video_prompt'])
        self.assertIn('不需要', values['task_resolve_requirement'])
        self.assertEqual(values['raw'], {'自定义列': '模拟值'})
        self.assertEqual(values['path'], '模拟视频.mp4')
        self.assertTrue(values['has_task'])

    def test_canvas_has_all_ports_without_changing_existing_node_positions(self):
        from PYUI.node_graph_canvas import NodeScene
        original = copy.deepcopy(self.graph)
        scene = NodeScene(self.graph, self.registry)
        node = scene.nodes[self.entry['id']]
        ports = [child for child in node.childItems() if hasattr(child, 'port') and child.output]
        self.assertEqual({p.port.key for p in ports}, set(self.values()))
        self.assertTrue(all(node.boundingRect().contains(p.pos()) for p in ports))
        self.assertEqual(self.graph, original)


if __name__ == '__main__':
    unittest.main()
