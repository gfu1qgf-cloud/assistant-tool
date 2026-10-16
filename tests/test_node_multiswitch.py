import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, validate, evaluate_graph, GraphError, save_graph, load_graph
from model.node_workflow.selection import KIND
from model.node_workflow.clipboard import capture_nodes, paste_nodes
from model.node_workflow.reroute import insert_reroute


class MultiSwitchTests(unittest.TestCase):
    def setUp(self):
        self.registry = NodeRegistry(); register_core(self.registry)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.calls = []

    def graph(self, conditions=(False, True, True), dtype='text', values=('A', 'B', 'C', 'D')):
        chosen = new_node(self.registry.definitions[KIND])
        chosen['params']['branches'] = len(conditions)
        graph = dict(empty_graph(), nodes=[chosen])
        for key, value in list(zip((f'condition_{i}' for i in range(1, len(conditions) + 1)), conditions)) + list(
                zip([f'value_{i}' for i in range(1, len(conditions) + 1)] + ['default'], values)):
            data_type = 'boolean' if key.startswith('condition_') else dtype
            def build(_i, _p, _c, key=key, value=value):
                self.calls.append(key)
                if isinstance(value, Exception): raise value
                return {'value': value}
            definition = NodeDefinition('test.' + key, key, outputs=(NodePort('value', key, data_type),), build=build)
            self.registry.register('test', definition)
            source = new_node(definition)
            graph['nodes'].append(source)
            graph['connections'].append([source['id'], 'value', chosen['id'], key])
        return graph, chosen

    def test_first_true_wins_and_later_conditions_and_results_are_not_built(self):
        graph, chosen = self.graph((True, RuntimeError('later condition'), True),
                                  values=('A', RuntimeError('unused result'), 'C', RuntimeError('else')))
        result = evaluate_graph(graph, self.registry, self.context, root_id=chosen['id'])
        self.assertEqual(result, {'value': 'A'})
        self.assertEqual(self.calls, ['condition_1', 'value_1'])

    def test_elif_and_default_have_exact_order_and_short_circuit(self):
        for conditions, expected, calls in (
                ((False, True, True), 'B', ['condition_1', 'condition_2', 'value_2']),
                ((False, False, False), 'D', ['condition_1', 'condition_2', 'condition_3', 'default'])):
            self.setUp(); graph, chosen = self.graph(conditions)
            self.assertEqual(evaluate_graph(graph, self.registry, self.context, root_id=chosen['id'])['value'], expected)
            self.assertEqual(self.calls, calls)

    def test_values_are_polymorphic_but_mixed_unused_branch_is_still_invalid(self):
        for dtype, values in (('number', (1, 2, 3, 4)), ('boolean', (True, False, True, False)),
                              ('video', tuple(ResourceRef(f'{i}.mp4', 'video', 0, 0) for i in range(4)))):
            self.setUp(); graph, chosen = self.graph(dtype=dtype, values=values)
            self.assertIs(evaluate_graph(graph, self.registry, self.context, root_id=chosen['id'])['value'], values[1])
        self.setUp(); graph, chosen = self.graph()
        source = next(n for n in graph['nodes'] if n['kind'] == 'test.value_3')
        self.registry.definitions[source['kind']] = replace(self.registry.definitions[source['kind']],
            outputs=(NodePort('value', '数值', 'number'),))
        with self.assertRaisesRegex(GraphError, '不匹配'):
            evaluate_graph(graph, self.registry, self.context, root_id=chosen['id'])

    def test_missing_condition_result_or_default_fails_before_execution(self):
        for port in ('condition_3', 'value_3', 'default'):
            self.setUp(); graph, chosen = self.graph((True, False, False))
            graph['connections'] = [e for e in graph['connections'] if e[3] != port]
            self.assertTrue(validate(graph, self.registry, root_id=chosen['id'])[0])
            with self.assertRaises(GraphError):
                evaluate_graph(graph, self.registry, self.context, root_id=chosen['id'])
            self.assertFalse(self.calls)

    def test_port_factory_bounds_invalid_count_cannot_execute(self):
        definition = self.registry.definitions[KIND]
        self.assertEqual(len(definition.input_ports({'branches': 32})), 65)
        for value in (0, 33, True, '3', 2.5):
            graph, chosen = self.graph()
            chosen['params']['branches'] = value
            self.assertTrue(validate(graph, self.registry, root_id=chosen['id'])[0])
            self.setUp()

    def test_dynamic_ports_survive_save_copy_and_alt_junction(self):
        graph, chosen = self.graph((False,) * 5, values=('A', 'B', 'C', 'D', 'E', 'F'))
        edge = next(e for e in graph['connections'] if e[3] == 'value_5')
        dot = insert_reroute(graph, self.registry, edge, [40, 50])
        self.assertFalse(validate(graph, self.registry, root_id=chosen['id'])[0])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'graph.json'; save_graph(path, graph)
            self.assertEqual(load_graph(path), graph)
        data, _ = capture_nodes(graph, {n['id'] for n in graph['nodes']})
        copied, ids = paste_nodes(empty_graph(), data, self.registry, [100, 200])
        target = next(n for n in copied['nodes'] if n['kind'] == KIND)
        self.assertEqual(len(self.registry.definitions[KIND].input_ports(target['params'])), 11)
        self.assertEqual(evaluate_graph(copied, self.registry, self.context, root_id=target['id'])['value'], 'F')

    def test_registry_rejects_noncallable_or_malformed_factory(self):
        for changes in ({'input_factory': 'bad'}, {'select_input': 'bad'},
                        {'input_factory': lambda _p: (NodePort('x', 'x', 'any'),)}):
            with self.assertRaises(ValueError):
                self.registry.register('test', NodeDefinition('test.bad', 'bad', build=lambda *_: {}, **changes))
