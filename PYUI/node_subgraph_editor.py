"""Child workspace navigation reuses the regular canvas/inspector/history."""
import copy
import json
from uuid import uuid4
from qt_compat import QtCore, QtWidgets


class SubgraphInspector(QtWidgets.QWidget):
    def __init__(self, params, changed, parent=None):
        super().__init__(parent)
        self.params, self.changed = copy.deepcopy(params), changed
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        self.name = QtWidgets.QLineEdit(params.get('name', '子图')); root.addWidget(self.name)
        self.name.textEdited.connect(lambda value: self.update_value('name', value))
        for title, specs in (('输入', params.get('inputs', [])), ('输出', params.get('outputs', []))):
            root.addWidget(QtWidgets.QLabel(title + '：' + '、'.join(s['title'] for s in specs)))
        interface = QtWidgets.QPushButton('对外端口名称／类型…'); root.addWidget(interface); interface.clicked.connect(self.edit_interface)
        button = QtWidgets.QPushButton('进入子图'); root.addWidget(button); button.clicked.connect(self.enter)
        preview = QtWidgets.QPushButton('预览子图输出'); root.addWidget(preview); preview.clicked.connect(self.preview)
        root.addWidget(QtWidgets.QLabel('公开参数（JSON值；内部节点仍负责类型检查）'))
        self.error = QtWidgets.QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet('color:#c33442')
        form = QtWidgets.QFormLayout(); root.addLayout(form)
        for exposure in params.get('promoted', []):
            control = QtWidgets.QLineEdit(json.dumps(params.get('values', {}).get(exposure['id']), ensure_ascii=False))
            control.editingFinished.connect(lambda k=exposure['id'], c=control: self.promoted(k, c.text()))
            form.addRow(exposure['title'], control)
        root.addWidget(self.error)

    def owner(self):
        widget = self.parentWidget()
        while widget is not None:
            if hasattr(widget, 'open_subgraph') and hasattr(widget, 'inspected'):
                return widget
            widget = widget.parentWidget()
        raise RuntimeError('子图界面已关闭。')

    def enter(self):
        owner = self.owner(); owner.open_subgraph(owner.inspected)

    def preview(self):
        owner = self.owner(); owner.preview_node(owner.inspected)

    def update_value(self, key, value):
        if self.changed({key: value}) is not False:
            self.params[key] = value

    def promoted(self, key, text):
        try:
            values = copy.deepcopy(self.params.get('values', {})); values[key] = json.loads(text)
            self.update_value('values', values); self.error.clear()
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))

    def edit_interface(self):
        from model.node_workflow.code_node import TYPES
        dialog = QtWidgets.QDialog(self); dialog.setWindowTitle('对外接口 · ID稳定，改名不丢线')
        layout = QtWidgets.QVBoxLayout(dialog); tables = {}
        for direction, title in (('inputs', '输入'), ('outputs', '输出')):
            layout.addWidget(QtWidgets.QLabel(title))
            specs = self.params.get(direction, [])
            table = QtWidgets.QTableWidget(len(specs), 3); table.setHorizontalHeaderLabels(['名称', '类型', '必填'])
            for row, spec in enumerate(specs):
                table.setItem(row, 0, QtWidgets.QTableWidgetItem(spec['title']))
                combo = QtWidgets.QComboBox(); combo.addItems(TYPES)
                if combo.findText(spec['type']) < 0: combo.addItem(spec['type'])
                combo.setCurrentText(spec['type']); table.setCellWidget(row, 1, combo)
                required = QtWidgets.QCheckBox(); required.setChecked(spec.get('required', True)); table.setCellWidget(row, 2, required)
            tables[direction] = table; layout.addWidget(table)
        note = QtWidgets.QLabel('端口由封装边界生成。增删边界请展开／重新封装；修改类型会检查内外连接，不会静默清线。')
        note.setWordWrap(True); layout.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted: return
        values = copy.deepcopy(self.params)
        for direction, table in tables.items():
            for row, spec in enumerate(values[direction]):
                title = table.item(row, 0).text().strip()
                if not title or len(title) > 100:
                    self.error.setText('端口名称不能为空或超过100字。'); return
                spec.update(title=title, type=table.cellWidget(row, 1).currentText(), required=table.cellWidget(row, 2).isChecked())
                if direction == 'inputs':
                    entry = next(n for n in values['graph']['nodes'] if n['id'] == spec['node'])
                    entry['params'].update(type=spec['type'], required=spec['required'])
        sink = next(n for n in values['graph']['nodes'] if n['id'] == values['graph']['output_node'])
        sink['params']['ports'] = [{k: s[k] for k in ('id', 'title', 'type', 'required')} for s in values['outputs']]
        if self.changed(values) is not False:
            self.params = values; self.error.clear()
        else:
            self.error.setText('类型修改会冲突，原端口和连线已保留。')


