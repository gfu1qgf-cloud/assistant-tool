import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from threading import Event

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets, QtTest
from model.TaskTableSchema import normalize_task_table_schema
from model.TaskSchemaMapping import SchemaMapping, read_columns, display_name, saved_display_names
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from PYUI.task_schema_canvas import SchemaMappingPanel
from PYUI.task_schema_node_pyui import TaskTableSchemaNodeDialog


def schema():
    return normalize_task_table_schema({'sheet_name': '任务', 'sheet_index': 2, 'header_row': 1,
        'header_scan_rows': 35, 'labels': {'task_id': '任务编号', 'task_type': '类型', 'task_name': '标题'},
        'fields': {'task_id': {'aliases': ['编号', '旧编号'], 'default': '{row}'},
                   'task_type': {'aliases': ['类型']}, 'task_name': {'aliases': ['标题'], 'default': '{task_id}'}},
        'submission_sheet': {'header_scan_rows': 18, 'fields': {'chinese': {'aliases': ['标题']}}}})


class MappingModelTests(unittest.TestCase):
    def test_generic_translation_preserves_internal_keys_and_private_names(self):
        original = schema()
        original['labels']['admin'] = '用户自定称呼'
        result = saved_display_names(original)
        self.assertEqual(result['labels']['task_audio_text'], '配音文案')
        self.assertEqual(result['labels']['admin'], '用户自定称呼')
        self.assertEqual(result['fields'], original['fields'])
        self.assertEqual(set(result['fields']), set(original['fields']))
        self.assertEqual(original['labels']['task_audio_text'], 'task_audio_text')
    def test_round_trip_retains_all_old_settings(self):
        original = schema()
        for submission in (False, True):
            self.assertEqual(SchemaMapping(original, submission).compile(), original)

    def test_one_column_can_supply_multiple_fields_and_priority_is_ordered(self):
        doc = SchemaMapping(schema())
        doc.connect('标题', 'task_audio_text')
        doc.connect('标题', 'task_notes')
        self.assertEqual(doc.aliases('task_id'), ['编号', '旧编号'])
        self.assertEqual(doc.aliases('task_audio_text'), ['标题'])
        self.assertEqual(doc.aliases('task_notes')[-1], '标题')

    def test_duplicate_and_unknown_connection_rejected_without_mutation(self):
        doc = SchemaMapping(schema())
        before = doc.compile()
        for source, field in [('编号', 'task_id'), ('missing', 'task_name'), ('标题', 'missing')]:
            with self.assertRaises(ValueError):
                doc.connect(source, field)
        self.assertEqual(doc.compile(), before)

    def test_rename_preserves_connections_and_does_not_change_other_mapping(self):
        doc = SchemaMapping(schema())
        doc.connect('标题', 'task_audio_text')
        original_submission = copy.deepcopy(doc.schema['submission_sheet'])
        doc.rename_source('标题', '新标题')
        self.assertEqual(doc.aliases('task_name'), ['新标题'])
        self.assertEqual(doc.aliases('task_audio_text'), ['新标题'])
        self.assertEqual(doc.schema['submission_sheet'], original_submission)

    def test_remove_disconnects_only_the_named_source(self):
        doc = SchemaMapping(schema())
        doc.remove_source('编号')
        self.assertEqual(doc.aliases('task_id'), ['旧编号'])
        self.assertEqual(doc.aliases('task_name'), ['标题'])

    def test_editor_mutations_never_change_input_schema(self):
        original = schema()
        frozen = copy.deepcopy(original)
        doc = SchemaMapping(original)
        doc.rename_source('编号', '新编号')
        self.assertEqual(original, frozen)

    def test_renaming_to_existing_or_invalid_name_keeps_model(self):
        doc = SchemaMapping(schema())
        before = doc.compile()
        for value in ('标题', '', '\n'):
            with self.assertRaises(ValueError):
                doc.rename_source('编号', value)
        self.assertEqual(doc.compile(), before)


