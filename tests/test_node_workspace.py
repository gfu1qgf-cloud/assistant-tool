import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import threading
import time
from types import SimpleNamespace
from contextlib import ExitStack
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.host import PluginHost
from app_plugins.api import NodeDefinition, NodePort
from PYUI.main_pyui import MainDialog
from PYUI.node_graph_canvas import NodeItem


class NodeWorkspaceTests(unittest.TestCase):
    def test_ai_node_uses_standard_inspector_and_edits_do_not_call_api(self):
        from app_plugins.builtin.ai_model_client.nodes import KIND
        panel = self.panel()
        with mock.patch.object(panel.host.ai_client, 'chat') as chat:
            panel.add_node(KIND); self.settle()
            node = panel.graph['nodes'][-1]
            self.assertEqual(node['kind'], KIND)
            panel.set_parameter(node, 'provider', 'mistral')
            panel.set_parameter(node, 'model', 'synthetic-model')
            panel.scene.nodes[node['id']].setSelected(True); self.settle()
            editors = panel.findChildren(QtWidgets.QPlainTextEdit)
            prompt = next(edit for edit in editors if edit.toPlainText() == node['params']['prompt'])
            prompt.setPlainText('Príliš veľa · 中文'); self.settle()
            panel.scene.clearSelection(); self.settle()
            panel.scene.nodes[node['id']].setSelected(True); self.settle()
            current = next(n for n in panel.graph['nodes'] if n['id'] == node['id'])
            self.assertEqual(current['params']['prompt'], 'Príliš veľa · 中文')
            self.assertEqual(current['params']['model'], 'synthetic-model')
            chat.assert_not_called()

    def test_batch_effect_type_editor_uses_synthetic_task_and_multi_terminal(self):
        from app_plugins.builtin.batch_text_video.node_effects import TYPE_ID, INPUT_KIND, OUTPUT_KIND
        controller = self.window.node_workspace
        self.assertTrue(controller.open_workflow(workflow_type=TYPE_ID)); self.settle()
        panel = controller.panel
        self.assertEqual(panel.graph['workflow_type'], TYPE_ID)
        self.assertEqual(panel.graph['execution_mode'], 'line')
        self.assertFalse(panel.task_action.isEnabled())
        self.assertTrue(panel.run_action.isEnabled(), panel.status.text())
        output = next(n for n in panel.graph['nodes'] if n['kind'] == OUTPUT_KIND)
        item = panel.scene.nodes[output['id']]
        self.assertFalse(any(is_output for is_output, _key in item.ports))
        self.assertTrue(item.ports[(False, 'operations')].port.multiple)
        self.assertTrue(item.can_preview())
        caption = next(n for n in panel.graph['nodes'] if n['kind'] == 'batch_text_video.caption')
        panel.scene.nodes[caption['id']].setSelected(True); self.settle()
        self.assertTrue(panel.preview_action.isEnabled()); self.assertIsNotNone(panel._preview_button)
        self.assertEqual(panel._preview_button.text(), '纯色模拟此效果')
        handle = mock.Mock(); handle.isRunning.return_value = True
        with mock.patch.object(panel.host.background_tasks, 'submit', return_value=handle) as submit:
            panel.preview_node(output['id'])
        payload = submit.call_args.kwargs['payload']
        self.assertTrue(payload['global_data']['is_preview'])
        self.assertEqual(json.loads(payload['task']['fields_json'])['task_id'], '1000')
        self.assertEqual(panel.bound_tasks, [])
        self.assertIn(INPUT_KIND, [n['kind'] for n in panel.graph['nodes']])
        if os.environ.get('NODE_QA'):
            destination = Path(os.environ['NODE_QA']); destination.mkdir(parents=True, exist_ok=True)
            panel.scene.clearSelection(); panel.fit(); self.settle()
            panel.grab().save(str(destination / 'node-effect-workflow.png'))

    def test_timing_node_numeric_and_effect_settings_survive_blank_and_reselection(self):
        panel = self.panel(); panel.add_node('batch_text_video.display_window'); self.settle()
        node = panel.graph['nodes'][-1]
        def select():
            panel.scene.clearSelection(); panel.scene.nodes[node['id']].setSelected(True); self.settle()
            return panel.inspector.widget()
        select().findChildren(QtWidgets.QDoubleSpinBox)[0].setValue(20); self.settle()
        select().findChildren(QtWidgets.QDoubleSpinBox)[1].setValue(25); self.settle()
        control = select().findChildren(QtWidgets.QComboBox)[0]
        control.setCurrentIndex(control.findData('left')); self.settle()
        control = select().findChildren(QtWidgets.QComboBox)[1]
        control.setCurrentIndex(control.findData('small')); self.settle()
        self.assertEqual(node['params']['start'], 20)
        self.assertEqual(node['params']['end'], 25)
        self.assertEqual(node['params']['enter'], 'left')
        self.assertEqual(node['params']['exit'], 'small')
        expected = copy.deepcopy(node['params'])
        panel.scene.clearSelection(); self.settle()
        inspector = select()
        self.assertEqual(node['params'], expected)
        self.assertEqual(inspector.findChildren(QtWidgets.QDoubleSpinBox)[0].value(), 20)
        self.assertEqual(inspector.findChildren(QtWidgets.QComboBox)[0].currentData(), 'left')
        if os.environ.get('NODE_QA'):
            destination = Path(os.environ['NODE_QA']); destination.mkdir(parents=True, exist_ok=True)
            panel.grab().save(str(destination / 'timing-node-editor.png'))

    def test_custom_python_editor_stable_names_and_rejects_wired_port_removal(self):
        from PYUI.node_code_editor import CodeNodeEditor
        panel = self.panel(); panel.add_node('core.python_code'); self.settle()
        code = panel.graph['nodes'][-1]
        panel.add_node('core.number'); self.settle(); source = panel.graph['nodes'][-1]
        edge = [source['id'], 'number', code['id'], 'in_value']; panel.graph['connections'].append(edge)
        panel.scene.rebuild(); self.settle()
        editor = self.selected_editor(panel, code, CodeNodeEditor)
        editor.tables['inputs'].item(0, 0).setText('renamed'); self.settle()
        self.assertEqual(code['params']['inputs'][0]['name'], 'renamed'); self.assertIn(edge, panel.graph['connections'])
        editor = self.selected_editor(panel, code, CodeNodeEditor)
        editor.tables['inputs'].selectRow(0); editor.remove_port('inputs'); self.settle()
        self.assertEqual(len(code['params']['inputs']), 1); self.assertIn(edge, panel.graph['connections'])
        self.assertEqual(editor.tables['inputs'].rowCount(), 1)
        if os.environ.get('NODE_QA'):
            destination = Path(os.environ['NODE_QA']); destination.mkdir(parents=True, exist_ok=True)
            panel.grab().save(str(destination / 'python-node-editor.png'))
        self.assertEqual(panel.inspector.horizontalScrollBar().maximum(), 0)

    def test_subgraph_modal_navigation_edits_use_same_inspector_and_parent_history(self):
        from model.node_workflow.subgraphs import collapse
        from model.node_workflow.graph import new_node, empty_graph
        from PYUI.node_workspace import NodeWorkspace
        panel = self.panel(); graph = empty_graph()
        a = new_node(panel.registry.definitions['core.text']); a['params']['text'] = 'old'
        p = new_node(panel.registry.definitions['core.preview_text'], (400, 0))
        graph['nodes'] = [a, p]; graph['connections'] = [[a['id'], 'text', p['id'], 'value']]
        panel.replace_graph(graph)
        wrapper = collapse(panel.graph, panel.registry, {a['id']}, 'words'); panel.scene.rebuild(); panel.changed()
        def edit(dialog):
            child = dialog.findChild(NodeWorkspace); self.assertIsNotNone(child)
            self.assertEqual(child.mode_combo.currentData(), 'detail')
            inner = next(n for n in child.graph['nodes'] if n['id'] == a['id'])
            child.set_parameter(inner, 'text', 'new words'); self.settle()
            self.assertEqual(next(n for n in wrapper['params']['graph']['nodes'] if n['id'] == a['id'])['params']['text'], 'new words')
            if os.environ.get('NODE_QA'):
                destination = Path(os.environ['NODE_QA']); destination.mkdir(parents=True, exist_ok=True)
                dialog.show(); self.settle(); child.fit(); self.settle()
                dialog.grab().save(str(destination / 'subgraph-editor.png'))
            return QtWidgets.QDialog.DialogCode.Accepted
        with mock.patch.object(QtWidgets.QDialog, 'exec', edit):
            panel.open_subgraph(wrapper['id'])
        self.settle()
        panel.undo(); self.settle()
        restored = next(n for n in panel.graph['nodes'] if n['id'] == wrapper['id'])
        self.assertEqual(next(n for n in restored['params']['graph']['nodes'] if n['id'] == a['id'])['params']['text'], 'old')

    def selected_editor(self, panel, node, editor_type):
        panel.scene.clearSelection()
        panel.scene.nodes[node['id']].setSelected(True)
        self.settle()
        editor = panel.inspector.widget().findChild(editor_type)
        self.assertIsNotNone(editor)
        return editor

    def test_repro_text_node_style_position_and_blank_reselection(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel = self.panel(); panel.add_node('batch_text_video.text_layer'); self.settle()
        node = panel.graph['nodes'][-1]
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        editor.panel.content_editor.text.setPlainText('Testing text content')
        editor.panel.text_properties.controls['color'].setText('#f07839')
        editor.panel.text_properties.update_values()
        editor.panel.properties['x'].setValue(23)
        editor.panel.properties['width'].setValue(70)
        editor.offset.setValue(7)
        self.settle()
        self.assertEqual(node['params']['layer']['color'], '#f07839')
        self.assertEqual(node['params']['layer']['x'], 23)
        self.assertEqual(node['params']['layer']['width'], 70)
        expected = copy.deepcopy(node['params'])
        panel.scene.clearSelection(); self.settle()
        self.assertEqual(node['params'], expected)
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        self.assertEqual(node['params'], expected)
        self.assertEqual(editor.panel.properties['x'].value(), 23)
        self.assertEqual(editor.panel.text_properties.controls['color'].text(), '#f07839')
        self.assertEqual(editor.offset.value(), 7)

    def test_repro_caption_style_animation_position_and_blank_reselection(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        panel = self.panel()
        node = next(n for n in panel.graph['nodes'] if n['kind'] == 'batch_text_video.caption')
        editor = self.selected_editor(panel, node, CaptionNodeEditor)
        editor.mode.setCurrentIndex(1)
        editor.panel.text_properties.controls['color'].setText('#f07839')
        editor.panel.text_properties.update_values()
        editor.panel.properties['y'].setValue(38)
        editor.panel.caption_properties.controls['caption_hold_ms'].setValue(333)
        self.settle()
        self.assertEqual(node['params']['caption']['color'], '#f07839')
        self.assertEqual(node['params']['caption']['y'], 38)
        self.assertEqual(node['params']['caption']['caption_hold_ms'], 333)
        expected = copy.deepcopy(node['params'])
        panel.scene.clearSelection(); self.settle()
        editor = self.selected_editor(panel, node, CaptionNodeEditor)
        self.assertEqual(node['params'], expected)
        self.assertEqual(editor.panel.properties['y'].value(), 38)
        self.assertEqual(editor.panel.text_properties.controls['color'].text(), '#f07839')

    def test_repro_regular_node_after_blank_and_preview_inspector_rebuild(self):
        panel = self.panel(); panel.add_node('core.number'); self.settle()
        node = panel.graph['nodes'][-1]
        control = panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox)
        control.setValue(987)
        self.settle()
        self.assertEqual(node['params']['value'], 987)
        panel.scene.clearSelection(); self.settle()
        panel.scene.nodes[node['id']].setSelected(True); self.settle()
        self.assertEqual(node['params']['value'], 987)
        self.assertEqual(panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox).value(), 987)
        panel.preview_finished(); self.settle()
        self.assertEqual(node['params']['value'], 987)

    def test_repro_gif_node_keying_geometry_source_and_blank_reselection(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel = self.panel(); panel.add_node('batch_text_video.animated_layer'); self.settle()
        node = panel.graph['nodes'][-1]
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        properties = editor.panel.visual_properties
        properties.controls['key_mode'].setCurrentIndex(properties.controls['key_mode'].findData('green'))
        properties.controls['key_similarity'].setValue(.27)
        properties.controls['key_alpha_high'].setValue(82)
        properties.controls['blend_mode'].setCurrentIndex(properties.controls['blend_mode'].findData('screen'))
        properties.controls['visual_animation'].setCurrentIndex(properties.controls['visual_animation'].findData('pulse'))
        properties.controls['animation_period'].setValue(2.8)
        editor.panel.properties['x'].setValue(23)
        editor.panel.properties['width'].setValue(70)
        self.settle()
        self.assertEqual(node['params']['layer']['key_mode'], 'green')
        self.assertEqual(node['params']['layer']['key_similarity'], .27)
        self.assertEqual(node['params']['layer']['key_alpha_high'], 82)
        self.assertEqual(node['params']['layer']['x'], 23)
        expected = copy.deepcopy(node['params'])
        panel.scene.clearSelection(); self.settle()
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        self.assertEqual(node['params'], expected)
        self.assertEqual(editor.panel.properties['x'].value(), 23)
        self.assertEqual(editor.panel.visual_properties.controls['key_mode'].currentData(), 'green')
        self.assertEqual(editor.panel.visual_properties.controls['key_similarity'].value(), .27)
        self.assertEqual(editor.panel.visual_properties.controls['animation_period'].value(), 2.8)

    def test_retired_gif_inspector_cannot_overwrite_newer_parameters(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel = self.panel(); panel.add_node('batch_text_video.animated_layer'); self.settle()
        node = panel.graph['nodes'][-1]
        old = self.selected_editor(panel, node, LayerNodeEditor)
        old.panel.properties['width'].setValue(60)
        # deleteLater has not run yet. This is the window in which a queued or
        # focus-out notification can still arrive from a retired editor.
        panel.scene.clearSelection()
        panel.scene.nodes[node['id']].setSelected(True)
        current = panel.inspector.widget().findChild(LayerNodeEditor)
        current.panel.properties['width'].setValue(85)
        old.offset.setValue(7)
        self.assertEqual(node['params']['layer']['width'], 85)
        self.assertEqual(node['params']['offset'], 0)

    def test_preview_start_finish_preserves_active_gif_editor_and_tab(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel = self.panel(); panel.add_node('batch_text_video.animated_layer'); self.settle()
        node = panel.graph['nodes'][-1]
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        editor.panel.reveal_property('visual')
        editor.panel.reveal_property('animation')
        text = next(n for n in panel.graph['nodes'] if n['kind'] == 'core.import')
        handle = mock.Mock(); handle.isRunning.return_value = True
        with mock.patch('PYUI.node_workspace.prepare_job', return_value=({'graph': copy.deepcopy(panel.graph)}, ())), \
             mock.patch.object(panel.host.background_tasks, 'submit', return_value=handle):
            panel.preview_node(text['id'])
        self.assertIs(panel.inspector.widget().findChild(LayerNodeEditor), editor)
        self.assertTrue(editor.panel.editor.sections['visual'].toggle.isChecked())
        panel.preview_finished()
        self.assertIs(panel.inspector.widget().findChild(LayerNodeEditor), editor)
        self.assertTrue(editor.panel.editor.sections['visual'].toggle.isChecked())
        self.assertTrue(editor.panel.editor.sections['animation'].toggle.isChecked())

    def test_gif_manual_typing_then_blank_click_commits_latest_values(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel = self.panel(); panel.add_node('batch_text_video.animated_layer'); self.settle()
        node = panel.graph['nodes'][-1]
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        editor.panel.reveal_property('position')
        spin = editor.panel.properties['width']
        # Also exercise a pending edit, rather than only programmatic setValue.
        spin.setKeyboardTracking(False)
        spin.setFocus(); spin.selectAll()
        QTest.keyClicks(spin, '73')
        point = QtCore.QPoint(12, 12)
        self.assertFalse(any(isinstance(item, NodeItem) for item in panel.view.items(point)))
        QTest.mouseClick(panel.view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                         QtCore.Qt.KeyboardModifier.NoModifier, point)
        self.settle()
        self.assertEqual(node['params']['layer']['width'], 73)
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        self.assertEqual(editor.panel.properties['width'].value(), 73)

    def test_retired_regular_controls_after_reselection_and_history_replacement(self):
        panel = self.panel(); panel.add_node('core.number'); self.settle()
        node = panel.graph['nodes'][-1]
        old = panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox)
        old.setValue(11)
        panel.inspect_selection()
        current = panel.inspector.widget().findChild(QtWidgets.QDoubleSpinBox)
        current.setValue(22)
        old.setValue(33)
        self.assertEqual(node['params']['value'], 22)
        panel.history.stack.clear()
        panel.history.reset(panel.graph)
        replacement = copy.deepcopy(panel.graph)
        next(n for n in replacement['nodes'] if n['id'] == node['id'])['params']['value'] = 44
        current.setKeyboardTracking(False)
        current.lineEdit().setText('55')
        panel.restore_history(replacement)
        self.assertEqual(next(n for n in panel.graph['nodes'] if n['id'] == node['id'])['params']['value'], 44)

    def test_preview_submission_does_not_steal_focus_from_gif_parameter(self):
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        panel = self.panel(); panel.add_node('batch_text_video.animated_layer'); self.settle()
        node = panel.graph['nodes'][-1]
        editor = self.selected_editor(panel, node, LayerNodeEditor)
        panel.window().activateWindow(); self.settle()
        editor.offset.setFocus(); self.settle()
        focused = self.app.focusWidget()
        self.assertIsNotNone(focused)
        self.assertTrue(focused is editor.offset or editor.offset.isAncestorOf(focused))
        handle = mock.Mock(); handle.isRunning.return_value = True
        with mock.patch('PYUI.node_workspace.prepare_job', return_value=({'graph': copy.deepcopy(panel.graph)}, ())), \
             mock.patch.object(panel.host.background_tasks, 'submit', return_value=handle):
            panel.preview_node(node['id'])
        self.assertIs(self.app.focusWidget(), focused)
        self.assertIs(panel.inspector.widget().findChild(LayerNodeEditor), editor)
        panel.preview_finished()

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        if os.environ.get('NODE_QA'):
            path = Path('C:/Windows/Fonts/msyh.ttc')
            if path.is_file(): QtGui.QFontDatabase.addApplicationFont(str(path))
            cls.app.setFont(QtGui.QFont('Microsoft YaHei', 9))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.config = {"untouched":{"key":"synthetic","resource_list":["keep"]},"run_upload":False}
        Path("config.json").write_text(json.dumps(self.config),encoding="utf-8")
        self.stack = ExitStack()
        for method in ("setupNotificationTray","startGoogleSheetMonitor","startFlowParameterGuard",
                       "registerLoadTaskGlobalHotkey","saveCurrentConfig"):
            self.stack.enter_context(mock.patch.object(MainDialog,method,return_value=True))
        self.stack.enter_context(mock.patch.object(PluginHost,"start_all"))
        self.window = MainDialog()
        self.window.resize(1250,950)
        self.window.show()
        self.settle()

    def settle(self):
        for _ in range(4): self.app.processEvents()

    def tearDown(self):
        self.window.config_reload.timer.stop()
        self.window.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.settle()
        self.stack.close()
        os.chdir(self.cwd)
        self.temp.cleanup()

    def panel(self, task=False):
        self.window.node_workspace.show()
        self.settle()
        panel = self.window.node_workspace.panel
        if not task:
            panel.new_example()
        return panel

    def test_workspace_is_lazy_and_does_not_change_original_panes(self):
        controller = self.window.node_workspace
        self.assertIsNone(controller.frame)
        self.assertIs(self.window.log_splitter,self.window.workspace_layout.vertical)
        self.assertIn("batch_text_video.caption",self.window.plugin_host.available_nodes().definitions)
        self.assertEqual(json.loads(Path("config.json").read_text()),self.config)

    def test_add_node_is_incremental_and_topmost_without_moving_old_items_or_view(self):
        panel = self.panel()
        panel.view.scale(.73, .73)
        panel.view.centerOn(600, 70); self.settle()
        original = dict(panel.scene.nodes)
        positions = {key: list(item.data['position']) for key, item in original.items()}
        screens = {key: panel.view.mapFromScene(item.pos()) for key, item in original.items()}
        point = original[next(iter(original))].pos()
        for kind in ('core.number', 'core.text', 'video_stitch.horizontal'):
            with mock.patch.object(panel.scene, 'rebuild', side_effect=AssertionError('添加节点不应重建已有画布')):
                panel.add_node(kind, point)
            self.settle()
            new = panel.graph['nodes'][-1]
            self.assertEqual(new['position'], [point.x(), point.y()])
            for key, old in original.items():
                self.assertIs(panel.scene.nodes[key], old)
                self.assertEqual(old.data['position'], positions[key])
                self.assertLessEqual((panel.view.mapFromScene(old.pos())-screens[key]).manhattanLength(), 1)
                self.assertGreater(panel.scene.nodes[new['id']].zValue(), old.zValue())
            hit = [item for item in panel.scene.items(point+QtCore.QPointF(120, 20)) if isinstance(item, NodeItem)]
            self.assertEqual(hit[0].data['id'], new['id'])
        last = panel.graph['nodes'][-1]['id']
        panel.scene.rebuild()
        self.assertGreater(panel.scene.nodes[last].zValue(), max(panel.scene.nodes[key].zValue() for key in original))

    def test_adding_deleting_and_rebuilding_ports_keep_view_and_existing_coordinates(self):
        panel = self.panel(); panel.view.scale(.8, .8)
        panel.scene.setSceneRect(-10000, -10000, 20000, 20000)
        panel.view.centerOn(1500, -800); self.settle()
        center = panel.view.mapToScene(panel.view.viewport().rect().center())
        anchor = panel.graph['nodes'][0]
        screen_point = panel.view.mapFromScene(QtCore.QPointF(*anchor['position']))
        positions = {n['id']: list(n['position']) for n in panel.graph['nodes']}
        panel.add_node('core.choose'); self.settle()
        node = panel.graph['nodes'][-1]
        panel.set_parameter(node, 'branches', 5); self.settle()
        after = panel.view.mapToScene(panel.view.viewport().rect().center())
        self.assertLess((panel.view.mapFromScene(QtCore.QPointF(*anchor['position'])) - screen_point).manhattanLength(), 2)
        self.assertEqual({n['id']: n['position'] for n in panel.graph['nodes'] if n['id'] in positions}, positions)
        self.assertIn((False, 'condition_5'), panel.scene.nodes[node['id']].ports)
        self.assertIn((False, 'default'), panel.scene.nodes[node['id']].ports)
        panel.undo(); self.settle()
        self.assertNotIn((False, 'condition_5'), panel.scene.nodes[node['id']].ports)
        self.assertLess((panel.view.mapFromScene(QtCore.QPointF(*anchor['position'])) - screen_point).manhattanLength(), 2)

    def test_reducing_wired_switch_branch_is_refused_without_losing_wires(self):
        panel = self.panel(); panel.add_node('core.choose'); self.settle()
        node = panel.graph['nodes'][-1]
        panel.add_node('core.boolean'); self.settle()
        source = panel.graph['nodes'][-1]
        self.assertTrue(panel.scene.nodes[source['id']].sceneBoundingRect().intersects(
            panel.scene.nodes[node['id']].sceneBoundingRect()))
        self.assertGreater(panel.scene.nodes[source['id']].zValue(), panel.scene.nodes[node['id']].zValue())
        edge = [source['id'], 'boolean', node['id'], 'condition_3']
        panel.graph['connections'].append(edge); panel.scene.rebuild()
        self.assertFalse(panel.set_parameter(node, 'branches', 2))
        self.assertEqual(node['params']['branches'], 3)
        self.assertIn(edge, panel.graph['connections'])
        qa = os.environ.get('NODE_QA')
        if qa:
            Path(qa).mkdir(parents=True, exist_ok=True)
            panel.scene.nodes[node['id']].setSelected(True)
            panel.fit(); self.settle()
            panel.grab().save(str(Path(qa) / '多条件选择.png'))

    def test_preview_refresh_reuses_window_and_detects_only_upstream_changes(self):
        panel = self.panel(); panel.add_node('core.text'); self.settle()
        text = panel.graph['nodes'][-1]
        panel.add_node('core.preview'); self.settle()
        preview = panel.graph['nodes'][-1]
        panel.graph['connections'].append([text['id'], 'text', preview['id'], 'value'])
        panel.scene.rebuild(); panel.changed()
        result = {'node_id': preview['id'], 'title': '预览', 'text': '旧数据', 'png': '', 'time': None}
        panel.show_preview(result); self.settle()
        dialog = panel.preview_dialogs[0]
        self.assertFalse(dialog.stale)
        panel.set_parameter(text, 'text', '新的数据'); self.settle()
        self.assertTrue(dialog.stale)
        with mock.patch.object(panel, 'preview_node') as refresh:
            dialog.refresh_button.click()
            refresh.assert_called_once_with(preview['id'])
        panel._preview_signatures[preview['id']] = panel.preview_signature(preview['id'])
        panel.show_preview(dict(result, text='新的数据')); self.settle()
        self.assertEqual(panel.preview_dialogs, [dialog])
        self.assertEqual(dialog.text.toPlainText(), '新的数据')
        self.assertFalse(dialog.stale)
        panel.add_node('core.text'); self.settle()
        panel.set_parameter(panel.graph['nodes'][-1], 'text', '无关数据')
        self.assertFalse(dialog.stale)
        dialog.close(); self.settle()

    def test_preview_auto_refresh_debounces_and_closing_does_not_reopen(self):
        panel = self.panel(); panel.add_node('core.text'); self.settle()
        text = panel.graph['nodes'][-1]
        result = {'node_id': text['id'], 'title': '预览', 'text': '', 'png': '', 'time': None}
        panel.show_preview(result); dialog = panel.preview_dialogs[0]
        dialog.auto_refresh.setChecked(True)
        with mock.patch.object(panel, 'preview_node') as refresh:
            for value in ('a', 'b', 'c'): panel.set_parameter(text, 'text', value)
            QTest.qWait(750); self.settle()
            refresh.assert_called_once_with(text['id'])
        dialog.close(); self.settle()
        panel.show_preview(result)
        self.assertEqual(panel.preview_dialogs, [])

    def test_queued_refresh_uses_latest_upstream_state_and_keeps_old_result_stale(self):
        panel = self.panel(); panel.add_node('core.text'); self.settle()
        text = panel.graph['nodes'][-1]
        result = {'node_id': text['id'], 'title': '预览', 'text': '旧值', 'png': '', 'time': None}
        panel.show_preview(result); dialog = panel.preview_dialogs[0]
        handle = mock.Mock(); handle.isRunning.return_value = True
        with mock.patch.object(panel.host.background_tasks, 'submit', return_value=handle) as submit:
            panel.preview_node(text['id'])
            panel.set_parameter(text, 'text', '最新值')
            panel.preview_node(text['id']); panel.preview_node(text['id'])
            self.assertEqual(len(panel._queued_previews), 1)
            panel.show_preview(result)
            self.assertTrue(dialog.stale)
            self.assertEqual(dialog.text.toPlainText(), '旧值')
            handle.isRunning.return_value = False
            panel.preview_finished(); self.settle()
            self.assertEqual(submit.call_count, 2)
            node = next(n for n in submit.call_args.kwargs['payload']['graph']['nodes'] if n['id'] == text['id'])
            self.assertEqual(node['params']['text'], '最新值')
            panel._queued_previews[text['id']] = None
            dialog.auto_refresh.setChecked(True)
            panel.cancel()
            self.assertFalse(panel._queued_previews)
            self.assertFalse(dialog.auto_refresh.isChecked())
            self.assertFalse(dialog.refresh_timer.isActive())
        dialog.close(); self.settle()

    def test_default_save_only_asks_name_updates_library_and_save_as_makes_copy(self):
        from model.node_workflow.library import WorkflowLibrary
        panel = self.panel(); library = WorkflowLibrary(Path(self.temp.name) / 'workflow-library')
        with mock.patch('PYUI.node_workspace.WorkflowLibrary', return_value=library), \
             mock.patch('PYUI.node_workspace.QtWidgets.QInputDialog.getText', return_value=('我的草稿', True)) as name, \
             mock.patch('PYUI.node_workspace.QtWidgets.QFileDialog.getSaveFileName') as external:
            self.assertTrue(panel.save_file())
            key = panel.graph['workflow_id']
            self.assertEqual(library.load(key), panel.graph)
            panel.set_parameter(panel.graph['nodes'][0], 'video', 'synthetic.mp4')
            self.assertTrue(panel.save_file())
            self.assertEqual(name.call_count, 1)
            self.assertEqual(panel.graph['workflow_id'], key)
            self.assertTrue(panel.save_file(True))
            self.assertNotEqual(panel.graph['workflow_id'], key)
            self.assertEqual(len(library.list()), 2)
            external.assert_not_called()
        self.assertEqual(json.loads(Path('config.json').read_text()), self.config)

    def test_node_keyboard_clipboard_cut_duplicate_and_undo_are_single_steps(self):
        panel = self.panel()
        graph = copy.deepcopy(panel.graph)
        for item in panel.scene.nodes.values(): item.setSelected(True)
        panel.view.setFocus(); self.settle()
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.settle()
        self.assertEqual(len(panel.graph['nodes']), len(graph['nodes']) * 2)
        self.assertEqual(len(panel.graph['connections']), len(graph['connections']) * 2)
        self.assertEqual(panel.history.stack.count(), 1)
        panel.undo(); self.settle(); self.assertEqual(panel.graph, graph)
        panel.redo(); self.settle()
        pasted = {n['id'] for n in panel.graph['nodes']} - {n['id'] for n in graph['nodes']}
        for item in panel.scene.nodes.values(): item.setSelected(item.data['id'] in pasted)
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_X, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.settle(); self.assertEqual(panel.graph, graph)
        panel.undo(); self.settle(); self.assertEqual(len(panel.graph['nodes']), len(graph['nodes']) * 2)
        for item in panel.scene.nodes.values(): item.setSelected(item.data['id'] in pasted)
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_D, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.settle(); self.assertEqual(len(panel.graph['nodes']), len(graph['nodes']) * 3)

    def test_rebuild_never_exposes_half_constructed_selected_merge_to_inspector(self):
        panel = self.panel()
        seen = []
        def check():
            seen.append(set(panel.scene.nodes))
            self.assertEqual(set(panel.scene.nodes), {node['id'] for node in panel.graph['nodes']})
        panel.scene.selectionChanged.connect(check)
        for item in panel.scene.nodes.values(): item.setSelected(True)
        seen.clear()
        panel.scene.rebuild()
        self.assertEqual(len(seen), 1)

    def test_copy_paste_in_text_field_stays_text_not_nodes(self):
        panel = self.panel(); panel.add_node('core.text'); self.settle()
        text = panel.inspector.findChild(QtWidgets.QPlainTextEdit)
        text.setPlainText('标题原文'); text.selectAll(); text.setFocus(); self.settle()
        before = len(panel.graph['nodes'])
        QTest.keyClick(text, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(text, QtCore.Qt.Key.Key_End)
        QTest.keyClick(text, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.settle()
        self.assertEqual(text.toPlainText(), '标题原文标题原文')
        self.assertEqual(len(panel.graph['nodes']), before)

    def test_independent_window_and_attachment_reuse_panel_without_reparenting(self):
        old = self.window.log_splitter
        panel = self.panel()
        controller = self.window.node_workspace
        self.assertIs(controller.frame.centralWidget(), panel)
        self.assertTrue(controller.frame.isWindow())
        self.assertFalse(controller.frame.attached)
        controller.toggle_attachment(True)
        self.settle()
        self.assertTrue(controller.frame.attached)
        controller.frame.close()
        self.settle()
        controller.show()
        self.assertIs(controller.panel,panel)
        controller.toggle_attachment(False)
        self.settle()
        self.assertFalse(controller.frame.attached)
        self.assertIs(self.window.workspace_layout.host_layout.itemAt(0).widget(), old)
        self.assertIs(self.window.task_directory_splitter.widget(0),self.window.task_table_widget)

    def test_example_shows_required_ports_and_disables_run_before_files(self):
        panel = self.panel()
        self.assertEqual(len(panel.scene.nodes),4)
        self.assertFalse(panel.run_action.isEnabled())
        caption = panel.graph["nodes"][1]
        item = panel.scene.nodes[caption["id"]]
        self.assertNotIn((False,"video"), item.ports)
        self.assertTrue(item.ports[(False,"subtitles")].port.required)
        item.setSelected(True)
        self.settle()
        self.assertEqual(panel.inspected,caption["id"])

    def test_memory_and_stream_examples_are_available_and_wired(self):
        panel = self.panel()
        for mode in ("memory", "stream"):
            panel.new_example(mode)
            self.settle()
            self.assertEqual(len(panel.scene.nodes),5)
            imported = next(n for n in panel.graph["nodes"] if n["kind"]=="core.import")
            transport = next(n for n in panel.graph["nodes"] if n["kind"]=="core.transport")
            self.assertEqual(transport["params"]["mode"],mode)
            self.assertIn([imported["id"],"video",transport["id"],"video"],panel.graph["connections"])
            video_consumers = [e for e in panel.graph["connections"] if e[0]==transport["id"]]
            self.assertEqual(len(video_consumers),1)
            self.assertFalse(panel.run_action.isEnabled())

    def test_loop_examples_and_paired_insertion_are_available(self):
        panel = self.panel()
        for mode in ("count", "foreach", "while"):
            panel.new_loop_example(mode)
            self.settle()
            self.assertTrue(panel.run_action.isEnabled())
            begin = next(n for n in panel.graph["nodes"] if n["kind"] == "core.loop_begin")
            end = next(n for n in panel.graph["nodes"] if n["kind"] == "core.loop_end")
            self.assertEqual(begin["params"]["mode"], mode)
            self.assertIn(end["id"][:8], panel.scene.nodes[begin["id"]].toolTip())
        before = len(panel.graph["nodes"])
        panel.add_loop_block()
        self.settle()
        self.assertEqual(len(panel.graph["nodes"]), before + 2)
        panel.undo()
        self.settle()
        self.assertEqual(len(panel.graph["nodes"]), before)

    def test_loop_required_ports_follow_selected_mode(self):
        panel = self.panel()
        panel.new_loop_example("while")
        begin = next(n for n in panel.graph["nodes"] if n["kind"] == "core.loop_begin")
        end = next(n for n in panel.graph["nodes"] if n["kind"] == "core.loop_end")
        first = panel.scene.nodes[begin["id"]]
        last = panel.scene.nodes[end["id"]]
        self.assertTrue(first.ports[(False, "initial")].is_required())
        self.assertTrue(last.ports[(False, "stop")].is_required())
        self.assertFalse(first.ports[(False, "items")].is_required())
        panel.set_parameter(begin, "mode", "count")
        self.assertFalse(last.ports[(False, "stop")].is_required())
        panel.set_parameter(begin, "mode", "foreach")
        self.assertTrue(first.ports[(False, "items")].is_required())
        self.assertIn("必须输入", first.ports[(False, "items")].toolTip())

    def test_loop_region_color_undo_redo_and_node_motion_do_not_touch_config(self):
        panel = self.panel()
        panel.new_loop_example('count')
        begin = next(n for n in panel.graph['nodes'] if n['kind'] == 'core.loop_begin')
        original = panel.scene.regions[begin['id']].region.color
        panel.scene.set_loop_color(begin['id'], '#f09ca0')
        self.settle()
        self.assertTrue(panel.dirty)
        panel.undo()
        self.settle()
        self.assertEqual(panel.scene.regions[begin['id']].region.color, original)
        panel.redo()
        self.settle()
        self.assertEqual(panel.scene.regions[begin['id']].region.color, '#f09ca0')
        self.assertEqual(json.loads(Path('config.json').read_text()), self.config)

    def test_media_choice_and_limit_save_in_workflow_only(self):
        panel = self.panel()
        panel.new_example("memory")
        transport = next(n for n in panel.graph["nodes"] if n["kind"]=="core.transport")
        item = panel.scene.nodes[transport["id"]]
        item.setSelected(True)
        self.settle()
        self.assertEqual(panel.inspected,transport["id"])
        panel.set_parameter(transport,"limit_mb",256)
        path = Path(self.temp.name)/"media-flow.json"
        panel.path = str(path)
        panel.export_file()
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(next(n for n in saved["nodes"] if n["kind"]=="core.transport")["params"]["limit_mb"],256)
        self.assertNotIn("resource_id",path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(Path("config.json").read_text()),self.config)

    def test_connection_type_mismatch_does_not_modify_graph(self):
        panel = self.panel()
        imported = panel.scene.nodes[panel.graph["nodes"][0]["id"]]
        caption = panel.scene.nodes[panel.graph["nodes"][1]["id"]]
        before = list(panel.graph["connections"])
        panel.scene.port_pressed(imported.ports[(True,"video")])
        panel.scene.port_pressed(caption.ports[(False,"subtitles")])
        self.assertEqual(panel.graph["connections"],before)
        self.assertIn("类型不匹配",panel.status.text())

    def test_deleting_node_clears_related_links_deferred_safely(self):
        panel = self.panel()
        node = panel.graph["nodes"][1]["id"]
        panel.scene.remove_node(node)
        self.settle()
        self.assertNotIn(node,panel.scene.nodes)
        self.assertFalse(any(node in (e[0],e[2]) for e in panel.graph["connections"]))
        self.assertFalse(panel.run_action.isEnabled())

    def test_native_mouse_connection_updates_without_destroying_event_sender(self):
        panel = self.panel()
        source_id,target_id = panel.graph["nodes"][0]["id"],panel.graph["nodes"][1]["id"]
        edge = [source_id,"subtitles",target_id,"subtitles"]
        panel.graph["connections"].remove(edge)
        panel.scene.rebuild()
        panel.fit()
        self.settle()
        first = panel.scene.nodes[source_id].ports[(True,"subtitles")]
        second = panel.scene.nodes[target_id].ports[(False,"subtitles")]
        QTest.mouseClick(panel.view.viewport(),QtCore.Qt.MouseButton.LeftButton,
                         pos=panel.view.mapFromScene(first.scenePos()))
        QTest.mouseClick(panel.view.viewport(),QtCore.Qt.MouseButton.LeftButton,
                         pos=panel.view.mapFromScene(second.scenePos()))
        self.settle()
        self.assertIn(edge,panel.graph["connections"])

    def test_native_drag_moves_node_and_remembers_position(self):
        panel = self.panel()
        item = panel.scene.nodes[panel.graph["nodes"][1]["id"]]
        start = panel.view.mapFromScene(item.mapToScene(QtCore.QPointF(80,16)))
        QTest.mousePress(panel.view.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=start)
        QTest.mouseMove(panel.view.viewport(),start+QtCore.QPoint(20,50))
        QTest.mouseRelease(panel.view.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=start+QtCore.QPoint(20,50))
        self.settle()
        self.assertNotEqual(item.data["position"],[275,40])
        self.assertTrue(panel.dirty)

    def test_submission_uses_host_queue_with_snapshot_and_only_output_directory_lock(self):
        panel = self.panel()
        root = Path(self.temp.name)
        video,subtitle = root/"source.mp4",root/"source.srt"
        video.write_bytes(b"synthetic")
        subtitle.write_text("synthetic",encoding="utf-8")
        panel.graph["nodes"][0]["params"].update(video=str(video),subtitles=str(subtitle))
        panel.graph["nodes"][-1]["params"]["output"] = str(root/"result.mp4")
        handle = mock.Mock()
        manager = self.window.plugin_host.background_tasks
        with mock.patch("PYUI.node_workspace.resolve_tools",return_value=("ffmpeg.exe","ffprobe.exe")), \
             mock.patch.object(manager,"submit",return_value=handle) as submit:
            panel.run()
        handle.isRunning.return_value = True
        call = submit.call_args.kwargs
        self.assertEqual(call["owner"],"node_workflow")
        self.assertEqual(call["kind"],"media")
        self.assertEqual(len(call["resources"]),1)
        self.assertTrue(call["resources"][0].startswith("directory:"))
        panel.graph["nodes"][0]["params"]["video"] = "changed"
        self.assertEqual(call["payload"]["graph"]["nodes"][0]["params"]["video"],str(video))
        self.window.node_workspace.frame.close()
        handle.requestInterruption.assert_not_called()

    def test_can_hide_busy_panel_without_cancelling_job(self):
        panel = self.panel()
        panel.handle = mock.Mock()
        panel.handle.isRunning.return_value = True
        self.window.node_workspace.frame.close()
        self.settle()
        panel.handle.requestInterruption.assert_not_called()

    def test_core_and_plugin_registry_are_additive_with_registration_rollback(self):
        host = self.window.plugin_host
        class BrokenPlugin:
            plugin_id = "bad"
            def register(self,context):
                context.register_node(NodeDefinition("bad.test","bad",outputs=(NodePort("x","x","video"),),
                                                     build=lambda *_:{}))
                raise ValueError("broken")
        with self.assertRaises(ValueError): host.install(BrokenPlugin())
        self.assertNotIn("bad.test",host.node_registry.definitions)
        self.assertIn("core.import",host.node_registry.definitions)

    def test_mode_control_is_explicit_saved_and_does_not_rewrite_wires(self):
        panel = self.panel(task=True)
        self.assertEqual(panel.mode_combo.currentData(), "line")
        self.bind_tasks(panel)
        wires = list(panel.graph["connections"])
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("detail"))
        self.assertFalse(panel.is_line_mode())
        self.assertEqual(panel.graph["connections"], wires)
        self.assertEqual(len(panel.bound_tasks), 2)
        self.assertFalse(panel.task_button.isEnabled())
        self.assertFalse(panel.run_action.isEnabled())
        self.assertTrue(any("仅适用于 Line" in message for _, message in panel.current_errors))
        panel.path = Path(self.temp.name) / "mode.json"
        panel.export_file()
        saved = json.loads(panel.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["execution_mode"], "detail")
        self.assertNotIn("fields_json", panel.path.read_text(encoding="utf-8"))
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("line"))
        self.assertTrue(panel.run_action.isEnabled())
        self.assertEqual(json.loads(Path("config.json").read_text()), self.config)

    def test_line_execution_no_longer_depends_on_presence_of_task_nodes(self):
        panel = self.panel()
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("line"))
        self.assertTrue(panel.is_line_mode())
        self.bind_tasks(panel)
        with mock.patch.object(panel, "run_tasks") as run_rows:
            panel.run()
        run_rows.assert_called_once()

    def test_detail_submits_once_and_preview_does_not_take_retained_row(self):
        panel = self.panel(task=True)
        self.bind_tasks(panel)
        panel.new_export_example()
        self.assertEqual(panel.mode_combo.currentData(), "detail")
        source = Path(self.temp.name) / "source.mp4"
        source.write_bytes(b"synthetic")
        subtitle = source.with_suffix(".srt")
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nAhoj.\n", encoding="utf-8")
        panel.graph["nodes"][0]["params"].update(video=str(source), subtitles=str(subtitle))
        output = next(node for node in panel.graph["nodes"] if node["kind"] == "core.export")
        output["params"]["output"] = str(source.with_name("final.mp4"))
        handle = mock.Mock()
        handle.isRunning.return_value = True
        with mock.patch("PYUI.node_workspace.resolve_tools", return_value=("ffmpeg", "ffprobe")), \
             mock.patch.object(panel.host.background_tasks, "submit", return_value=handle) as submit:
            panel.run()
        self.assertEqual(submit.call_count, 1)
        self.assertIsNone(submit.call_args.kwargs["payload"]["task"])
        self.assertEqual(submit.call_args.kwargs["payload"]["graph"]["execution_mode"], "detail")
        panel.add_node("core.number")
        preview = panel.graph["nodes"][-1]
        with mock.patch.object(panel.host.background_tasks, "submit", return_value=handle) as preview_submit:
            panel.preview_node(preview["id"])
        self.assertIsNone(preview_submit.call_args.kwargs["payload"]["task"])

    def test_parameters_and_output_path_stay_in_workflow_not_global_config(self):
        panel = self.panel()
        data = panel.graph["nodes"][1]
        panel.set_parameter(data,"size",42)
        self.assertTrue(panel.dirty)
        self.assertEqual(data["params"]["size"],42)
        self.assertEqual(json.loads(Path("config.json").read_text()),self.config)

    def bind_tasks(self, panel, count=2):
        root = Path(self.temp.name)
        self.window.loaded_project_dir = root
        targets = []
        for index in range(count):
            directory = root / "口播" / str(index + 1)
            directory.mkdir(parents=True)
            (directory / "01.mp4").write_bytes(b"synthetic")
            targets.append({"task": SimpleNamespace(task_id=str(index + 1), task_type="口播",
                task_audio_text=f"文案 {index + 1}", raw={"新增列": "保留"}),
                "label": str(index + 1), "target_dir": str(directory)})
        with mock.patch.object(self.window, "selectedTaskRows", return_value=list(range(count))), \
             mock.patch.object(self.window, "taskTargetsForRows", return_value=targets), \
             mock.patch.object(self.window, "detectTaskLanguage", return_value="sk"):
            self.assertTrue(panel.bind_selected_tasks())
        return targets

    def wait_tasks(self, panel, timeout=5):
        deadline = time.monotonic() + timeout
        while any(handle.isRunning() for handle in panel.task_handles.values()) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.settle()
        self.assertFalse(any(handle.isRunning() for handle in panel.task_handles.values()))

    def test_default_task_entry_binds_all_fields_without_file_dialog(self):
        panel = self.panel(task=True)
        self.assertEqual(panel.graph["nodes"][0]["kind"], "core.task")
        self.assertFalse(panel.run_action.isEnabled())
        with mock.patch.object(QtWidgets.QFileDialog, "getOpenFileName", side_effect=AssertionError("不应选择文件")):
            targets = self.bind_tasks(panel)
        self.assertEqual(len(panel.bound_tasks), 2)
        self.assertEqual(panel.bound_tasks[0].fields["raw"], {"新增列": "保留"})
        targets[0]["task"].task_audio_text = "changed"
        self.assertEqual(panel.bound_tasks[0].fields["task_audio_text"], "文案 1")
        self.assertTrue(panel.run_action.isEnabled())
        self.assertEqual(len(panel.graph["nodes"]), 6)
        self.assertEqual(panel.graph["nodes"][-1]["kind"], "core.export")
        task_node = panel.scene.nodes[panel.graph["nodes"][0]["id"]]
        self.assertIn((True, "task_path"), task_node.ports)
        self.assertIn((True, "task_audio_text"), task_node.ports)
        self.assertNotIn((True, "task"), task_node.ports)

    def test_middle_mouse_pans_canvas_over_node_without_moving_nodes(self):
        panel = self.panel(task=True)
        panel.view.resetTransform()
        node = panel.scene.nodes[panel.graph["nodes"][0]["id"]]
        panel.view.centerOn(node.scenePos() + QtCore.QPointF(120, 110))
        self.settle()
        before = panel.view.mapToScene(panel.view.viewport().rect().center())
        positions = [list(item["position"]) for item in panel.graph["nodes"]]
        start = panel.view.viewport().rect().center()
        QTest.mousePress(panel.view.viewport(), QtCore.Qt.MouseButton.MiddleButton, pos=start)
        QTest.mouseMove(panel.view.viewport(), start + QtCore.QPoint(80, 40))
        QTest.mouseRelease(panel.view.viewport(), QtCore.Qt.MouseButton.MiddleButton, pos=start + QtCore.QPoint(80, 40))
        self.settle()
        after = panel.view.mapToScene(panel.view.viewport().rect().center())
        self.assertAlmostEqual(before.x() - after.x(), 80, delta=2)
        self.assertAlmostEqual(before.y() - after.y(), 40, delta=2)
        self.assertEqual(positions, [item["position"] for item in panel.graph["nodes"]])
        self.assertIsNone(panel.view._pan_position)

    def test_middle_pan_extends_canvas_beyond_initial_bounds(self):
        panel = self.panel()
        view = panel.view
        initial = view.sceneRect()
        view.resetTransform()
        view.centerOn(initial.topLeft())
        start = view.viewport().rect().center()
        QTest.mousePress(view.viewport(), QtCore.Qt.MouseButton.MiddleButton, pos=start)
        for index in range(4):
            QTest.mouseMove(view.viewport(), start + QtCore.QPoint(50 + index * 30, 50 + index * 25))
        QTest.mouseRelease(view.viewport(), QtCore.Qt.MouseButton.MiddleButton, pos=start)
        self.assertLess(view.sceneRect().left(), initial.left())
        self.assertLess(view.sceneRect().top(), initial.top())

    def test_long_middle_drag_has_no_cumulative_drift_at_multiple_scales(self):
        panel = self.panel()
        view = panel.view
        positions = [list(n["position"]) for n in panel.graph["nodes"]]
        for scale in (.31, .73, 1.0, 2.4):
            view.resetTransform()
            view.scale(scale, scale)
            view.centerOn(0, 0)
            before = view.mapToScene(view.viewport().rect().center())
            start = view.viewport().rect().center()
            global_start = QtCore.QPointF(view.viewport().mapToGlobal(start))
            QTest.mousePress(view.viewport(), QtCore.Qt.MouseButton.MiddleButton, pos=start)
            for index in range(800):
                delta = QtCore.QPointF((index % 79) * 2 - 70, (index % 53) - 20)
                event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(start) + delta,
                    global_start + delta, QtCore.Qt.MouseButton.NoButton,
                    QtCore.Qt.MouseButton.MiddleButton, QtCore.Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(view.viewport(), event)
                wanted = before - delta / scale
                actual = view.mapToScene(view.viewport().rect().center())
                self.assertAlmostEqual(actual.x(), wanted.x(), delta=2 / scale)
                self.assertAlmostEqual(actual.y(), wanted.y(), delta=2 / scale)
            QTest.mouseMove(view.viewport(), start)
            QTest.mouseRelease(view.viewport(), QtCore.Qt.MouseButton.MiddleButton, pos=start)
            actual = view.mapToScene(view.viewport().rect().center())
            self.assertAlmostEqual(actual.x(), before.x(), delta=2 / scale)
            self.assertAlmostEqual(actual.y(), before.y(), delta=2 / scale)
        self.assertEqual(positions, [n["position"] for n in panel.graph["nodes"]])

    def test_dragging_wire_released_on_blank_cancels_without_affecting_existing_connections(self):
        panel = self.panel()
        first = panel.scene.nodes[panel.graph["nodes"][0]["id"]].ports[(True, "video")]
        before = list(panel.graph["connections"])
        start = panel.view.mapFromScene(first.scenePos())
        end = start + QtCore.QPoint(75, -45)
        QTest.mousePress(panel.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(panel.view.viewport(), end)
        QTest.mouseRelease(panel.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=end)
        self.settle()
        self.assertIsNone(panel.scene.pending)
        self.assertIsNone(panel.scene.wire)
        self.assertEqual(panel.graph["connections"], before)

    def test_click_wire_is_cancellable_by_blank_right_click_or_focus_loss(self):
        panel = self.panel()
        for mode in ("blank", "right", "focus"):
            first = panel.scene.nodes[panel.graph["nodes"][0]["id"]].ports[(True, "video")]
            start = panel.view.mapFromScene(first.scenePos())
            QTest.mouseClick(panel.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
            self.assertIsNotNone(panel.scene.pending)
            if mode == "focus":
                self.app.sendEvent(panel.view, QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut))
            else:
                QTest.mouseClick(panel.view.viewport(), QtCore.Qt.MouseButton.RightButton if mode == "right" else QtCore.Qt.MouseButton.LeftButton,
                                 pos=QtCore.QPoint(12, 12))
            self.assertIsNone(panel.scene.pending)
            self.assertIsNone(panel.scene.wire)

    def test_explicit_export_example_and_grouped_node_menu(self):
        panel = self.panel()
        panel.new_export_example()
        self.assertIn("core.settle", [n["kind"] for n in panel.graph["nodes"]])
        output = next(n for n in panel.graph["nodes"] if n["id"] == panel.graph["output_node"])
        self.assertEqual(output["kind"], "core.export")
        self.assertEqual(output["params"]["destination"], "file")
        nodes_menu = next(a.menu() for a in panel.menu.actions() if a.text() == "节点")
        self.assertTrue(all(a.menu() is not None for a in nodes_menu.actions()
                            if not a.isSeparator() and not a.text().startswith("查找／添加节点")))
        self.assertIn("预览", [a.text() for a in nodes_menu.actions()])

    def test_preview_submits_only_selected_ancestors_without_export_or_final_path(self):
        panel = self.panel()
        panel.add_node("core.number")
        node = panel.graph["nodes"][-1]
        node["params"]["value"] = 239
        handle = mock.Mock()
        handle.isRunning.return_value = True
        with mock.patch.object(panel.host.background_tasks, "submit", return_value=handle) as submit:
            panel.preview_node(node["id"])
        call = submit.call_args.kwargs
        self.assertEqual(call["payload"]["preview_node"], node["id"])
        self.assertEqual(call["resources"], ())
        self.assertEqual(call["owner"], "node_workflow")
        panel.cancel()
        handle.requestInterruption.assert_called_once()
        node["params"]["value"] = 1
        self.assertEqual(next(n for n in call["payload"]["graph"]["nodes"] if n["id"] == node["id"])["params"]["value"], 239)

    def test_preview_dialog_displays_plain_text_and_aspect_preserving_image(self):
        from PYUI.node_preview import NodePreviewDialog
        image = QtGui.QImage(240, 320, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor("red"))
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        import base64
        dialog = NodePreviewDialog({"title": "测试", "text": "<b>原始文本</b>",
                                   "png": base64.b64encode(bytes(buffer.data())).decode("ascii"), "time": 1}, self.window)
        dialog.show()
        self.settle()
        self.assertEqual(dialog.findChild(QtWidgets.QPlainTextEdit).toPlainText(), "<b>原始文本</b>")
        from PYUI.node_preview import PreviewImage
        frame = dialog.findChild(PreviewImage).pixmap()
        self.assertAlmostEqual(frame.width() / frame.height(), .75, delta=.01)
        dialog.close()

    def test_invalid_preview_result_is_reported_without_escaping_qt_slot(self):
        panel = self.panel()
        panel.show_preview({"title": "坏数据", "text": "test", "png": "!not-base64!"})
        self.assertIn("显示预览失败", panel.status.text())
        self.assertEqual(panel.preview_dialogs, [])

    def test_out_of_range_numeric_parameters_remain_invalid_without_crashing_inspector(self):
        panel = self.panel()
        panel.add_node("core.number")
        node = panel.graph["nodes"][-1]
        node["params"]["value"] = 10**300
        panel.inspect_selection()
        self.assertEqual(node["params"]["value"], 10**300)

    def test_task_batch_failure_does_not_block_other_task_or_require_review(self):
        from model.BackgroundTasks import BackgroundTaskResultError
        panel = self.panel(task=True)
        self.bind_tasks(panel)
        def execute(payload, context, **kwargs):
            label = payload["task"]["label"]
            if label == "1":
                raise BackgroundTaskResultError("synthetic error", {"status": "failed", "task_label": label,
                    "error": "synthetic error", "failed_node": {"title": "智能剪辑"}, "paths": []})
            return {"status": "completed_with_notes", "task_label": label, "notes": [{"detail": "保留不确定片段"}],
                    "video": str(Path(payload["task"]["directory"]) / "result.mp4"), "paths": []}
        with mock.patch("PYUI.node_workspace.resolve_tools", return_value=("ffmpeg", "ffprobe")), \
             mock.patch("PYUI.node_workspace.run_task_workflow", side_effect=execute), \
             mock.patch.object(QtWidgets.QMessageBox, "exec", side_effect=AssertionError("不应要求确认")):
            panel.run()
            self.wait_tasks(panel)
        rows = [panel.results.topLevelItem(index) for index in range(2)]
        self.assertEqual([item.text(1) for item in rows], ["失败", "完成 · 有提醒"])
        self.assertEqual(rows[0].data(0, QtCore.Qt.ItemDataRole.UserRole)["failed_node"]["title"], "智能剪辑")
        self.assertEqual(len(panel.task_handles), 2)

    def test_global_loop_data_is_assigned_per_submission_not_source_row(self):
        from dataclasses import replace
        from model.node_workflow.scope import initialize_execution
        panel = self.panel(task=True)
        self.bind_tasks(panel)
        panel.bound_tasks = [replace(task, source_row=row) for task, row in zip(panel.bound_tasks, (20, 35))]
        received = []
        def execute(payload, context, **_kwargs):
            initialize_execution(payload, context)
            values = panel.registry.definitions["core.global_data"].build({}, {}, context)
            received.append((payload["task"]["source_row"], values))
            return {"status": "completed", "task_label": payload["task"]["label"], "paths": []}
        with mock.patch("PYUI.node_workspace.resolve_tools", return_value=("ffmpeg", "ffprobe")), \
             mock.patch("PYUI.node_workspace.run_task_workflow", side_effect=execute):
            panel.run()
            self.wait_tasks(panel)
            panel.run()
            self.wait_tasks(panel)
        for batch in (received[:2], received[2:]):
            values = sorted(batch, key=lambda item: item[0])
            self.assertEqual([item[0] for item in values], [20, 35])
            self.assertEqual([item[1]["iteration"] for item in values], [1, 2])
            self.assertEqual([item[1]["iteration_count"] for item in values], [2, 2])
            self.assertEqual(len({item[1]["batch_id"] for item in values}), 1)
        self.assertNotEqual(received[0][1]["batch_id"], received[2][1]["batch_id"])

    def test_global_node_inspector_and_preview_show_first_iteration_and_total(self):
        from model.node_workflow.scope import initialize_execution
        panel = self.panel(task=True)
        self.bind_tasks(panel)
        panel.add_node("core.global_data")
        node = panel.graph["nodes"][-1]
        self.settle()
        tables = panel.inspector.widget().findChildren(QtWidgets.QTreeWidget)
        self.assertEqual(len(tables), 1)
        shown = {tables[0].topLevelItem(index).text(0): tables[0].topLevelItem(index).text(1)
                 for index in range(tables[0].topLevelItemCount())}
        self.assertEqual(shown["当前循环次数 · 从 1 开始"], "1")
        self.assertEqual(shown["本次循环总数"], "2")
        handle = mock.Mock()
        handle.isRunning.return_value = True
        with mock.patch.object(panel.host.background_tasks, "submit", return_value=handle) as submit:
            panel.preview_node(node["id"])
        payload = submit.call_args.kwargs["payload"]
        context = SimpleNamespace()
        initialize_execution(payload, context)
        self.assertEqual(context.workflow_globals.iteration, 1)
        self.assertEqual(context.workflow_globals.iteration_count, 2)
        self.assertTrue(context.workflow_globals.is_preview)
        self.assertEqual(json.loads(Path("config.json").read_text()), self.config)

    def test_detail_global_preview_ignores_retained_line_batch(self):
        panel = self.panel(task=True)
        self.bind_tasks(panel)
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("detail"))
        values = panel.global_preview_snapshot().values()
        self.assertEqual(values["iteration"], 1)
        self.assertEqual(values["iteration_count"], 1)
        self.assertEqual(values["mode"], "detail")

    def test_queued_task_cancel_and_hide_panel_do_not_cancel_other_task(self):
        panel = self.panel(task=True)
        self.bind_tasks(panel)
        manager = self.window.plugin_host.background_tasks
        manager.kind_limits["media"] = 1
        started, release = threading.Event(), threading.Event()
        def execute(payload, context, **kwargs):
            started.set()
            while not release.wait(.01):
                context.check_canceled()
            return {"status": "completed", "task_label": payload["task"]["label"], "paths": []}
        try:
            with mock.patch("PYUI.node_workspace.resolve_tools", return_value=("ffmpeg", "ffprobe")), \
                 mock.patch("PYUI.node_workspace.run_task_workflow", side_effect=execute):
                panel.run()
                deadline = time.monotonic() + 3
                while not started.is_set() and time.monotonic() < deadline:
                    self.settle()
                    time.sleep(.01)
                self.assertTrue(started.is_set())
                handles = list(panel.task_handles.values())
                self.assertEqual(manager.get(handles[1].task_id)["status"], "queued")
                handles[1].requestInterruption()
                self.window.node_workspace.frame.close()
                self.assertTrue(handles[0].isRunning())
                release.set()
                self.wait_tasks(panel)
        finally:
            release.set()
        self.assertEqual(panel.results.topLevelItem(0).text(1), "完成")
        self.assertEqual(panel.results.topLevelItem(1).text(1), "已取消")

    def test_preview_is_dedicated_node_not_button_on_every_node(self):
        panel = self.panel()
        panel.add_node("core.number")
        source = panel.graph["nodes"][-1]
        self.settle()
        self.assertFalse(panel.preview_action.isEnabled())
        self.assertFalse(any("预览" in button.text() for button in panel.inspector.findChildren(QtWidgets.QPushButton)))
        panel.add_node("core.preview")
        preview = panel.graph["nodes"][-1]
        panel.graph["connections"].append([source["id"], "number", preview["id"], "value"])
        panel.scene.rebuild()
        panel.scene.nodes[preview["id"]].setSelected(True)
        self.settle()
        self.assertTrue(panel.preview_action.isEnabled())
        buttons = [b for b in panel.inspector.findChildren(QtWidgets.QPushButton) if b.text() == "打开预览内容"]
        self.assertEqual(len(buttons), 1)
        handle = mock.Mock()
        handle.isRunning.return_value = True
        with mock.patch.object(panel.host.background_tasks, "submit", return_value=handle) as submit:
            buttons[0].click()
        payload = submit.call_args.kwargs["payload"]
        self.assertEqual(payload["preview_node"], preview["id"])
        self.assertEqual(set(n["id"] for n in payload["graph"]["nodes"]) & {source["id"], preview["id"]}, {source["id"], preview["id"]})
        self.assertFalse(panel.preview_action.isEnabled())

    def test_style_menu_follows_loaded_graph_and_saves_without_global_config_write(self):
        panel = self.panel()
        panel.style_actions["rounded"].trigger()
        self.assertEqual(panel.graph["view"]["connection_style"], "rounded")
        self.assertTrue(panel.dirty)
        panel.path = Path(self.temp.name) / "saved.workflow.json"
        panel.export_file()
        saved = json.loads(panel.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["view"]["connection_style"], "rounded")
        panel.replace_graph(saved)
        self.assertTrue(panel.style_actions["rounded"].isChecked())
        self.assertFalse(panel.style_actions["smooth"].isChecked())
        saved.pop("view")
        panel.replace_graph(saved)
        self.assertTrue(panel.style_actions["smooth"].isChecked())
        self.assertEqual(json.loads(Path("config.json").read_text()), self.config)

    def test_menu_has_one_polymorphic_switch_and_preview_not_type_variants(self):
        panel = self.panel()
        nodes_menu = next(a.menu() for a in panel.menu.actions() if a.text() == "节点")
        def titles_in(menu):
            return [title for action in menu.actions() for title in
                    (titles_in(action.menu()) if action.menu() else [action.text()])]
        titles = titles_in(nodes_menu)
        self.assertEqual([title for title in titles if title.startswith("条件选择")], ["条件选择"])
        self.assertEqual([title for title in titles if title.startswith("预览")], ["预览"])

    def test_path_nodes_have_separate_category_and_text_judgment_is_visible(self):
        panel = self.panel()
        nodes_menu = next(a.menu() for a in panel.menu.actions() if a.text() == "节点")
        groups = {action.text(): action.menu() for action in nodes_menu.actions() if action.menu()}
        self.assertIn('路径处理', groups)
        paths = [action.text() for action in groups['路径处理'].actions()]
        self.assertIn('文件名 · basename', paths)
        self.assertIn('父目录 · dirname', paths)
        self.assertIn('路径判断 · 包含／通配符', paths)
        native_groups = {a.text(): a.menu() for a in groups['Python 原生'].actions() if a.menu()}
        texts = [action.text() for action in native_groups['逻辑与判断'].actions()]
        self.assertIn('文本判断 · 包含／开头／结尾', texts)
        files = [action.text() for action in groups['任务与文件'].actions()]
        self.assertNotIn('文件名 · basename', files)
        panel.add_node('core.compress')
        self.settle()
        definition = panel.registry.definitions['core.compress']
        enabled = next(port for port in definition.inputs if port.key == 'enabled')
        self.assertFalse(enabled.required)
        self.assertEqual(enabled.data_type, 'boolean')

    def test_task_workflow_right_click_submenu_is_available(self):
        menu = QtWidgets.QMenu()
        self.window.node_workspace.populate_task_menu(menu, [0, 1])
        submenu = menu.actions()[0].menu()
        self.assertEqual(submenu.title(), "节点工作流")
        self.assertEqual([action.text() for action in submenu.actions()],
                         ["载入所选任务", "运行所选任务（当前工作流）"])

    def test_python_native_menu_and_search_catalog(self):
        panel = self.panel()
        nodes = next(a.menu() for a in panel.menu.actions() if a.text() == "节点")
        native = next(a.menu() for a in nodes.actions() if a.text() == "Python 原生")
        self.assertEqual(len(native.actions()), 10)
        sections = {a.text(): a.menu() for a in native.actions() if a.menu()}
        self.assertIn("流程控制", sections)
        self.assertNotIn("流程控制", [a.text() for a in nodes.actions()])
        control = {a.text(): a.menu() for a in sections["流程控制"].actions() if a.menu()}
        self.assertIn("添加成对循环块 · Begin + End", [a.text() for a in control["Begin · End 循环块"].actions()])
        panel.show_node_search(QtCore.QPointF(120, 160))
        picker = panel.node_picker
        picker.search.setText("list_get")
        self.settle()
        self.assertEqual(sum(not item.isHidden() for item, _terms in picker.entries), 1)
        picker.choose()
        self.settle()
        self.assertEqual(panel.graph["nodes"][-1]["kind"], "core.py_list_get")
        self.assertEqual(panel.graph["nodes"][-1]["position"], [120, 160])

    def test_batch_data_example_menu_and_result(self):
        from model.node_workflow.graph import evaluate_graph
        from model.BackgroundTasks import TaskRunContext
        panel = self.panel()
        files = next(a.menu() for a in panel.menu.actions() if a.text() == "文件")
        action = next(a for a in files.actions() if a.text() == "新建 Python 批量分组示例")
        with mock.patch.object(panel, "discard_confirmed", return_value=True):
            action.trigger()
        self.settle()
        context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        value = evaluate_graph(panel.graph, panel.registry, context)["value"].value
        self.assertEqual(value, {"Maria": [{"admin": "Maria", "id": 1}, {"admin": "Maria", "id": 3}],
                                 "John": [{"admin": "John", "id": 2}]})

    def test_tab_opens_search_without_changing_graph(self):
        panel = self.panel()
        count = len(panel.graph["nodes"])
        panel.view.setFocus()
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_Tab)
        self.settle()
        self.assertIsNotNone(panel.node_picker)
        self.assertTrue(panel.node_picker.isVisible())
        panel.node_picker.search.setText("不存在的节点abcdef")
        self.assertTrue(all(item.isHidden() for item, _terms in panel.node_picker.entries))
        panel.node_picker.choose()
        self.assertEqual(len(panel.graph["nodes"]), count)
        panel.node_picker.close()

    def test_add_delete_undo_redo_preserve_connections_and_ids(self):
        panel = self.panel()
        original = json.loads(json.dumps(panel.graph))
        panel.add_node("core.py_range")
        added = panel.graph["nodes"][-1]["id"]
        self.assertTrue(panel.undo_action.isEnabled())
        panel.undo()
        self.assertEqual(panel.graph, original)
        self.assertFalse(panel.dirty)
        panel.redo()
        self.assertEqual(panel.graph["nodes"][-1]["id"], added)
        caption = panel.graph["nodes"][1]["id"]
        edges = list(panel.graph["connections"])
        panel.scene.remove_node(caption)
        # Undo also flushes pending refresh before it restores a snapshot.
        panel.undo()
        self.settle()
        self.assertIn(caption, panel.scene.nodes)
        self.assertEqual(panel.graph["connections"], edges)
        panel.redo()
        self.assertNotIn(caption, panel.scene.nodes)

    def test_parameter_changes_merge_but_save_keeps_clean_state(self):
        panel = self.panel()
        panel.add_node("core.number")
        number = panel.graph["nodes"][-1]
        panel.set_parameter(number, "value", 10)
        panel.set_parameter(number, "value", 20)
        self.assertEqual(panel.history.stack.count(), 2)
        panel.undo()
        current = panel.graph["nodes"][-1]
        self.assertEqual(current["params"]["value"], 0)
        panel.redo()
        self.assertEqual(panel.graph["nodes"][-1]["params"]["value"], 20)
        panel.path = Path(self.temp.name) / "native-undo.json"
        with mock.patch('PYUI.node_workspace.QtWidgets.QInputDialog.getText', return_value=('撤销测试', True)):
            panel.save_file()
        self.assertFalse(panel.dirty)
        panel.undo()
        self.assertTrue(panel.dirty)
        panel.redo()
        self.assertFalse(panel.dirty)
        self.assertNotIn("history", json.loads(Path(panel.path).read_text(encoding="utf-8")))

    def test_keyboard_undo_redo_and_replacing_graph_clears_history(self):
        panel = self.panel()
        panel.add_node("core.py_json")
        added = panel.graph["nodes"][-1]["id"]
        panel.view.setFocus()
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.settle()
        self.assertNotIn(added, panel.scene.nodes)
        QTest.keyClick(panel.view, QtCore.Qt.Key.Key_Y, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.settle()
        self.assertIn(added, panel.scene.nodes)
        graph = json.loads(json.dumps(panel.graph))
        panel.replace_graph(graph)
        self.assertFalse(panel.history.stack.canUndo())
        self.assertFalse(panel.dirty)

    def test_connection_and_view_style_are_undoable(self):
        panel = self.panel()
        caption = panel.graph["nodes"][1]["id"]
        source = panel.graph["nodes"][0]["id"]
        edge = [source, "subtitles", caption, "subtitles"]
        panel.graph["connections"].remove(edge)
        panel.replace_graph(panel.graph)
        panel.scene.port_pressed(panel.scene.nodes[source].ports[(True, "subtitles")])
        panel.scene.port_pressed(panel.scene.nodes[caption].ports[(False, "subtitles")])
        self.settle()
        self.assertIn(edge, panel.graph["connections"])
        panel.undo()
        self.assertNotIn(edge, panel.graph["connections"])
        panel.redo()
        self.assertIn(edge, panel.graph["connections"])
        panel.scene.set_connection_style("straight")
        panel.undo()
        self.assertEqual(panel.scene.connection_style(), "smooth")
        panel.redo()
        self.assertEqual(panel.scene.connection_style(), "straight")

    def test_moved_node_undo_does_not_retain_graphics_objects(self):
        panel = self.panel()
        node = panel.graph["nodes"][1]
        original = list(node["position"])
        node["position"] = [800, 200]
        panel.scene.rebuild()
        panel.changed("移动节点")
        panel.undo()
        self.assertEqual(panel.graph["nodes"][1]["position"], original)
        panel.redo()
        self.assertEqual(panel.graph["nodes"][1]["position"], [800, 200])
        # A previously retained inspector node dict is safely rebound by ID.
        panel.set_parameter(node, "font_size", 45)
        self.assertEqual(panel.graph["nodes"][1]["params"]["font_size"], 45)

    def test_plugin_function_can_use_same_registry_and_category(self):
        from app_plugins.api import node_function
        from app_plugins.host import PluginContext
        @node_function(kind="sample_function.echo", title="测试函数", inputs=(NodePort("text", "文本", "text"),),
            outputs=(NodePort("text", "文本", "text"),), category="示例插件/文本")
        def echo(text):
            return text
        context = PluginContext(self.window.plugin_host, "sample_function")
        context.register_function(echo)
        self.assertEqual(self.window.plugin_host.node_registry.definitions["sample_function.echo"].category, "示例插件/文本")
        self.assertEqual(json.loads(Path("config.json").read_text()), self.config)

    def test_python_example_submits_data_job_and_opens_result(self):
        panel = self.panel()
        panel.new_python_example()
        self.assertTrue(panel.run_button.isEnabled())
        self.assertEqual(panel.run_button.text(), "执行数据流程")
        handle = mock.Mock()
        manager = panel.host.background_tasks
        with mock.patch.object(manager, "submit", return_value=handle) as submit, \
             mock.patch("PYUI.node_workspace.resolve_tools", side_effect=AssertionError("不要解析FFmpeg")):
            panel.run()
        self.assertEqual(submit.call_args.kwargs["kind"], "cpu")
        self.assertEqual(submit.call_args.kwargs["resources"], ())
        result = {"result_kind": "data", "title": "数据结果", "text": '["Maria_001.mp4"]', "png": "", "time": None, "task_label": ""}
        with mock.patch.object(panel, "show_preview") as show:
            panel.on_completed(result)
        show.assert_called_once_with(result)
        self.assertEqual(panel.progress.value(), 100)
        self.assertIsNone(panel.last_result)


if __name__ == "__main__": unittest.main()