def open_editor(owner, node_id):
    from .node_workspace import NodeWorkspace
    from model.node_workflow.graph import validate
    wrapper = next(n for n in owner.graph['nodes'] if n['id'] == node_id)
    params = copy.deepcopy(wrapper['params'])
    dialog = QtWidgets.QDialog(owner); dialog.setWindowTitle('子图 · ' + params.get('name', '子图')); dialog.resize(1250, 800)
    root = QtWidgets.QVBoxLayout(dialog)
    top = QtWidgets.QHBoxLayout(); root.addLayout(top)
    back = QtWidgets.QPushButton('← 返回上一级'); top.addWidget(back)
    label = QtWidgets.QLabel(getattr(owner, 'breadcrumb', '主图') + ' / ' + params.get('name', '子图')); top.addWidget(label, 1)
    class ChildWorkspace(NodeWorkspace):
        def save_file(self, *_, **__):
            commit(); self.status.setText('内部修改已写回主图；主图保存时一起保存。'); return True
        def open_library(self, *_, **__):
            self.error('请返回主图打开其他工作流。')
        def open_file(self, *_, **__):
            self.error('请返回主图导入工作流。')
        def save_to_library(self, *_, **__):
            return self.save_file()
        def run(self):
            commit(); owner.preview_node(node_id)
        def preview_node(self, internal_id, _checked=False):
            commit(); owner.preview_node(node_id + '/' + internal_id)
        def validate_ui(self):
            if not hasattr(self, 'run_button'): return
            self.sync_execution_mode()
            self.task_button.hide(); self.task_label.hide()
            self.run_button.setText('预览子图输出'); self.run_action.setText('预览子图输出（不导出）')
            from model.node_workflow.typing import resolve_port_types
            self.scene.port_types, _ = resolve_port_types(self.graph, self.registry)
            self.scene.update_edges()
            root_id = self.graph.get('output_node')
            try:
                errors, _, _ = validate(self.graph, self.registry, root_id=root_id) if root_id else ([], [], {})
                entries = {n['id'] for n in self.graph['nodes'] if n['kind'] == 'core.subgraph_input'}
                errors = [(key, text) for key, text in errors if not (key in entries and '未连接' in text)]
            except ValueError as error:
                errors = [('', str(error))]
            self.current_errors = errors
            for key, item in self.scene.nodes.items():
                item.errors = [message for node, message in errors if node == key]; item.update()
    child = ChildWorkspace(owner.host, dialog)
    initial = copy.deepcopy(params['graph'])
    for exposure in params.get('promoted', []):
        target = next((n for n in initial['nodes'] if n['id'] == exposure['node']), None)
        if target and exposure['id'] in params.get('values', {}):
            target['params'][exposure['parameter']] = copy.deepcopy(params['values'][exposure['id']])
    child.replace_graph(initial)
    child.bound_tasks = owner.bound_tasks; child.breadcrumb = label.text()
    child.mode_combo.setEnabled(False); child.task_button.hide(); child.task_label.hide()
    child.menu.actions()[0].setVisible(False)
    child.run_button.setText('预览子图输出'); child.run_action.setText('预览子图输出（不导出）')
    root.addWidget(child, 1)
    def commit():
        child.flush_canvas_changes()
        for exposure in params.get('promoted', []):
            target = next((n for n in child.graph['nodes'] if n['id'] == exposure['node']), None)
            if target:
                params.setdefault('values', {})[exposure['id']] = copy.deepcopy(target['params'][exposure['parameter']])
        values = {'graph': copy.deepcopy(child.graph), 'promoted': copy.deepcopy(params.get('promoted', [])),
                  'values': copy.deepcopy(params.get('values', {}))}
        # One outer history entry per edit burst; cancellation never drops node params.
        if owner.set_node_parameters(wrapper, values) is not False:
            params.update(values)
    def expose():
        data = next((n for n in child.graph['nodes'] if n['id'] == child.inspected), None)
        if not data: child.error('请先选中内部节点。'); return
        definition = child.registry.definitions[data['kind']]
        choices = [p for p in definition.parameters if p.kind != 'object' and p.key != 'approved']
        if not choices: child.error('该节点没有可公开的简单参数；完整组件设置仍在内部调整。'); return
        titles = [p.title + ' · ' + p.key for p in choices]
        title, ok = QtWidgets.QInputDialog.getItem(dialog, '公开内部参数', '选择参数', titles, editable=False)
        if not ok: return
        parameter = choices[titles.index(title)]
        if any(s['node'] == data['id'] and s['parameter'] == parameter.key for s in params.get('promoted', [])):
            child.error('该参数已经公开。'); return
        key = uuid4().hex
        params.setdefault('promoted', []).append({'id': key, 'node': data['id'], 'parameter': parameter.key,
            'title': definition.title + ' · ' + parameter.title})
        params.setdefault('values', {})[key] = copy.deepcopy(data['params'].get(parameter.key, parameter.default))
        commit(); child.status.setText('已公开到主图子图节点参数面板。')
    expose_button = QtWidgets.QPushButton('公开所选节点参数…'); top.addWidget(expose_button); expose_button.clicked.connect(expose)
    syncing = [False]
    def sync_parent(_index):
        if syncing[0]: return
        syncing[0] = True
        try: commit()
        finally: syncing[0] = False
    child.history.stack.indexChanged.connect(sync_parent)
    back.clicked.connect(dialog.accept)
    dialog.exec(); commit()
    owner.scene.schedule_refresh(); owner.inspect_selection()
    child.cancel_action.setEnabled(False)  # Parent owns previews; dialog never cancels them.
    child._preview_queue_timer.stop(); child.scene.refresh_timer.stop()
    try: owner.host.background_tasks.changed.disconnect(child.task_record_changed)
    except (TypeError, RuntimeError): pass
    dialog.deleteLater()
