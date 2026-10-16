"""Real selection/edit events with connected, live preview branches."""
import copy
import unittest
from unittest import mock

import test_node_workspace as fixture
from qt_compat import QtCore, QtWidgets
from PyQt6.QtTest import QTest
from model.node_workflow.graph import empty_graph, new_node


class ConnectedPreviewEditTests(unittest.TestCase):
    setUpClass = classmethod(fixture.NodeWorkspaceTests.setUpClass.__func__)
    setUp = fixture.NodeWorkspaceTests.setUp
    tearDown = fixture.NodeWorkspaceTests.tearDown
    settle = fixture.NodeWorkspaceTests.settle
    panel = fixture.NodeWorkspaceTests.panel
    selected_editor = fixture.NodeWorkspaceTests.selected_editor

    def graph_with_preview(self, kind, output):
        panel = self.panel()
        graph = empty_graph()
        node = new_node(panel.registry.definitions[kind])
        preview = new_node(panel.registry.definitions['core.preview'], (350, 0))
        graph['nodes'] = [node, preview]
        graph['connections'] = [[node['id'], output, preview['id'], 'value']]
        graph['output_node'] = preview['id']
        panel.replace_graph(graph)
        self.settle()
        return panel, node, preview

    def reselect(self, panel, node):
        panel.scene.clearSelection()
        self.settle()
        panel.scene.nodes[node['id']].setSelected(True)
        self.settle()
        return panel.inspector.widget()

    def live_preview(self, panel, preview):
        panel.show_preview({'node_id': preview['id'], 'title': 'Synthetic preview',
                            'text': 'old result', 'png': '', 'time': None})
        dialog = panel.preview_dialog(preview['id'])
        dialog.auto_refresh.setChecked(True)
        self.settle()
        handle = mock.Mock()
        handle.isRunning.return_value = True
        self.stack.enter_context(mock.patch.object(panel.host.background_tasks, 'submit', return_value=handle))
        return dialog

    def test_connected_number_keeps_all_edits_after_blank_and_auto_preview(self):
        panel, node, preview = self.graph_with_preview('core.number', 'number')
        self.live_preview(panel, preview)
        editor = self.reselect(panel, node)
        editor.findChild(QtWidgets.QDoubleSpinBox).setValue(987.25)
        self.settle()
        self.assertEqual(node['params']['value'], 987.25)
        self.reselect(panel, node)
        self.assertEqual(node['params']['value'], 987.25)
        self.assertEqual(panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox).value(), 987.25)
        panel.preview_node(preview['id'])
        panel.preview_finished()
        self.reselect(panel, node)
        self.assertEqual(node['params']['value'], 987.25)

    def test_connected_palette_keeps_choices_and_seed_after_blank(self):
        panel, node, preview = self.graph_with_preview('core.text_palette', 'body')
        self.live_preview(panel, preview)
        editor = self.reselect(panel, node)
        for combo in editor.findChildren(QtWidgets.QComboBox):
            if combo.findData('ocean') >= 0:
                combo.setCurrentIndex(combo.findData('ocean'))
            if combo.findData('colored') >= 0:
                combo.setCurrentIndex(combo.findData('colored'))
        for spin in editor.findChildren(QtWidgets.QDoubleSpinBox):
            spin.setValue(9)
        self.settle()
        expected = copy.deepcopy(node['params'])
        self.reselect(panel, node)
        self.assertEqual(node['params'], expected)
        self.assertEqual(node['params']['family'], 'ocean')
        self.assertEqual(node['params']['style'], 'colored')

    def test_connected_gif_geometry_and_keying_survive_blank_and_auto_preview(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel, node, preview = self.graph_with_preview('batch_text_video.animated_layer', 'operations')
        self.live_preview(panel, preview)
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        editor.panel.properties['x'].setValue(23)
        editor.panel.properties['width'].setValue(73)
        props = editor.panel.visual_properties.controls
        props['key_similarity'].setValue(.27)
        props['blend_mode'].setCurrentIndex(props['blend_mode'].findData('screen'))
        self.settle()
        expected = copy.deepcopy(node['params'])
        self.reselect(panel, node)
        self.assertEqual(node['params'], expected)
        self.assertEqual(node['params']['layer']['width'], 73)
        self.assertEqual(node['params']['layer']['key_similarity'], .27)

    def test_save_then_connected_preview_edits_reselect_the_live_values(self):
        panel, node, preview = self.graph_with_preview('core.number', 'number')
        self.live_preview(panel, preview)
        control = self.reselect(panel, node).findChild(QtWidgets.QDoubleSpinBox)
        control.setValue(12)
        with mock.patch.object(QtWidgets.QInputDialog, 'getText', return_value=('Synthetic saved workflow', True)):
            self.assertTrue(panel.save_to_library())
        control.setValue(42)
        live = next(item for item in panel.graph['nodes'] if item['id'] == node['id'])
        self.assertEqual(live['params']['value'], 42)
        self.reselect(panel, node)
        self.assertEqual(panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox).value(), 42)
        self.assertIs(panel.scene.nodes[node['id']].data, live)

    def test_save_then_gif_preview_edits_survive_blank_without_reconnection(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel, node, preview = self.graph_with_preview('batch_text_video.animated_layer', 'operations')
        self.live_preview(panel, preview)
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        with mock.patch.object(QtWidgets.QInputDialog, 'getText', return_value=('Synthetic saved GIF workflow', True)):
            self.assertTrue(panel.save_to_library())
        editor.panel.properties['width'].setValue(73)
        editor.panel.visual_properties.controls['key_similarity'].setValue(.27)
        live = next(item for item in panel.graph['nodes'] if item['id'] == node['id'])
        self.assertEqual(live['params']['layer']['width'], 73)
        self.assertEqual(live['params']['layer']['key_similarity'], .27)
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        self.assertEqual(editor.panel.properties['width'].value(), 73)
        self.assertEqual(editor.panel.visual_properties.controls['key_similarity'].value(), .27)

    def test_save_keeps_live_graph_objects_layout_and_active_editor(self):
        panel, node, preview = self.graph_with_preview('core.number', 'number')
        editor = self.reselect(panel, node)
        nodes, edges = panel.graph['nodes'], panel.graph['connections']
        item = panel.scene.nodes[node['id']]
        point = item.pos()
        for save_as, rename in ((False, False), (False, False), (True, False), (False, True)):
            with mock.patch.object(QtWidgets.QInputDialog, 'getText', return_value=('Synthetic workflow', True)):
                self.assertTrue(panel.save_to_library(save_as=save_as, rename=rename))
            self.assertIs(panel.graph['nodes'], nodes)
            self.assertIs(panel.graph['connections'], edges)
            self.assertIs(panel.scene.nodes[node['id']], item)
            self.assertIs(item.data, node)
            self.assertEqual(item.pos(), point)
            self.assertIs(panel.inspector.widget(), editor)
            self.assertTrue(item.isSelected())
            self.assertFalse(panel.dirty)

    def test_saved_palette_with_preview_keeps_mode_family_and_contrast_after_repeated_edits(self):
        panel, node, preview = self.graph_with_preview('core.text_palette', 'body')
        self.live_preview(panel, preview)
        self.reselect(panel, node)
        with mock.patch.object(QtWidgets.QInputDialog, 'getText', return_value=('Synthetic palette', True)):
            self.assertTrue(panel.save_to_library())
        for family in ('ocean', 'rose', 'lavender'):
            editor = self.reselect(panel, node)
            for combo in editor.findChildren(QtWidgets.QComboBox):
                for value in (family, 'colored'):
                    if combo.findData(value) >= 0:
                        combo.setCurrentIndex(combo.findData(value))
            for spin in editor.findChildren(QtWidgets.QDoubleSpinBox):
                spin.setValue(9)
            expected = copy.deepcopy(node['params'])
            self.reselect(panel, node)
            self.assertEqual(node['params'], expected)
            self.assertEqual(node['params']['style'], 'colored')
            self.assertEqual(node['params']['family'], family)

    def test_synthetic_preview_completion_never_replaces_newer_unsaved_parameters(self):
        panel, node, preview = self.graph_with_preview('core.number', 'number')
        dialog = self.live_preview(panel, preview)
        self.reselect(panel, node)
        with mock.patch.object(QtWidgets.QInputDialog, 'getText', return_value=('Synthetic auto preview', True)):
            self.assertTrue(panel.save_to_library())
        control = panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox)
        control.setValue(42)
        QTest.qWait(700)
        self.assertIsNotNone(panel.preview_handle)
        payload = panel.host.background_tasks.submit.call_args.kwargs['payload']
        self.assertEqual(payload['graph']['nodes'][0]['params']['value'], 42)
        control.setValue(99)
        self.assertEqual(payload['graph']['nodes'][0]['params']['value'], 42)
        panel.show_preview({'node_id': preview['id'], 'title': 'Synthetic preview',
                            'text': '42', 'png': '', 'time': None})
        panel.preview_finished()
        self.assertTrue(dialog.stale)
        self.reselect(panel, node)
        self.assertEqual(node['params']['value'], 99)
        self.assertEqual(panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox).value(), 99)

    def test_detached_canvas_record_cannot_seed_a_stale_inspector(self):
        panel, node, preview = self.graph_with_preview('core.number', 'number')
        panel.scene.nodes[node['id']].data = copy.deepcopy(node)
        node['params']['value'] = 42
        self.reselect(panel, node)
        self.assertEqual(panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox).value(), 42)
        self.assertIs(panel.scene.nodes[node['id']].data, node)

    def test_save_and_undo_redo_remain_correct_with_preview_connected(self):
        panel, node, preview = self.graph_with_preview('core.number', 'number')
        self.reselect(panel, node).findChild(QtWidgets.QDoubleSpinBox).setValue(12)
        with mock.patch.object(QtWidgets.QInputDialog, 'getText', return_value=('Synthetic undo', True)):
            self.assertTrue(panel.save_to_library())
        self.reselect(panel, node).findChild(QtWidgets.QDoubleSpinBox).setValue(42)
        panel.undo()
        current = next(n for n in panel.graph['nodes'] if n['id'] == node['id'])
        self.assertEqual(current['params']['value'], 12)
        self.assertIs(panel.scene.nodes[node['id']].data, current)
        panel.redo()
        current = next(n for n in panel.graph['nodes'] if n['id'] == node['id'])
        self.assertEqual(current['params']['value'], 42)
        self.reselect(panel, current)
        self.assertEqual(panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox).value(), 42)


if __name__ == '__main__':
    unittest.main()
