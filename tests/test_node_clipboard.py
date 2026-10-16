import copy
import json
import unittest

from model.node_workflow.clipboard import capture_nodes, paste_nodes, read_fragment, MAX_BYTES
from model.node_workflow.graph import NodeRegistry, GraphError, register_core, empty_graph, new_node, validate
from model.node_workflow.loops import loop_example_graph, paired_blocks


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.registry = NodeRegistry(); register_core(self.registry)
        self.graph = empty_graph()
        self.a = new_node(self.registry.definitions['core.text'], (10, 20))
        self.b = new_node(self.registry.definitions['core.reroute'], (310, 20))
        self.c = new_node(self.registry.definitions['core.preview'], (610, 20))
        self.a['params']['text'] = '中文标题'
        self.graph.update(nodes=[self.a, self.b, self.c], connections=[
            [self.a['id'], 'text', self.b['id'], 'value'], [self.b['id'], 'value', self.c['id'], 'value']])

    def test_copy_internal_wires_only_paste_new_ids_and_positions_without_mutating(self):
        before = copy.deepcopy(self.graph)
        data, ids = capture_nodes(self.graph, {self.a['id'], self.b['id']})
        fragment = read_fragment(data, self.registry)
        self.assertEqual(len(fragment['connections']), 1)
        result, pasted = paste_nodes(self.graph, data, self.registry, (100, 200))
        self.assertEqual(self.graph, before)
        self.assertEqual(len(result['nodes']), 5)
        self.assertFalse(pasted & ids)
        self.assertEqual(result['nodes'][-2]['position'], [100, 200])
        self.assertEqual(result['nodes'][-1]['position'], [400, 200])
        self.assertEqual(result['nodes'][-2]['params']['text'], '中文标题')
        self.assertTrue(set(result['connections'][-1][::2]) <= pasted)
        result['nodes'][-2]['params']['text'] = 'independent'
        self.assertEqual(self.a['params']['text'], '中文标题')

    def test_loop_boundary_includes_body_and_remaps_pair_without_external_feedback(self):
        for mode in ('count', 'foreach', 'while'):
            with self.subTest(mode=mode):
                graph = loop_example_graph(self.registry, mode)
                begin = next(n for n in graph['nodes'] if n['kind'] == 'core.loop_begin')
                begin['appearance'] = {'loop_color': '#ab1234'}
                data, expanded = capture_nodes(graph, {begin['id']})
                result, pasted = paste_nodes(graph, data, self.registry, (1000, 200))
                by_end, by_begin, errors = paired_blocks(result)
                self.assertFalse(errors)
                copied_begin = next(n for n in result['nodes'] if n['id'] in pasted and n['kind'] == 'core.loop_begin')
                self.assertIn(by_begin[copied_begin['id']], pasted)
                self.assertEqual(copied_begin['appearance'], begin['appearance'])
                self.assertEqual(len(pasted), len(expanded))
                self.assertEqual(result['output_node'], graph['output_node'])
                self.assertFalse(validate(result, self.registry)[0])

    def test_clipboard_cannot_import_runtime_or_workflow_identity(self):
        self.graph.update(workflow_id='private-id', workflow_name='private-name', prepared={'secret': 'synthetic'})
        data, _ = capture_nodes(self.graph, {self.a['id']})
        value = json.loads(data)
        self.assertEqual(set(value), {'format', 'version', 'nodes', 'connections'})
        self.assertNotIn(b'private', data)

    def test_invalid_external_clipboard_is_rejected_atomically(self):
        data, _ = capture_nodes(self.graph, {self.a['id'], self.b['id']})
        original = copy.deepcopy(self.graph)
        for change in ('port', 'missing', 'duplicate', 'unknown', 'version', 'position'):
            value = json.loads(data)
            if change == 'port': value['connections'][0][1] = 'nonexistent'
            if change == 'missing': value['connections'][0][0] = 'no-node'
            if change == 'duplicate': value['nodes'][1]['id'] = value['nodes'][0]['id']
            if change == 'unknown': value['nodes'][0]['kind'] = 'evil.node'
            if change == 'version': value['nodes'][0]['version'] = 999
            if change == 'position': value['nodes'][0]['position'] = [float('nan'), 0]
            with self.subTest(change=change), self.assertRaises(GraphError):
                paste_nodes(self.graph, json.dumps(value).encode(), self.registry, (0, 0))
            self.assertEqual(self.graph, original)
        for raw in (b'plain text', b'{}', b'x' * (MAX_BYTES + 1)):
            with self.assertRaises(GraphError): paste_nodes(self.graph, raw, self.registry, (0, 0))

    def test_empty_selection_and_capacity_limit_leave_graph_untouched(self):
        with self.assertRaises(GraphError): capture_nodes(self.graph, set())
        data, _ = capture_nodes(self.graph, {self.a['id']})
        graph = empty_graph(); graph['nodes'] = [new_node(self.registry.definitions['core.text']) for _ in range(128)]
        before = copy.deepcopy(graph)
        with self.assertRaises(GraphError): paste_nodes(graph, data, self.registry, (0, 0))
        self.assertEqual(graph, before)
