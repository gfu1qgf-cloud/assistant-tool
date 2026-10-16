import copy
import itertools
import threading
import unittest

from qt_compat import QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.basic import boolean_operation
from model.node_workflow.contracts import NodeDefinition, NodePort
from model.node_workflow.graph import NodeRegistry, register_core, new_node, empty_graph, validate, evaluate_graph
from PYUI.node_graph_canvas import NodeScene


class MultiBooleanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def graph(self, values, second=None):
        graph = empty_graph()
        operator = new_node(self.registry.definitions['core.boolean_math'])
        graph['nodes'] = [operator]
        for index, value in enumerate(values):
            source = new_node(self.registry.definitions['core.boolean'])
            source['params']['value'] = 'true' if value else 'false'
            graph['nodes'].append(source)
            graph['connections'].append([source['id'], 'boolean', operator['id'], 'a'])
        if second is not None:
            source = new_node(self.registry.definitions['core.boolean'])
            source['params']['value'] = 'true' if second else 'false'
            graph['nodes'].append(source)
            graph['connections'].append([source['id'], 'boolean', operator['id'], 'b'])
        return graph, operator

    def test_one_to_eight_conditions_all_truth_tables(self):
        for count in range(1, 9):
            for values in itertools.product((False, True), repeat=count):
                for op, expected in (('and', all(values)), ('or', any(values)), ('xor', sum(values) % 2 == 1)):
                    self.assertIs(boolean_operation({'a': values}, {'operation': op}, None)['boolean'], expected)

    def test_optional_second_condition_and_legacy_scalar_call(self):
        for a, b in itertools.product((False, True), repeat=2):
            for op, expected in (('and', a and b), ('or', a or b), ('xor', a != b)):
                self.assertIs(boolean_operation({'a': a, 'b': b}, {'operation': op}, None)['boolean'], expected)
                graph, operator = self.graph([a], b)
                operator['params']['operation'] = op
                before = copy.deepcopy(graph)
                self.assertEqual(validate(graph, self.registry, root_id=operator['id'])[0], [])
                self.assertIs(evaluate_graph(graph, self.registry, self.context, root_id=operator['id'])['boolean'], expected)
                self.assertEqual(graph, before)

    def test_empty_missing_and_non_boolean_inputs_are_rejected(self):
        for invalid in (None, 0, 1, '', 'false', [], (False, 0)):
            with self.subTest(value=invalid), self.assertRaises(TypeError):
                boolean_operation({'a': invalid}, {'operation': 'and'}, None)
        with self.assertRaises(ValueError):
            boolean_operation({'a': ()}, {'operation': 'and'}, None)
        graph, operator = self.graph([], True)
        self.assertTrue(any('未连接' in text for _, text in validate(graph, self.registry, root_id=operator['id'])[0]))

    def test_many_direct_connections_evaluate_without_list_assembly(self):
        values = [True] * 31 + [False]
        graph, operator = self.graph(values)
        self.assertEqual(validate(graph, self.registry, root_id=operator['id'])[0], [])
        for op, expected in (('and', False), ('or', True), ('xor', True)):
            operator['params']['operation'] = op
            self.assertIs(evaluate_graph(graph, self.registry, self.context, root_id=operator['id'])['boolean'], expected)

    def test_all_connected_inputs_are_evaluated_not_short_circuited(self):
        calls = []
        def source(_inputs, _params, _context):
            calls.append(True)
            return {'boolean': True}
        definition = NodeDefinition('test.boolean', '计数', outputs=(NodePort('boolean', '条件', 'boolean'),), build=source)
        self.registry.register('test', definition)
        graph, operator = self.graph([False])
        extra = new_node(definition)
        graph['nodes'].append(extra)
        graph['connections'].append([extra['id'], 'boolean', operator['id'], 'a'])
        self.assertFalse(evaluate_graph(graph, self.registry, self.context, root_id=operator['id'])['boolean'])
        self.assertEqual(calls, [True])

    def test_port_is_multiple_vertical_and_existing_nodes_do_not_move(self):
        graph, operator = self.graph([True, False, True])
        before = copy.deepcopy(graph)
        scene = NodeScene(graph, self.registry)
        try:
            port = scene.nodes[operator['id']].ports[(False, 'a')]
            self.assertTrue(port.port.multiple)
            self.assertGreater(port.boundingRect().height(), port.boundingRect().width())
            self.assertEqual(graph, before)
        finally:
            scene.refresh_timer.stop()
            scene.clear()
            scene.deleteLater()
