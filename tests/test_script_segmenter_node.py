"""Native-list output, real typed graph integration, no user data or network."""
import copy
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_plugins.builtin.script_segmenter.engine import split_copy
from app_plugins.builtin.script_segmenter.nodes import KIND, build, segment_node
from app_plugins.builtin.script_segmenter.plugin import ScriptSegmenterPlugin
from model.BackgroundTasks import BackgroundTaskCanceled, TaskRunContext
from model.node_workflow.graph import (
    GraphError, NodeRegistry, empty_graph, evaluate_graph, new_node, register_core, validate,
)


class SegmenterNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.definition = segment_node()
        self.registry.register('script_segmenter', self.definition)
        self.params = new_node(self.definition)['params']
        self.messages = []
        self.context = TaskRunContext(threading.Event(), lambda *args: self.messages.append(args), lambda *_: None)

    def build(self, text, **params):
        return build({'text': text}, {**self.params, **params}, self.context)

    def test_plugin_registers_node_and_existing_commands_without_dialog(self):
        definitions, commands = [], []
        context = SimpleNamespace(register_node=definitions.append, register_command=commands.append)
        plugin = ScriptSegmenterPlugin()
        plugin.register(context)
        self.assertEqual([item.kind for item in definitions], [KIND])
        self.assertEqual(len(commands), 3)
        self.assertIsNone(plugin.dialog)
        self.assertIsNone(plugin.quick_copy)
        self.assertEqual(self.definition.execution, 'compute')
        self.assertEqual(self.definition.execution_scope, 'any')
        self.assertEqual(self.definition.inputs[0].data_type, 'text')
        self.assertEqual(self.definition.outputs[0].data_type, 'list')
        self.assertNotIn('text', [item.key for item in self.definition.outputs])

    def test_native_list_has_only_unmodified_segment_text_not_copy_labels(self):
        source = 'Dr. Mária hovorí: “Boh nás miluje!”\n' + 'amen ' * 58 + '2026.'
        result = self.build(source)
        expected = split_copy(source, {'boundary_strategy': 'punctuation_first'})
        self.assertIs(type(result['segments']), list)
        self.assertTrue(all(type(item) is str for item in result['segments']))
        self.assertEqual(result['segments'], [item['text'] for item in expected['segments']])
        self.assertEqual(result['count'], len(result['segments']))
        self.assertEqual(result['estimated_seconds'], expected['estimated_seconds'])
        self.assertFalse(any(item.startswith('【第') for item in result['segments']))
        self.assertIn('“Boh nás miluje!”', result['segments'][0])

    def test_node_is_searchable_in_actual_qt_picker(self):
        from PYUI.node_catalog import NodePicker
        from qt_compat import QtCore
        picker = NodePicker(self.registry)
        try:
            picker.search.setText('智能文案分段')
            visible = [item for item, _terms in picker.entries if not item.isHidden()]
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0].data(0, QtCore.Qt.ItemDataRole.UserRole), KIND)
            self.assertEqual(visible[0].text(1), '文字处理')
        finally:
            picker.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_two_strategies_and_four_duration_rules_reuse_engine(self):
        source = (' '.join(['amen'] * 14) + ', ') * 5
        for strategy in ('punctuation_first', 'fewest'):
            for mode, durations in (('auto', [6, 8, 10]), ('10', [10]), ('8', [8]), ('6', [6])):
                with self.subTest(strategy=strategy, mode=mode):
                    result = self.build(source, boundary_strategy=strategy, duration_mode=mode)
                    expected = split_copy(source, {'boundary_strategy': strategy, 'durations': durations})
                    self.assertEqual(result['segments'], [item['text'] for item in expected['segments']])

    def test_timing_parameters_and_list_values_are_not_shared_or_mutated(self):
        source = 'amen ' * 80
        params = copy.deepcopy(self.params)
        fast = self.build(source, words_per_minute=300)
        slow = self.build(source, words_per_minute=60, pause_seconds=1, padding_seconds=1)
        self.assertGreater(slow['estimated_seconds'], fast['estimated_seconds'])
        self.assertGreater(slow['count'], fast['count'])
        fast['segments'].clear()
        self.assertTrue(self.build(source)['segments'])
        self.assertEqual(self.params, params)

    def test_short_tail_absorbed_and_notice_is_separate_from_body(self):
        source = ' '.join(['amen'] * 14) + '. ' + ' '.join(['amen'] * 22) + '. Amen.'
        result = self.build(source)
        self.assertEqual(result['count'], 2)
        self.assertTrue(result['segments'][-1].endswith('Amen.'))
        self.assertIn('已并入上一段', result['warnings'])
        self.assertFalse(result['over_limit'])
        self.assertNotIn('已并入上一段', ''.join(result['segments']))

    def test_total_180_seconds_guard_and_custom_limit_without_popup_or_padding_sum(self):
        with patch('qt_compat.QtWidgets.QMessageBox', side_effect=AssertionError('No workflow dialog')):
            exact = self.build('amen ' * 450, padding_seconds=2)
            long = self.build('amen ' * 451)
            custom = self.build('amen ' * 451, total_limit_seconds=240)
        self.assertEqual(exact['estimated_seconds'], 180)
        self.assertFalse(exact['over_limit'])
        self.assertTrue(long['over_limit'])
        self.assertIn('超过3分00秒', long['warnings'])
        self.assertFalse(custom['over_limit'])
        self.assertTrue(any('超过3分00秒' in str(message) for message in self.messages))
        self.assertNotIn('超过3分00秒', ''.join(long['segments']))

    def test_empty_invalid_and_huge_inputs_fail_without_fake_empty_results(self):
        for source in ('', '  ', '.', 'x' * 120001):
            with self.subTest(source_length=len(source)), self.assertRaises(ValueError):
                self.build(source)
        for source in (None, 12, ['text']):
            with self.subTest(source=source), self.assertRaises(TypeError):
                self.build(source)

    def test_invalid_parameters_rejected_in_build(self):
        for key, value in (('boundary_strategy', 'bad'), ('duration_mode', '12'),
                           ('words_per_minute', True), ('words_per_minute', 0),
                           ('pause_seconds', float('nan')), ('padding_seconds', 3),
                           ('total_limit_seconds', 0)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.build('Amen.', **{key: value})

    def test_cancellation_before_and_during_split_propagates(self):
        self.context._canceled.set()
        with self.assertRaises(BackgroundTaskCanceled):
            self.build('amen ' * 100)
        self.context._canceled.clear()
        with patch.object(self.context, 'is_canceled', return_value=True):
            with self.assertRaises(BackgroundTaskCanceled):
                self.build('amen ' * 100)

    def graph(self, output_port='segments', mode='detail'):
        source, split, preview = [new_node(self.registry.definitions[kind])
                                  for kind in ('core.text', KIND, 'core.preview')]
        source['params']['text'] = 'amen ' * 60
        graph = empty_graph()
        graph.update(nodes=[source, split, preview], execution_mode=mode,
                     connections=[[source['id'], 'text', split['id'], 'text'],
                                  [split['id'], output_port, preview['id'], 'value']])
        return graph, split, preview

    def test_real_graph_previews_all_ports_with_typed_values_in_line_and_detail(self):
        for mode in ('line', 'detail'):
            for port in self.definition.outputs:
                with self.subTest(mode=mode, port=port.key):
                    graph, _, preview = self.graph(port.key, mode)
                    self.assertFalse(validate(graph, self.registry, root_id=preview['id'])[0])
                    value = evaluate_graph(graph, self.registry, self.context, root_id=preview['id'])['value']
                    expected = self.build('amen ' * 60)[port.key]
                    self.assertEqual(value, expected)

    def test_real_graph_list_can_feed_list_length_node_without_parsing(self):
        graph, split, preview = self.graph()
        length = new_node(self.registry.definitions['core.py_len'])
        graph['nodes'].append(length)
        graph['connections'][-1:] = [[split['id'], 'segments', length['id'], 'value'],
                                     [length['id'], 'number', preview['id'], 'value']]
        self.assertEqual(evaluate_graph(graph, self.registry, self.context, root_id=preview['id'])['value'], 3)

    def test_graph_serialization_preserves_parameters_and_rejects_wrong_connection(self):
        graph, split, preview = self.graph()
        split['params'].update(boundary_strategy='fewest', duration_mode='6', words_per_minute=90)
        loaded = json.loads(json.dumps(graph))
        self.assertEqual(evaluate_graph(loaded, self.registry, self.context, root_id=preview['id']),
                         evaluate_graph(graph, self.registry, self.context, root_id=preview['id']))
        target = new_node(self.registry.definitions['core.text_length'])
        graph['nodes'].append(target)
        graph['connections'].append([split['id'], 'segments', target['id'], 'text'])
        with self.assertRaises(GraphError):
            evaluate_graph(graph, self.registry, self.context, root_id=target['id'])


if __name__ == '__main__':
    unittest.main()
