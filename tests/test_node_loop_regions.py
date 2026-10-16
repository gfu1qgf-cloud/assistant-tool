"""Wired loop membership and non-interactive Qt backgrounds."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.graph import (NodeRegistry, register_core, empty_graph,
    new_node, save_graph, load_graph, evaluate_graph)
from model.node_workflow.loops import BEGIN, END, add_loop_pair, loop_example_graph
from model.node_workflow.loop_regions import loop_regions
from model.node_workflow.reroute import insert_reroute
from model.node_workflow.python_values import unpack
from PYUI.node_graph_canvas import NodeScene, NodeView
from PYUI.node_loop_regions import LoopRegionItem


class LoopRegionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.graph = loop_example_graph(self.registry, 'while')
        self.scene = None

    def tearDown(self):
        if self.scene is not None:
            self.scene.clear()
            self.scene.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def canvas(self):
        self.scene = NodeScene(self.graph, self.registry)
        return self.scene

    def find(self, kind):
        return next(node for node in self.graph['nodes'] if node['kind'] == kind)

    def node(self, kind, position):
        node = new_node(self.registry.definitions[kind], position)
        self.graph['nodes'].append(node)
        return node

    def wire(self, first, out, last, inp):
        self.graph['connections'].append([first['id'], out, last['id'], inp])

    def test_only_directed_begin_to_end_paths_belong_to_loop(self):
        region, = loop_regions(self.graph)
        expected = {node['id'] for node in self.graph['nodes']
                    if node['kind'] in {BEGIN, END, 'core.arithmetic', 'core.compare'}}
        self.assertEqual(region.members, expected)
        for node in self.graph['nodes']:
            if node['kind'] in {'core.number', 'core.py_output', 'core.loop_list'}:
                self.assertNotIn(node['id'], region.members)
        dead_branch = self.node('core.arithmetic', (120, 1000))
        self.wire(self.find(BEGIN), 'number', dead_branch, 'a')
        self.assertNotIn(dead_branch['id'], loop_regions(self.graph)[0].members)

    def nested(self):
        self.graph = empty_graph()
        outer, end = add_loop_pair(self.graph, self.registry, (0, 0))
        inner, inner_end = add_loop_pair(self.graph, self.registry, (300, 90))
        inner_end['position'] = [1050, 90]
        end['position'] = [1350, 0]
        body = self.node('core.arithmetic', (650, 150))
        constant = self.node('core.number', (550, 700))
        self.wire(outer, 'number', inner, 'initial')
        self.wire(inner, 'value', body, 'a')
        self.wire(constant, 'number', body, 'b')
        self.wire(body, 'number', inner_end, 'result')
        self.wire(inner_end, 'last', end, 'result')
        return outer, end, inner, inner_end, body

    def test_nested_regions_have_depth_colors_and_include_inner_body(self):
        outer, end, inner, inner_end, body = self.nested()
        first, second = loop_regions(self.graph)
        self.assertEqual((first.begin, second.begin), (outer['id'], inner['id']))
        self.assertEqual((first.depth, second.depth), (0, 1))
        self.assertLess(second.members, first.members)
        self.assertNotEqual(first.color, second.color)
        scene = self.canvas()
        self.assertEqual(scene.region_for_node(body['id']).region.begin, inner['id'])
        self.assertLess(scene.regions[outer['id']].zValue(), scene.regions[inner['id']].zValue())

    def test_background_contains_members_and_updates_during_move(self):
        scene = self.canvas()
        begin = self.find(BEGIN)
        region = scene.regions[begin['id']]
        for node_id in region.region.members:
            self.assertTrue(region.path().contains(scene.nodes[node_id].sceneBoundingRect().center()))
        old_bounds = region.boundingRect()
        body = scene.nodes[self.find('core.arithmetic')['id']]
        body.setPos(1500, 900)
        self.assertNotEqual(region.boundingRect(), old_bounds)
        self.assertTrue(region.path().contains(body.sceneBoundingRect().center()))

    def test_background_never_blocks_blank_canvas_clicks_or_ports(self):
        scene = self.canvas()
        region = next(iter(scene.regions.values()))
        self.assertTrue(region.shape().isEmpty())
        self.assertEqual(region.acceptedMouseButtons(), QtCore.Qt.MouseButton.NoButton)
        self.assertIsNone(scene.itemAt(region.label_rect.center(), QtGui.QTransform()))
        begin = scene.nodes[region.region.begin]
        port = begin.ports[(True, 'block')]
        self.assertIs(scene.itemAt(port.scenePos(), QtGui.QTransform()), port)
        self.assertLess(region.zValue(), min(edge.zValue() for edge in scene.edges))

    def test_pair_reroute_is_included_and_disconnected_blocks_have_no_blob(self):
        begin, end = self.find(BEGIN), self.find(END)
        edge = next(edge for edge in self.graph['connections'] if edge == [begin['id'], 'block', end['id'], 'block'])
        knot = insert_reroute(self.graph, self.registry, edge, [320, -60])
        self.assertIn(knot['id'], loop_regions(self.graph)[0].members)
        self.canvas()
        self.graph['connections'] = [edge for edge in self.graph['connections'] if edge[2:] != [end['id'], 'block']]
        self.scene.rebuild()
        self.assertFalse(self.scene.regions)

    def test_duplicate_end_does_not_draw_misleading_regions(self):
        duplicate = self.node(END, (1200, 1000))
        self.wire(self.find(BEGIN), 'block', duplicate, 'block')
        self.assertEqual(loop_regions(self.graph), [])

    def test_color_round_trip_and_style_are_presentation_only(self):
        before = copy.deepcopy(self.graph)
        scene = self.canvas()
        begin = self.find(BEGIN)
        scene.set_loop_color(begin['id'], '#98d8aa')
        self.assertEqual(scene.regions[begin['id']].region.color, '#98d8aa')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workflow.json'
            save_graph(path, self.graph)
            restored = load_graph(path)
        self.assertEqual(loop_regions(restored)[0].color, '#98d8aa')
        self.assertEqual(restored['connections'], before['connections'])
        for graph in (before, restored):
            context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
            context.prepared = {}
            result = evaluate_graph(graph, self.registry, context)
            self.assertEqual(unpack(result['value']), [1, 2, 3, 4, 5])

    def test_bad_appearance_is_ignored_without_crashing_graph(self):
        for appearance in (None, [], {'loop_color': 'not-color'}, {'loop_color': 3}):
            self.find(BEGIN)['appearance'] = appearance
            self.assertTrue(QtGui.QColor(loop_regions(self.graph)[0].color).isValid())

    def test_ports_report_connected_and_empty_state_and_hover(self):
        scene = self.canvas()
        begin = scene.nodes[self.find(BEGIN)['id']]
        initial, unused = begin.ports[(False, 'initial')], begin.ports[(False, 'items')]
        self.assertTrue(initial.connected())
        self.assertFalse(unused.connected())
        self.assertIn('已连接', initial.toolTip())
        self.assertIn('未连接', unused.toolTip())
        self.assertFalse(initial.hovered)

    def test_region_label_tracks_mode_count_and_input(self):
        self.graph = loop_example_graph(self.registry)
        scene = self.canvas()
        begin, end = self.find(BEGIN), self.find(END)
        self.assertIn('5 次', scene.regions[begin['id']].region.title)
        end['params']['count'] = 12
        scene.update_edges()
        self.assertIn('12 次', scene.regions[begin['id']].region.title)
        source = self.node('core.number', (-300, 500))
        self.wire(source, 'number', end, 'count')
        scene.rebuild()
        self.assertIn('次数来自输入', scene.regions[begin['id']].region.title)

    def test_all_wire_styles_and_focus_pair_render_without_errors(self):
        self.nested()
        scene = self.canvas()
        view = NodeView(scene)
        view.resize(1100, 680)
        view.show()
        try:
            for style in ('smooth', 'straight', 'elbow', 'rounded'):
                scene.set_connection_style(style)
                view.fitInView(scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                self.app.processEvents()
                self.assertFalse(view.grab().isNull())
            target = self.graph['nodes'][1]['id']
            scene.focus_node(target)
            self.assertTrue(scene.nodes[target].isSelected())
        finally:
            view.close()
            view.deleteLater()
