import copy
import unittest
from types import SimpleNamespace
from qt_compat import QtCore, QtWidgets
from model.node_workflow.graph import NodeRegistry, register_core, new_node, empty_graph, evaluate_graph, validate
from model.node_workflow.presentation import annotation, arrange
from model.node_workflow.clipboard import capture_nodes, paste_nodes
from model.node_workflow.subgraphs import collapse, expand, flatten
from PYUI.node_graph_canvas import NodeScene, NodeView


class PresentationUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = NodeRegistry(); register_core(self.registry)
        self.graph = empty_graph()
        self.a = new_node(self.registry.definitions['core.text']); self.a['params']['text'] = 'hello'
        self.b = new_node(self.registry.definitions['core.text_length'], (420, 50))
        self.c = new_node(self.registry.definitions['core.preview_number'], (800, 50))
        self.graph['nodes'] = [self.a, self.b, self.c]
        self.graph['connections'] = [[self.a['id'], 'text', self.b['id'], 'text'], [self.b['id'], 'number', self.c['id'], 'value']]
        self.context = SimpleNamespace(check_canceled=lambda: None, report=lambda *_: None, prepared={})

    def test_group_note_copy_paste_remaps_members(self):
        group = annotation('group', (-50, -50), members=[self.a['id'], self.b['id']]); group['text'] = 'test'
        note = annotation('note', (900, 200)); note['text'] = 'only note'
        self.graph['annotations'] = [group, note]
        fragment, ids = capture_nodes(self.graph, {group['id']})
        result, added = paste_nodes(self.graph, fragment, self.registry, (20, 30))
        copied = result['annotations'][-1]
        self.assertEqual(copied['text'], 'test'); self.assertEqual(len(copied['members']), 2)
        self.assertTrue(set(copied['members']) <= added); self.assertFalse(set(copied['members']) & {self.a['id'], self.b['id']})
        fragment, _ = capture_nodes(self.graph, {note['id']})
        result, added = paste_nodes(self.graph, fragment, self.registry, (20, 30))
        self.assertEqual(len(result['nodes']), 3); self.assertEqual(len(added), 1)

    def test_arrange_only_selected_and_preserve_wires(self):
        before = copy.deepcopy(self.graph)
        arrange(self.graph, {n['id']: (245, 160) for n in self.graph['nodes']}, {self.a['id'], self.b['id']}, 'vertical')
        self.assertEqual(self.c['position'], before['nodes'][2]['position'])
        self.assertGreater(self.b['position'][1], self.a['position'][1]); self.assertEqual(self.graph['connections'], before['connections'])

    def test_vertical_ports_and_wires_keep_positions(self):
        self.graph['view'] = {'orientation': 'vertical'}
        scene = NodeScene(self.graph, self.registry)
        inp = scene.nodes[self.b['id']].ports[(False, 'text')]
        out = scene.nodes[self.b['id']].ports[(True, 'number')]
        self.assertEqual(inp.pos().y(), 0); self.assertEqual(out.pos().y(), scene.nodes[self.b['id']].height)
        self.assertEqual(self.b['position'], [420, 50])

    def test_subgraph_output_same_and_expand_preserves_rest(self):
        expected = evaluate_graph(self.graph, self.registry, self.context, root_id=self.c['id'])
        wrapper = collapse(self.graph, self.registry, {self.b['id']}, 'length')
        self.assertEqual(evaluate_graph(self.graph, self.registry, self.context, root_id=self.c['id']), expected)
        flat, root, _ = flatten(self.graph, self.registry, wrapper['id'])
        self.assertEqual(evaluate_graph(flat, self.registry, self.context, root_id=root)[wrapper['params']['outputs'][0]['id']], 5)
        before_a, before_c = list(self.a['position']), list(self.c['position'])
        expand(self.graph, self.registry, wrapper['id'])
        self.assertEqual(evaluate_graph(self.graph, self.registry, self.context, root_id=self.c['id']), expected)
        self.assertEqual(self.graph['nodes'][0]['position'], before_a); self.assertEqual(self.graph['nodes'][1]['position'], before_c)

    def test_subgraph_internal_preview_and_nested_execution(self):
        wrapper = collapse(self.graph, self.registry, {self.b['id']}, 'one')
        internal = self.b['id']
        flat, root, _ = flatten(self.graph, self.registry, wrapper['id'] + '/' + internal)
        self.assertEqual(evaluate_graph(flat, self.registry, self.context, root_id=root)['number'], 5)
        parent = collapse(self.graph, self.registry, {wrapper['id']}, 'two')
        self.assertEqual(evaluate_graph(self.graph, self.registry, self.context, root_id=self.c['id'])['value'], 5)
        self.assertFalse(validate(self.graph, self.registry, root_id=self.c['id'])[0])

    def test_promoted_parameter_applied_and_retained_expand(self):
        wrapper = collapse(self.graph, self.registry, {self.a['id']}, 'words')
        wrapper['params']['promoted'] = [{'id': 'p', 'node': self.a['id'], 'parameter': 'text', 'title': 'text'}]
        wrapper['params']['values'] = {'p': 'changed'}
        self.assertEqual(evaluate_graph(self.graph, self.registry, self.context, root_id=self.c['id'])['value'], 7)
        expand(self.graph, self.registry, wrapper['id'])
        self.assertEqual(evaluate_graph(self.graph, self.registry, self.context, root_id=self.c['id'])['value'], 7)
