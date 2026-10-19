"""New reusable workflow primitives, without touching production media or task data."""
import unittest
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from model.node_workflow.basic import export, number_format
from model.node_workflow.contracts import (DirectoryRef, FileList, NodeDefinition, NodePort,
                                           RenderPlan, ResourceRef, SubtitleCue, SubtitleTrack)
from model.node_workflow.filesystem import build_file_filter
from model.node_workflow.global_data import WorkflowGlobals
from model.node_workflow.graph import (GraphError, NodeRegistry, empty_graph,
                                       evaluate_graph, new_node, register_core, validate)
from model.node_workflow.path_nodes import build_output_path
from model.node_workflow.subtitle_nodes import merge, shift, trim
from model.node_workflow.video_probe_nodes import build_video_probe


class NodeFeatureSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(check_canceled=lambda: None, ffmpeg='')

    def test_all_new_nodes_register_with_stable_typed_ports(self):
        registry = NodeRegistry()
        register_core(registry)
        expected = {'core.video_probe', 'core.file_filter', 'core.output_path',
                    'core.number_format', 'core.subtitle_shift', 'core.subtitle_trim',
                    'core.subtitle_merge', 'core.skip_if'}
        self.assertLessEqual(expected, registry.definitions.keys())
        self.assertEqual(registry.definitions['core.video_probe'].execution, 'compute')
        self.assertTrue(registry.definitions['core.subtitle_merge'].inputs[0].multiple)

    def test_probe_uses_display_dimensions_fps_audio_and_rotation(self):
        info = {'duration': 83.5, 'video': {'width': 720, 'height': 1080,
                'sample_aspect_ratio': '2:1', 'avg_frame_rate': '30000/1001',
                'side_data_list': [{'rotation': 90}]}, 'audio': True}
        with patch('model.node_workflow.render.probe_resource', return_value=info):
            result = build_video_probe({'video': object()}, {}, self.context)
        self.assertEqual((result['width'], result['height']), (1080, 1440))
        self.assertAlmostEqual(result['fps'], 29.97, places=2)
        self.assertTrue(result['has_audio'])
        self.assertEqual(result['rotate'], 90)

    def test_probe_reads_a_real_small_video_when_bundled_tools_exist(self):
        ffmpeg = Path(r'C:\Users\RTV\pythonPro\辅助小工具\runtime\ffmpeg\ffmpeg.exe')
        if not ffmpeg.is_file() or not ffmpeg.with_name('ffprobe.exe').is_file():
            self.skipTest('缺少随程序提供的 FFmpeg / FFprobe')
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'probe.mp4'
            subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'color=c=red:s=320x240:r=30', '-t', '1', '-an',
                            '-c:v', 'mpeg4', '-y', str(video)], check=True, timeout=20)
            self.context.ffmpeg = str(ffmpeg)
            result = build_video_probe({'video': ResourceRef.capture(video, 'video')},
                                       {}, self.context)
        self.assertEqual((result['width'], result['height']), (320, 240))
        self.assertAlmostEqual(result['duration'], 1, delta=.1)
        self.assertAlmostEqual(result['fps'], 30, delta=.1)
        self.assertFalse(result['has_audio'])

    def test_filter_retains_snapshot_order_and_supports_exclusion(self):
        directory = DirectoryRef('C:/sample')
        refs = tuple(ResourceRef('C:/sample/' + name, 'video', 1, 1)
                     for name in ('第一集.mp4', '第二集.mov', '第一集_口播.mp4'))
        files = FileList(directory, refs)
        params = dict(pattern='*.mp4', subject='name', operation='glob', case='ignore', exclude='no')
        result = build_file_filter({'files': files}, params, self.context)
        self.assertEqual(result['files'].files, (refs[0], refs[2]))
        self.assertEqual(result['count'], 2)
        params.update(pattern='口播', operation='contains', exclude='yes')
        result = build_file_filter({'files': files}, params, self.context)
        self.assertEqual(result['files'].files, refs[:2])
        self.assertEqual(result['files'].directory, directory)

    def test_output_path_template_uses_batch_index_without_writing(self):
        self.context.workflow_globals = WorkflowGlobals.create('line', 10).at_iteration(5)
        params = {'template': '{stem}_字幕_{index:02d}{ext}', 'style': 'windows'}
        result = build_output_path({'source': r'D:\素材\第一集.mp4',
                                    'directory': r'D:\成品'}, params, self.context)
        self.assertEqual(result['path'], r'D:\成品\第一集_字幕_05.mp4')
        with self.assertRaisesRegex(ValueError, '未知变量'):
            build_output_path({'source': r'D:\素材\第一集.mp4'},
                              {'template': '{stem.__class__}', 'style': 'windows'}, self.context)
        plan = RenderPlan(ResourceRef(r'D:\素材\第一集.mp4', 'video', 1, 1), (), '')
        self.context.task = None
        rendered = export({'plan': plan, 'output': result['path']},
                          {'destination': 'file', 'output': ''}, self.context)
        self.assertEqual(rendered['result'].output, result['path'])
        with self.assertRaisesRegex(ValueError, '指定路径'):
            export({'plan': plan, 'output': result['path']},
                   {'destination': 'task'}, self.context)

    def test_number_formatting_is_explicit_and_does_not_truncate_fractions(self):
        self.assertEqual(number_format({'number': 3}, {'format': 'zero_pad', 'digits': 2}, None),
                         {'text': '03'})
        self.assertEqual(number_format({'number': 83}, {'format': 'mmss', 'digits': 0}, None),
                         {'text': '1:23'})
        self.assertEqual(number_format({'number': .873}, {'format': 'percent', 'digits': 1}, None),
                         {'text': '87.3%'})
        with self.assertRaisesRegex(ValueError, '整数'):
            number_format({'number': 3.5}, {'format': 'zero_pad', 'digits': 2}, None)

    def test_connected_output_path_is_accepted_by_graph_validation(self):
        registry = NodeRegistry()
        register_core(registry)
        registry.register('test', NodeDefinition('test.plan', '测试计划',
            outputs=(NodePort('plan', '计划', 'render_plan'),), build=lambda *_: {}))
        graph = empty_graph()
        graph['execution_mode'] = 'detail'
        plan = new_node(registry.definitions['test.plan'])
        path = new_node(registry.definitions['core.text'])
        output = new_node(registry.definitions['core.export'])
        output['params'].update(destination='file', output='')
        graph['nodes'] = [plan, path, output]
        graph['connections'] = [[plan['id'], 'plan', output['id'], 'plan'],
                                [path['id'], 'text', output['id'], 'output']]
        graph['output_node'] = output['id']
        self.assertEqual(validate(graph, registry)[0], [])

    def test_skip_line_is_lazy_and_records_nonfailure_reason(self):
        registry = NodeRegistry()
        register_core(registry)
        graph = empty_graph()
        graph['execution_mode'] = 'line'
        condition = new_node(registry.definitions['core.boolean'])
        condition['params']['value'] = 'false'
        expensive = new_node(registry.definitions['core.text'])
        skip = new_node(registry.definitions['core.skip_if'])
        graph['nodes'] = [condition, expensive, skip]
        graph['connections'] = [[condition['id'], 'boolean', skip['id'], 'condition'],
                                [expensive['id'], 'text', skip['id'], 'value']]
        graph['output_node'] = skip['id']
        messages = []
        self.context.report = lambda message, _progress: messages.append(message)
        with self.assertRaises(GraphError):
            evaluate_graph(graph, registry, self.context, root_id=skip['id'])
        self.assertIn('跳过本行', self.context.skipped_reason)
        self.assertFalse(any('文本' in message for message in messages))
        condition['params']['value'] = 'true'
        self.context.skipped_reason = ''
        result = evaluate_graph(graph, registry, self.context, root_id=skip['id'])
        self.assertEqual(result['value'], '')
        self.assertEqual(self.context.skipped_reason, '')

    def test_subtitle_shift_trim_merge_keep_valid_task_bound_tracks(self):
        source = SubtitleTrack((SubtitleCue(.5, 1.5, '甲'), SubtitleCue(2, 3, '乙')), 'task-a', 4)
        moved = shift({'subtitles': source, 'offset': -.75}, {}, None)['subtitles']
        self.assertEqual(moved.cues[0], SubtitleCue(0, .75, '甲'))
        self.assertEqual(moved.duration, 4)
        clipped = trim({'subtitles': source, 'start': 1, 'end': 3}, {}, None)['subtitles']
        self.assertEqual(clipped.cues[0], SubtitleCue(0, .5, '甲'))
        self.assertEqual(clipped.duration, 2)
        other = SubtitleTrack((SubtitleCue(1, 1.2, '丙'),), 'task-a', 4)
        combined = merge({'subtitles': (source, other)}, {}, None)['subtitles']
        self.assertEqual([cue.text for cue in combined.cues], ['甲', '丙', '乙'])
        foreign = SubtitleTrack((SubtitleCue(0, 1, '外'),), 'task-b', 4)
        with self.assertRaisesRegex(ValueError, '不同任务'):
            merge({'subtitles': (source, foreign)}, {}, None)


if __name__ == '__main__':
    unittest.main()
