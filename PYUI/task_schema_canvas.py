"""Filter nodes -> one structure node. Canvas reuse only, no execution."""
from types import SimpleNamespace
from uuid import uuid4
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.api import NodeDefinition, NodePort
from model.TaskSchemaMapping import display_name
from PYUI.node_graph_canvas import NodeScene, NodeView, NodeItem, EdgeItem


def no_execution(*args):
    raise RuntimeError('这是表格字段映射，不是可运行的节点工作流。')


class MappingNodeItem(NodeItem):
    WIDTH = 340

    @staticmethod
    def layout_metrics(definition, params):
        if definition.outputs:
            return (), 28, 90 + max(1, len(params.get('aliases', []))) * 28
        return definition.inputs, 36, 86 + max(1, len(definition.inputs)) * 36

    def __init__(self, scene, data, definition):
        names = data['params'].get('aliases', []) if definition.outputs else [port.title for port in definition.inputs]
        font = QtGui.QFont(QtWidgets.QApplication.font())
        font.setPointSizeF(max(11, font.pointSizeF()))
        metrics = QtGui.QFontMetrics(font)
        self.WIDTH = max(300, max((metrics.horizontalAdvance(str(name)) + 62 for name in names), default=0))
        super().__init__(scene, data, definition)
        if self.outputs:
            self.ports[(True, 'value')].setPos(self.WIDTH, self.height / 2)
            self.ports[(True, 'value')].setToolTip('按节点中候选列的顺序读取，取第一个非空值')

    def can_preview(self):
        return False

    def mouseDoubleClickEvent(self, event):
        if self.outputs and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.scene().editFilterRequested.emit(self.data['id'])
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        if not self.outputs:
            event.accept()
            return
        menu = QtWidgets.QMenu()
        edit = menu.addAction('编辑候选列名…')
        remove = menu.addAction('删除筛选器')
        selected = menu.exec(event.screenPos())
        if selected == edit:
            self.scene().editFilterRequested.emit(self.data['id'])
        elif selected == remove:
            self.scene().remove_node(self.data['id'])

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor('#83b5ff' if self.isSelected() else '#62748a'), 2))
        painter.setBrush(QtGui.QColor('#273446'))
        painter.drawRoundedRect(QtCore.QRectF(0, 0, self.WIDTH, self.height), 9, 9)
        painter.setPen(QtGui.QColor('#f4f7ff'))
        font = painter.font()
        font.setPointSizeF(max(11, font.pointSizeF()))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QtCore.QRectF(14, 5, self.WIDTH - 28, 30), QtCore.Qt.AlignmentFlag.AlignVCenter,
                         self.definition.title)
        font.setBold(False)
        painter.setFont(font)
        names = self.data['params'].get('aliases', []) if self.outputs else [port.title for port in self.inputs]
        for index, name in enumerate(names):
            text = f'{index + 1}. {name}' if self.outputs else name
            painter.drawText(QtCore.QRectF(14, 43 + index * self.row_spacing, self.WIDTH - 28, 24),
                             QtCore.Qt.AlignmentFlag.AlignLeft, text)
        painter.setPen(QtGui.QColor('#b3c6de'))
        font.setPointSizeF(9)
        painter.setFont(font)
        painter.drawText(QtCore.QRectF(14, self.height - 28, self.WIDTH - 28, 22), QtCore.Qt.AlignmentFlag.AlignLeft,
                         '从上到下取首个非空值 · 双击编辑' if self.outputs else '每个字段只接一个筛选器')


class MappingEdgeItem(EdgeItem):
    def __init__(self, scene, edge):
        super().__init__(scene, edge)
        self.setToolTip('右键断开映射；每个程序字段只接一个筛选器')

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu()
        remove = menu.addAction('断开映射')
        if menu.exec(event.screenPos()) == remove:
            self.scene().graph['connections'].remove(self.edge)
            self.scene().schedule_refresh()


