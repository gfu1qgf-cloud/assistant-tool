import copy
import os
import unittest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from model.node_workflow.graph import NodeRegistry, register_core, new_node, empty_graph
from model.node_workflow.editing import connection_candidate, insert_node_on_edge, gather_reroute
from PYUI.node_graph_canvas import NodeScene, NodeView, EdgeItem


class ConnectionUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = NodeRegistry(); register_core(self.registry)
        self.graph = empty_graph()
        self.a = new_node(self.registry.definitions['core.text'], (0, 0))
        self.b = new_node(self.registry.definitions['core.text_length'], (600, 250))
        self.graph['nodes'] = [self.a, self.b]
        self.scene = NodeScene(self.graph, self.registry)
        self.view = NodeView(self.scene); self.view.resize(1000, 600)
        self.addCleanup(self.view.close)

    def test_both_directions_and_nearby_snap_at_every_zoom(self):
        for reverse in (False, True):
            for zoom in (.25, .5, 1, 2, 3):
                with self.subTest(reverse=reverse, zoom=zoom):
                    self.graph['connections'] = []; self.scene.rebuild()
                    self.view.resetTransform(); self.view.scale(zoom, zoom)
                    out = self.scene.nodes[self.a['id']].ports[(True, 'text')]
                    inp = self.scene.nodes[self.b['id']].ports[(False, 'text')]
                    start, end = (inp, out) if reverse else (out, inp)
                    self.scene.begin_wire(start)
                    position = end.scenePos() + QtCore.QPointF(12/zoom, 0)
                    self.scene.move_wire(position)
                    self.assertIs(self.scene.snap_target, end)
                    self.assertEqual(end.snap_state, 'valid')
                    self.scene.finish_wire(position)
                    self.assertEqual(self.graph['connections'], [[self.a['id'], 'text', self.b['id'], 'text']])
                    self.scene.refresh_timer.stop()

    def test_canceled_or_invalid_rewire_keeps_old_connection(self):
        edge = [self.a['id'], 'text', self.b['id'], 'text']
        self.graph['connections'] = [edge]; self.scene.rebuild()
        fixed = self.scene.nodes[self.b['id']].ports[(False, 'text')]
        self.scene.begin_wire(fixed, replacing=(edge,)); self.scene.cancel_wire()
        self.assertEqual(self.graph['connections'], [edge])
        other = new_node(self.registry.definitions['core.boolean']); self.graph['nodes'].append(other)
        with self.assertRaisesRegex(ValueError, '类型'):
            connection_candidate(self.graph, self.registry, [other['id'], 'boolean'], [self.b['id'], 'text'], replacing=(edge,))
        self.assertEqual(self.graph['connections'], [edge])

    def test_selection_highlights_curve_without_rectangle(self):
        scene = QtWidgets.QGraphicsScene()
        edge = EdgeItem(scene, ['a', 'text', 'b', 'text'])
        path = QtGui.QPainterPath(QtCore.QPointF(25, 25)); path.cubicTo(200,25,430,300,625,300)
        edge.setPath(path); edge.setSelected(True)
        image = QtGui.QImage(650,330,QtGui.QImage.Format.Format_ARGB32); image.fill(QtGui.QColor('#192331'))
        painter = QtGui.QPainter(image); option = QtWidgets.QStyleOptionGraphicsItem()
        option.state |= QtWidgets.QStyle.StateFlag.State_Selected
        edge.paint(painter,option,None); painter.end()
        self.assertTrue(all(image.pixelColor(x,18).name() == '#192331' for x in range(150,500)))

    def test_insert_retains_multi_input_order_and_existing_positions(self):
        target = new_node(self.registry.definitions['core.text_join'])
        route = new_node(self.registry.definitions['core.reroute'], (300, 200))
        self.graph['nodes'] += [target, route]
        edge = [self.a['id'], 'text', target['id'], 'texts']
        self.graph['connections'] = [edge]
        positions = [n['position'][:] for n in self.graph['nodes']]
        insert_node_on_edge(self.graph, self.registry, route['id'], edge, ('value','value'))
        self.assertEqual(self.graph['connections'][-1], [route['id'],'value',target['id'],'texts'])
        self.assertEqual(positions, [n['position'] for n in self.graph['nodes']])

    def test_gather_same_source_and_refuse_silent_replacement(self):
        route = new_node(self.registry.definitions['core.reroute'])
        other = new_node(self.registry.definitions['core.text'])
        self.graph['nodes'] += [route, other]
        edge = [self.a['id'],'text',self.b['id'],'text']
        self.graph['connections'] = [[self.a['id'],'text',route['id'],'value'], edge]
        self.assertTrue(gather_reroute(self.graph,route['id'],edge))
        different = [other['id'],'text',self.b['id'],'text']
        self.graph['connections'].append(different)
        before = copy.deepcopy(self.graph)
        with self.assertRaisesRegex(ValueError,'来源不同'): gather_reroute(self.graph,route['id'],different)
        self.assertEqual(self.graph,before)


if __name__ == '__main__': unittest.main()
