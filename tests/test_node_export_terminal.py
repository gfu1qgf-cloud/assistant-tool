"""An export is a sink: typed executor result is not a connectable port."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from PYUI.node_graph_canvas import NodeScene
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import NodeDefinition, NodePort, RenderPlan
from model.node_workflow.graph import (NodeRegistry, GraphError, register_core, new_node,
                                      empty_graph, evaluate_graph, build_plan, validate, snapshot)
from model.node_workflow.render import run_workflow


class ExportTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def tearDown(self):
        self.temp.cleanup()

    def export_graph(self):
        source = self.root / 'source.mp4'
        source.write_bytes(b'synthetic source')
        graph = empty_graph()
        imported = new_node(self.registry.definitions['core.import'])
        settling = new_node(self.registry.definitions['core.settle'])
        exporting = new_node(self.registry.definitions['core.export'])
        imported['params']['video'] = str(source)
        exporting['params'].update(destination='file', output=str(self.root / 'final.mp4'))
        graph.update(nodes=[imported, settling, exporting], output_node=exporting['id'], connections=[
            [imported['id'], 'video', settling['id'], 'video'],
            [settling['id'], 'plan', exporting['id'], 'plan']])
        return graph

    def test_all_media_export_nodes_are_sinks_with_typed_internal_plan(self):
        for kind in ('core.export', 'core.render', 'core.task_render'):
            with self.subTest(kind=kind):
                definition = self.registry.definitions[kind]
                self.assertTrue(definition.inputs)
                self.assertEqual(definition.outputs, ())
                self.assertEqual(definition.terminal_result.data_type, 'render_plan')
                self.assertEqual(definition.result_fields, (definition.terminal_result,))

    def test_canvas_has_input_only_and_settler_keeps_its_real_outputs(self):
        graph = self.export_graph()
        scene = NodeScene(graph, self.registry)
        exporting = scene.nodes[graph['output_node']]
        self.assertEqual(set(exporting.ports), {(False, 'plan'),(False,'output')})
        self.assertFalse(any(direction for direction,_key in exporting.ports))
        settling = scene.nodes[graph['nodes'][1]['id']]
        self.assertIn((True, 'plan'), settling.ports)
        self.assertIn((True, 'video'), settling.ports)
        scene.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_internal_plan_still_reaches_executor_without_writing_or_encoding(self):
        graph = self.export_graph()
        with patch('model.node_workflow.render.render_plan') as render:
            plan = build_plan(snapshot(graph, self.registry), self.registry, self.context)
        render.assert_not_called()
        self.assertIsInstance(plan, RenderPlan)
        self.assertEqual(plan.output, str(self.root / 'final.mp4'))
        self.assertFalse(Path(plan.output).exists())

    def test_internal_result_is_not_a_wire_port_even_in_hand_edited_graph(self):
        graph = self.export_graph()
        preview = new_node(self.registry.definitions['core.preview'])
        graph['nodes'].append(preview)
        graph['connections'].append([graph['output_node'], 'result', preview['id'], 'value'])
        errors, _, _ = validate(graph, self.registry)
        self.assertTrue(any('端口不存在' in message for _, message in errors))
        with self.assertRaises(GraphError):
            snapshot(graph, self.registry)

    def test_registry_rejects_terminal_wire_outputs_and_nonfixed_contracts(self):
        contracts = (
            {'outputs': (NodePort('out', 'Out', 'text'),),
             'terminal_result': NodePort('result', 'Result', 'text')},
            {'terminal_result': NodePort('result', 'Result', 'any', type_group='value')},
            {'terminal_result': NodePort('result', 'Result', 'text', required=False)},
            {'terminal_result': NodePort('result', 'Result', 'text', multiple=True)},
            {'terminal_result': 'not a contract'},
        )
        for fields in contracts:
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, '终点节点'):
                self.registry.register('test', NodeDefinition('test.sink', 'Sink',
                    build=lambda *_: {'result': 'ok'}, **fields))

    def test_general_sink_support_is_not_hard_coded_to_export_kind(self):
        self.registry.register('test', NodeDefinition('test.sink', 'Sink',
            terminal_result=NodePort('value', 'Internal text', 'text'),
            build=lambda *_: {'value': 'ok', 'private': 'not exported'}))
        node = new_node(self.registry.definitions['test.sink'])
        graph = empty_graph()
        graph['nodes'] = [node]
        values = evaluate_graph(graph, self.registry, self.context, root_id=node['id'])
        self.assertEqual(values, {'value': 'ok'})

    def test_terminal_result_missing_null_or_wrong_type_is_rejected(self):
        for result in ({}, {'value': None}, {'value': 42}):
            with self.subTest(result=result):
                registry = NodeRegistry()
                registry.register('test', NodeDefinition('test.sink', 'Sink',
                    terminal_result=NodePort('value', 'Internal text', 'text'),
                    build=lambda *_: result))
                node = new_node(registry.definitions['test.sink'])
                graph = empty_graph()
                graph['nodes'] = [node]
                with self.assertRaisesRegex(GraphError, '内部结果'):
                    evaluate_graph(graph, registry, self.context, root_id=node['id'])

    def test_successful_render_result_is_native_python_data_for_caller(self):
        graph = self.export_graph()
        received = []
        def render(plan, _context, _ffmpeg):
            received.append(plan)
            Path(plan.output).write_bytes(b'synthetic final output')
            return {'paths': [plan.output], 'video': plan.output, 'subtitles': '',
                    'report': '', 'duration': 2.0, 'seconds': .1, 'notes': []}
        with patch('model.node_workflow.render.render_plan', side_effect=render):
            result = run_workflow({'graph': snapshot(graph, self.registry)}, self.context, registry=self.registry)
        self.assertEqual(len(received), 1)
        self.assertIs(type(result), dict)
        self.assertIs(type(result['video']), str)
        self.assertTrue(Path(result['video']).is_file())
        self.assertEqual(json.loads(json.dumps(result)), result)
        # Ordinary Python can consume the published path, without another graph port.
        self.assertEqual(Path(result['video']).read_bytes(), b'synthetic final output')

    def test_render_failure_or_cancellation_does_not_return_a_successful_file(self):
        graph = self.export_graph()
        for error in (RuntimeError('render failed'), BackgroundTaskCanceled('canceled')):
            with self.subTest(error=error), patch('model.node_workflow.render.render_plan', side_effect=error):
                with self.assertRaises(type(error)):
                    run_workflow({'graph': snapshot(graph, self.registry)}, self.context, registry=self.registry)
                self.assertFalse((self.root / 'final.mp4').exists())


if __name__ == '__main__':
    unittest.main()