class MappingScene(NodeScene):
    fieldFocused = QtCore.pyqtSignal(str)
    editFilterRequested = QtCore.pyqtSignal(str)

    def _rebuild_items(self):
        centers = [(view, view.mapToScene(view.viewport().rect().center())) for view in self.views()]
        self.nodes, self.edges, self.regions, self.annotations, self.port_types = {}, [], {}, {}, {}
        self.clear()
        for node in self.graph['nodes']:
            self.nodes[node['id']] = MappingNodeItem(self, node, self.registry.definitions[node['kind']])
        self.edges = [MappingEdgeItem(self, edge) for edge in self.graph['connections']]
        self.update_edges()
        self.setSceneRect(self.sceneRect().united(self.itemsBoundingRect().adjusted(-60, -60, 60, 60)))
        for view, center in centers:
            view.centerOn(center)

    def candidate(self, port):
        if self.pending is None or port.output == self.pending.output:
            raise ValueError('请把筛选器输出连接到右侧程序字段。')
        source, target = (self.pending, port) if self.pending.output else (port, self.pending)
        if source.node.data['kind'] != 'schema_map.filter' or target.node.data['id'] != 'structure':
            raise ValueError('只能从筛选器连接到最终数据结构。')
        edge = [source.node.data['id'], 'value', 'structure', target.port.key]
        edges = [list(e) for e in self.graph['connections'] if e not in self.replacing]
        if any(e[2:] == edge[2:] for e in edges):
            raise ValueError('该字段已经连接筛选器，请先断开旧连线。')
        return edges + [edge]

    def port_pressed(self, port):
        if not port.output:
            self.fieldFocused.emit(port.port.key)
        super().port_pressed(port)

    def preview_insertion(self, item):
        pass

    def complete_insertion(self, item, modifiers):
        pass

    def insert_junction(self, edge, position):
        self.connectionError.emit('这里只连接筛选器和数据结构，不需要中间接点。')

    def refresh_regions(self):
        pass

    def remove_node(self, node_id):
        if node_id == 'structure':
            self.connectionError.emit('最终数据结构固定保留；筛选器和连线可以删除。')
            return
        super().remove_node(node_id)


class MappingView(NodeView):
    addFilterRequested = QtCore.pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Tab:
            self.addFilterRequested.emit()
            event.accept()
        elif event.matches(QtGui.QKeySequence.StandardKey.SelectAll):
            for node in self.scene().nodes.values():
                node.setSelected(True)
            event.accept()
        elif event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier and event.key() in (
                QtCore.Qt.Key.Key_C, QtCore.Qt.Key.Key_V, QtCore.Qt.Key.Key_X, QtCore.Qt.Key.Key_D):
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        if self.itemAt(event.pos()) is not None:
            return super().contextMenuEvent(event)
        menu = QtWidgets.QMenu(self)
        add = menu.addAction('添加筛选器…')
        if menu.exec(event.globalPos()) == add:
            self.addFilterRequested.emit()