class MappingUiTests(unittest.TestCase):
    def test_field_names_are_chinese_and_visually_editable(self):
        panel = self.dialog.task_panel
        key = 'task_audio_text'
        panel.field.setCurrentIndex(panel.field.findData(key))
        self.assertEqual(panel.field.currentText(), '配音文案')
        panel.rename_field('朗读正文')
        self.assertEqual(self.dialog.schema()['labels'][key], '朗读正文')
        self.assertIn((False, key), panel.scene.nodes['structure'].ports)
        self.assertEqual(panel.scene.nodes['structure'].ports[(False, key)].port.title, '朗读正文')
        self.assertEqual(panel.field.currentData(), key)
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.dialog = TaskTableSchemaNodeDialog(schema())
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        if self.dialog.handle is not None:
            self.dialog.cancel_reading()
            self.wait_read()
        self.dialog.reject()
        self.dialog.deleteLater()
        self.app.processEvents()

    def wait_read(self):
        deadline = time.monotonic() + 5
        while self.dialog.handle is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.dialog.handle)

    def test_both_modes_have_two_types_and_one_structure_and_round_trip(self):
        self.assertEqual(self.dialog.schema(), schema())
        for panel in (self.dialog.task_panel, self.dialog.submission_panel):
            self.assertEqual(sum(node['id'] == 'structure' for node in panel.scene.graph['nodes']), 1)
            self.assertTrue(panel.filter_nodes())
            self.assertEqual(len(panel.scene.registry.definitions), 2)
            self.assertTrue(all(not port.multiple for port in panel.scene.nodes['structure'].inputs))

    def test_actual_port_gesture_connects_and_rejects_duplicate(self):
        panel = self.dialog.task_panel
        identifier = next(node['id'] for node in panel.filter_nodes() if node['params']['aliases'] == ['标题'])
        panel.scene.port_pressed(panel.scene.nodes[identifier].ports[(True, 'value')])
        panel.scene.port_pressed(panel.scene.nodes['structure'].ports[(False, 'task_audio_text')])
        self.app.processEvents()
        self.assertEqual(self.dialog.schema()['fields']['task_audio_text']['aliases'], ['标题'])
        panel.scene.port_pressed(panel.scene.nodes[identifier].ports[(True, 'value')])
        panel.scene.port_pressed(panel.scene.nodes['structure'].ports[(False, 'task_audio_text')])
        self.app.processEvents()
        self.assertIn('已经连接', panel.status.text())

    def test_a_second_filter_cannot_feed_an_occupied_field(self):
        panel = self.dialog.task_panel
        identifier = panel.add_filter_record(['其他编号', '备用编号'])
        before = self.dialog.schema()
        panel.scene.port_pressed(panel.scene.nodes[identifier].ports[(True, 'value')])
        panel.scene.port_pressed(panel.scene.nodes['structure'].ports[(False, 'task_id')])
        self.app.processEvents()
        self.assertEqual(self.dialog.schema(), before)
        self.assertIn('先断开', panel.status.text())

    def test_editing_shared_filter_updates_only_its_connected_fields(self):
        panel = self.dialog.task_panel
        identifier = next(node['id'] for node in panel.filter_nodes() if node['params']['aliases'] == ['标题'])
        panel.scene.port_pressed(panel.scene.nodes[identifier].ports[(True, 'value')])
        panel.scene.port_pressed(panel.scene.nodes['structure'].ports[(False, 'task_audio_text')])
        self.app.processEvents()
        with patch.object(panel, 'aliases_dialog', return_value=['新标题', '标题']):
            panel.edit_filter(identifier)
        result = self.dialog.schema()
        self.assertEqual(result['fields']['task_name']['aliases'], ['新标题', '标题'])
        self.assertEqual(result['fields']['task_audio_text']['aliases'], ['新标题', '标题'])
        self.assertEqual(result['fields']['task_id']['aliases'], ['编号', '旧编号'])

    def test_deleting_a_filter_does_not_delete_structure_or_other_mappings(self):
        panel = self.dialog.task_panel
        identifier = next(node['id'] for node in panel.filter_nodes() if node['params']['aliases'] == ['标题'])
        panel.scene.remove_node(identifier)
        self.app.processEvents()
        self.assertIn('structure', panel.scene.nodes)
        self.assertEqual(self.dialog.schema()['fields']['task_name']['aliases'], [])
        self.assertEqual(self.dialog.schema()['fields']['task_id']['aliases'], ['编号', '旧编号'])

    def test_real_mouse_clicks_connect_ports(self):
        panel = self.dialog.task_panel
        identifier = next(node['id'] for node in panel.filter_nodes() if node['params']['aliases'] == ['编号', '旧编号'])
        panel.field.setCurrentIndex(panel.field.findData('task_name'))
        panel.unlink_field()
        for output, node, port in ((True, identifier, 'value'), (False, 'structure', 'task_name')):
            item = panel.scene.nodes[node].ports[(output, port)]
            panel.view.ensureVisible(item)
            self.app.processEvents()
            point = panel.view.mapFromScene(item.scenePos())
            QtTest.QTest.mouseClick(panel.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertEqual(self.dialog.schema()['fields']['task_name']['aliases'], ['编号', '旧编号'])

    def test_source_node_grows_for_many_or_long_names(self):
        panel = self.dialog.task_panel
        old = panel.scene.nodes[panel.filter_nodes()[0]['id']].boundingRect()
        count = len(panel.scene.nodes)
        identifier = panel.add_filter_record(['很长的列名' * 30] + [f'额外列{number}' for number in range(150)])
        node = panel.scene.nodes[identifier]
        self.assertGreater(node.boundingRect().width(), old.width())
        self.assertGreater(node.boundingRect().height(), old.height())
        self.assertEqual(len(panel.scene.nodes), count + 1)
        self.assertIn('很长的列名' * 30, node.data['params']['aliases'])
        self.assertEqual(len(node.outputs), 1)

    def test_reorder_defaults_and_other_section_remain_intact(self):
        panel = self.dialog.task_panel
        panel.field.setCurrentIndex(panel.field.findData('task_id'))
        with patch.object(panel, 'aliases_dialog', return_value=['旧编号', '编号']):
            panel.edit_filter(panel.connected_filter())
        panel.set_default('{data_row}')
        result = self.dialog.schema()
        self.assertEqual(result['fields']['task_id'], {'aliases': ['旧编号', '编号'], 'default': '{data_row}'})
        self.assertEqual(result['submission_sheet'], schema()['submission_sheet'])

    def test_move_and_select_do_not_change_schema(self):
        before = self.dialog.schema()
        node = self.dialog.task_panel.scene.nodes[self.dialog.task_panel.filter_nodes()[0]['id']]
        node.setPos(80, 100)
        node.setSelected(True)
        self.app.processEvents()
        self.assertEqual(self.dialog.schema(), before)

    def test_no_execution_and_fixed_nodes_cannot_be_deleted(self):
        panel = self.dialog.task_panel
        panel.scene.remove_node('structure')
        self.assertIn('structure', panel.scene.nodes)
        for definition in panel.scene.registry.definitions.values():
            with self.assertRaises(RuntimeError):
                definition.build({}, {}, None)

    def test_background_header_read_retains_existing_connections(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / '任务.ods'
            from odf.opendocument import OpenDocumentSpreadsheet
            from odf.table import Table, TableRow, TableCell
            from odf.text import P
            document = OpenDocumentSpreadsheet()
            table = Table(name='任务')
            row = TableRow()
            for header in ('编号', '类型', '新列'):
                cell = TableCell(valuetype='string')
                cell.addElement(P(text=header))
                row.addElement(cell)
            table.addElement(row)
            document.spreadsheet.addElement(table)
            document.save(str(path))
            self.dialog.ods_path = path
            before = self.dialog.schema()
            self.dialog.read_headers()
            self.assertIsNotNone(self.dialog.handle)
            self.wait_read()
            self.assertIn('新列', self.dialog.available_headers)
            self.assertEqual(self.dialog.schema(), before)
            self.assertIn('3个列名', self.dialog.status.text())

    def test_cancel_leaves_input_config_untouched(self):
        original = schema()
        dialog = TaskTableSchemaNodeDialog(original)
        dialog.task_panel.document.rename_source('编号', '更改')
        dialog.reject()
        self.assertEqual(original, schema())
        self.assertEqual(dialog.result(), QtWidgets.QDialog.Rejected)
        dialog.deleteLater()

    def test_header_failure_recovers_controls(self):
        with tempfile.NamedTemporaryFile(suffix='.ods') as temporary:
            self.dialog.ods_path = Path(temporary.name)
            self.dialog.read_headers()
            self.wait_read()
            self.assertIn('读取失败', self.dialog.status.text())
            self.assertTrue(self.dialog.read.isEnabled())
            self.assertTrue(self.dialog.buttons.button(QtWidgets.QDialogButtonBox.Save).isEnabled())

    def test_optional_screenshot(self):
        destination = os.environ.get('LZX_SCHEMA_SCREENSHOT')
        if destination:
            self.app.processEvents()
            self.assertTrue(self.dialog.grab().save(destination))

    def test_cancel_blocked_read_releases_and_allows_next_attempt(self):
        from PYUI import task_schema_node_pyui as ui
        entered, release = Event(), Event()
        def slow(payload, context):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test deadline')
            context.check_canceled()
            return {'headers': ['不应应用'], 'sheet_name': '任务', 'header_row': 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dummy.ods'
            path.touch()
            self.dialog.ods_path = path
            with patch.object(ui, 'read_columns', slow):
                try:
                    self.dialog.read_headers()
                    self.app.processEvents()
                    self.assertTrue(entered.wait(1))
                    self.dialog.reject()
                    self.assertIn('等待读取', self.dialog.status.text())
                    self.dialog.cancel_reading()
                finally:
                    release.set()
                self.wait_read()
            self.assertNotIn('不应应用', self.dialog.task_panel.document.sources)
            self.assertTrue(self.dialog.read.isEnabled())
            with patch.object(ui, 'read_columns', return_value={'headers': ['下一轮'], 'sheet_name': '任务', 'header_row': 1}):
                self.dialog.read_headers()
                self.wait_read()
            self.assertIn('下一轮', self.dialog.available_headers)

    def test_stale_read_result_does_not_replace_edited_configuration(self):
        from PYUI import task_schema_node_pyui as ui
        release = Event()
        def slow(payload, context):
            release.wait(2)
            return {'headers': ['旧配置的表头'], 'sheet_name': '任务', 'header_row': 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dummy.ods'
            path.touch()
            self.dialog.ods_path = path
            with patch.object(ui, 'read_columns', slow):
                try:
                    self.dialog.read_headers()
                    self.dialog.header_row.setValue(2)
                finally:
                    release.set()
                self.wait_read()
            self.assertNotIn('旧配置的表头', self.dialog.task_panel.document.sources)
            self.assertEqual(self.dialog.schema()['header_row'], 2)
            self.assertIn('配置已更改', self.dialog.status.text())


class SettingsEntryTests(unittest.TestCase):
    def test_settings_page_keeps_original_button_and_adds_visual_button(self):
        from PYUI import main_setting_pyui as ui
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = QtWidgets.QWidget()
        window.settingTabWidget = QtWidgets.QTabWidget(window)
        window._section_label = lambda text, parent: QtWidgets.QLabel(text, parent)
        window._refresh_task_schema_status = MagicMock()
        window.edit_task_table_schema = MagicMock()
        ui.MainSettingDialog._build_task_schema_tab(window)
        self.assertEqual(window.edit_task_schema_button.text(), '打开任务表格结构设置…')
        self.assertEqual(window.edit_task_schema_nodes_button.text(), '节点式编辑结构…')
        window.edit_task_schema_button.click()
        window.edit_task_schema_nodes_button.click()
        self.assertEqual(window.edit_task_table_schema.call_count, 2)
        self.assertTrue(window.edit_task_table_schema.call_args.kwargs['node_editor'])
        window.deleteLater()
        app.processEvents()
    def test_old_and_new_entry_use_same_save_path(self):
        from PYUI import main_setting_pyui as ui
        for nodes in (False, True):
            window = MagicMock()
            window._current_task_ods_path.return_value = None
            result = schema()
            with patch.object(ui, 'load_task_table_schema', return_value=schema()), \
                 patch.object(ui, 'save_task_table_schema') as save, \
                 patch.object(ui.QMessageBox, 'information'), \
                 patch.object(ui.TaskTableSchemaDialog, 'get_schema', return_value=result) as original, \
                 patch.object(TaskTableSchemaNodeDialog, 'get_schema', return_value=result) as visual:
                ui.MainSettingDialog.edit_task_table_schema(window, node_editor=nodes)
            save.assert_called_once_with(result)
            self.assertEqual(visual.call_count, int(nodes))
            self.assertEqual(original.call_count, int(not nodes))

    def test_cancel_node_editor_never_saves(self):
        from PYUI import main_setting_pyui as ui
        with patch.object(ui, 'load_task_table_schema', return_value=schema()), \
             patch.object(ui, 'save_task_table_schema') as save, \
             patch.object(TaskTableSchemaNodeDialog, 'get_schema', return_value=None):
            ui.MainSettingDialog.edit_task_table_schema(MagicMock(), node_editor=True)
        save.assert_not_called()


class HeaderCancellationTests(unittest.TestCase):
    def test_cancel_prevents_file_read(self):
        canceled = Event()
        canceled.set()
        run = TaskRunContext(canceled, lambda *args: None, lambda *args: None)
        with self.assertRaises(BackgroundTaskCanceled):
            read_columns({'path': 'missing.ods', 'schema': schema()}, run)