class SchemaMappingPanel(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()

    def __init__(self, document, parent=None):
        super().__init__(parent)
        self.document = document
        self.registry = SimpleNamespace(definitions={
            'schema_map.filter': NodeDefinition('schema_map.filter', '筛选器',
                outputs=(NodePort('value', '读取结果', 'text'),), build=no_execution),
            'schema_map.structure': NodeDefinition('schema_map.structure', '最终数据结构',
                inputs=tuple(NodePort(key, display_name(document.section, key), 'text', required=False)
                             for key in document.fields), build=no_execution)})
        graph = {'nodes': [], 'connections': [], 'output_node': '', 'view': {}}
        shared = {}
        for key in document.fields:
            aliases = document.aliases(key)
            if not aliases:
                continue
            stamp = tuple(aliases)
            if stamp not in shared:
                identifier = uuid4().hex
                shared[stamp] = identifier
                graph['nodes'].append({'id': identifier, 'kind': 'schema_map.filter', 'params': {'aliases': aliases}, 'position': [0, 0]})
            graph['connections'].append([shared[stamp], 'value', 'structure', key])
        graph['nodes'].append({'id': 'structure', 'kind': 'schema_map.structure', 'params': {}, 'position': [900, 0]})
        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        for title, callback in [('添加筛选器', self.add_filter), ('整理布局', self.arrange), ('适应画布', self.fit)]:
            button = QtWidgets.QPushButton(title, self)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        splitter = QtWidgets.QSplitter(self)
        layout.addWidget(splitter, 1)
        self.scene = MappingScene(graph, self.registry, self)
        self.view = MappingView(self.scene, splitter)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setToolTip('筛选器的候选列名直接显示在节点上；拖动端口接线，中键平移，滚轮缩放。')
        properties = QtWidgets.QWidget(splitter)
        properties.setMinimumWidth(260)
        properties.setMaximumWidth(360)
        form = QtWidgets.QVBoxLayout(properties)
        form.addWidget(QtWidgets.QLabel('程序字段', properties))
        self.field = QtWidgets.QComboBox(properties)
        for key in document.fields:
            self.field.addItem(display_name(document.section, key), key)
            self.field.setItemData(self.field.count() - 1, '内部标识：' + key, QtCore.Qt.ItemDataRole.ToolTipRole)
        form.addWidget(self.field)
        form.addWidget(QtWidgets.QLabel('字段显示名称（可直接修改）', properties))
        self.label_edit = QtWidgets.QLineEdit(properties)
        form.addWidget(self.label_edit)
        form.addWidget(QtWidgets.QLabel('所连筛选器的候选列（依次尝试）', properties))
        self.aliases = QtWidgets.QListWidget(properties)
        self.aliases.setMaximumHeight(180)
        form.addWidget(self.aliases)
        row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QPushButton('编辑筛选器', properties)
        edit.clicked.connect(lambda: self.edit_filter(self.connected_filter()))
        unlink = QtWidgets.QPushButton('断开此字段', properties)
        unlink.clicked.connect(self.unlink_field)
        row.addWidget(edit)
        row.addWidget(unlink)
        form.addLayout(row)
        self.default_label = QtWidgets.QLabel('列缺失或内容为空时的默认值', properties)
        self.default = QtWidgets.QLineEdit(properties)
        form.addWidget(self.default_label)
        form.addWidget(self.default)
        self.default_label.setVisible(not document.submission)
        self.default.setVisible(not document.submission)
        hint = QtWidgets.QLabel('每个字段只接一个筛选器。一个筛选器可放多个候选列名，按显示顺序取第一个非空值。\n\n'
                               '同一筛选器可连到不同字段；修改它会同时影响这些字段。双击筛选器可编辑列名。', properties)
        hint.setWordWrap(True)
        form.addWidget(hint)
        form.addStretch()
        splitter.setSizes([850, 280])
        self.status = QtWidgets.QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.scene.connectionError.connect(self.status.setText)
        self.scene.graphChanged.connect(self.sync_connections)
        self.scene.fieldFocused.connect(lambda key: self.field.setCurrentIndex(self.field.findData(key)))
        self.scene.editFilterRequested.connect(self.edit_filter)
        self.view.addFilterRequested.connect(self.add_filter)
        self.field.currentIndexChanged.connect(self.refresh_properties)
        self.default.textEdited.connect(self.set_default)
        self.label_edit.textEdited.connect(self.rename_field)
        self.arrange()

    def filter_nodes(self):
        return [node for node in self.scene.graph['nodes'] if node['kind'] == 'schema_map.filter']

    def connected_filter(self):
        key = self.field.currentData()
        return next((e[0] for e in self.scene.graph['connections'] if e[2:] == ['structure', key]), None)

    def rebuild(self):
        self.scene.rebuild()
        self.refresh_properties()

    def sync_connections(self):
        nodes = {node['id']: node for node in self.filter_nodes()}
        for node in nodes.values():
            for name in node['params']['aliases']:
                if name not in self.document.sources:
                    self.document.sources.append(name)
        for field in self.document.fields:
            sources = [e[0] for e in self.scene.graph['connections'] if e[2:] == ['structure', field]]
            if len(sources) > 1:
                raise ValueError('每个程序字段只允许一个筛选器。')
            self.document.set_aliases(field, nodes[sources[0]]['params']['aliases'] if sources else [])
        self.refresh_properties()
        self.changed.emit()

    def refresh_properties(self, *_args):
        key = self.field.currentData()
        if key is not None:
            self.aliases.clear()
            self.aliases.addItems(self.document.aliases(key))
            self.default.setText(str(self.document.section['fields'][key].get('default', '')))
            self.label_edit.setText(display_name(self.document.section, key))

    def rename_field(self, text):
        key = self.field.currentData()
        if not text.strip() or key is None:
            return
        self.document.section['labels'][key] = text.strip()
        self.field.setItemText(self.field.currentIndex(), text.strip())
        # Rebuild only the painted structure; editing the line edit never
        # destroys/recreates its own widget or changes the canonical field key.
        definition = self.registry.definitions['schema_map.structure']
        from dataclasses import replace
        self.registry.definitions['schema_map.structure'] = replace(definition,
            inputs=tuple(NodePort(field, display_name(self.document.section, field), 'text', required=False)
                         for field in self.document.fields))
        self.scene.rebuild()
        self.changed.emit()

    def set_default(self, text):
        if not self.document.submission:
            self.document.section['fields'][self.field.currentData()]['default'] = text
            self.changed.emit()

    def unlink_field(self):
        key = self.field.currentData()
        self.scene.graph['connections'] = [edge for edge in self.scene.graph['connections'] if edge[2:] != ['structure', key]]
        self.rebuild()
        self.sync_connections()

    def aliases_dialog(self, values=()):
        text, ok = QtWidgets.QInputDialog.getMultiLineText(self, '筛选器候选列名',
            '每行一个列名；从上到下依次尝试，取首个非空值：', '\n'.join(values))
        if not ok:
            return None
        values = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
        if not values:
            self.status.setText('筛选器至少需要一个列名。')
            return None
        return values

    def add_filter_record(self, aliases, position=None):
        values = list(dict.fromkeys(str(value).strip() for value in aliases if str(value).strip()))
        if not values:
            raise ValueError('筛选器至少需要一个列名。')
        identifier = uuid4().hex
        if position is None:
            point = self.view.mapToScene(self.view.viewport().rect().center())
            position = [point.x() - 150, point.y() - 60]
        self.scene.graph['nodes'].append({'id': identifier, 'kind': 'schema_map.filter',
            'params': {'aliases': values}, 'position': position})
        self.rebuild()
        return identifier

    def add_filter(self):
        aliases = self.aliases_dialog()
        if aliases:
            self.add_filter_record(aliases)

    def edit_filter(self, identifier):
        node = next((node for node in self.filter_nodes() if node['id'] == identifier), None)
        if node is None:
            self.status.setText('请先选择或连接一个筛选器。')
            return
        aliases = self.aliases_dialog(node['params']['aliases'])
        if aliases is not None:
            node['params']['aliases'] = aliases
            self.rebuild()
            self.sync_connections()

    def arrange(self):
        # Keep wires out of other cards: independent filters in one left column.
        nodes = self.filter_nodes()
        items = self.scene.nodes
        column_width = max((items[node['id']].WIDTH for node in nodes), default=300) + 45
        bottom = 0
        for index, node in enumerate(nodes):
            node['position'] = [0, bottom]
            bottom += items[node['id']].height + 32
        structure = next(node for node in self.scene.graph['nodes'] if node['id'] == 'structure')
        structure['position'] = [column_width + 190, 0]
        self.rebuild()

    def fit(self):
        self.view.fitInView(self.scene.itemsBoundingRect().adjusted(-30, -30, 30, 30), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
