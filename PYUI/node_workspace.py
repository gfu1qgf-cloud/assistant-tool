"""Host-owned, independent node workspace. Closing UI does not own workers."""
import math
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import directory_resource
from model.node_workflow.graph import (example_graph, new_node, load_graph, save_graph,
                                       validate, MAX_NODES, migrate_task_ports, _task,
                                       export_example_graph, OUTPUT_KINDS)
from model.node_workflow.tasks import capture_task, prepare_job
from model.node_workflow.render import run_workflow, run_task_workflow, resolve_tools
from .node_graph_canvas import NodeScene, NodeView, NodeItem
from .node_workflow_results import WorkflowResults
from model.node_workflow.preview import run_preview, run_data_workflow, is_preview
from model.node_workflow.python_nodes import python_example_graph
from model.node_workflow.python_extended import batch_example_graph
from model.node_workflow.loops import add_loop_pair, loop_example_graph
from .node_preview import NodePreviewDialog
from .node_connection_paths import WIRE_STYLES
from model.node_workflow.reroute import ROUTE_KINDS
from model.node_workflow.scope import EXECUTION_MODES, execution_mode
from model.node_workflow.global_data import WorkflowGlobals, GLOBAL_OUTPUTS
from model.node_workflow.workflow_types import output_kinds
from model.node_workflow.library import WorkflowLibrary
from .node_catalog import NodePicker, node_category, visible_nodes
from .node_graph_history import GraphHistory
from .node_window import NodeWindow
from .node_annotations import AnnotationItem


class NodeWorkspace(QtWidgets.QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.registry = host.available_nodes()
        from .node_inspectors import install_core_inspectors
        install_core_inspectors(self.registry)
        self.graph = (export_example_graph(self.registry, True) if "smart_video_editor.edit" in self.registry.definitions
                      and "batch_text_video.caption" in self.registry.definitions else example_graph(self.registry))
        self.bound_tasks = []
        self.task_handles = {}
        self.batch_for_handle = {}
        self.path = None
        self._paste_signature = None
        self._paste_count = 0
        self.dirty = False
        self.handle = None
        self.preview_handle = None
        self.preview_dialogs = []
        self._preview_signatures = {}
        self._queued_previews = {}
        self._closed_previews = set()
        self._active_preview_node = None
        self._preview_queue_timer = QtCore.QTimer(self)
        self._preview_queue_timer.setSingleShot(True)
        self._preview_queue_timer.timeout.connect(self.start_next_preview)
        self.last_result = None
        self.inspected = None
        self._inspector_generation = 0
        self._preview_button = None
        self.node_picker = None
        self.history = GraphHistory(self.graph, self.restore_history, self)
        self.history.stack.cleanChanged.connect(self.history_clean_changed)
        self.setMinimumSize(500,320)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6,4,6,6)
        self.menu = QtWidgets.QMenuBar(self)
        file_menu = self.menu.addMenu("文件")
        self._action(file_menu, '新建工作流…（选择类型）', self.new_workflow)
        self._action(file_menu,"新建任务工作流",self.new_task_example)
        self._action(file_menu,"新建文件结算／导出示例",self.new_export_example)
        self._action(file_menu,"新建文件导入示例",self.new_example)
        self._action(file_menu,"新建内存视频示例",partial(self.new_example,"memory"))
        self._action(file_menu,"新建管道视频示例",partial(self.new_example,"stream"))
        self._action(file_menu,"新建 Python 数据处理示例",self.new_python_example)
        self._action(file_menu,"新建 Python 批量分组示例",self.new_batch_data_example)
        loops_menu = file_menu.addMenu("新建 Begin／End 循环示例")
        for mode, title in (("count", "按次数 · 计算 1～5 的平方"),
                            ("foreach", "逐项遍历 · 处理列表"), ("while", "条件与反馈 · 累加到 5")):
            self._action(loops_menu, title, partial(self.new_loop_example, mode))
        file_menu.addSeparator()
        open_action = self._action(file_menu, '打开工作流…', self.open_library)
        save_action = self._action(file_menu,"保存",self.save_file)
        save_as_action = self._action(file_menu,"另存为新工作流…",lambda:self.save_file(True))
        for action, shortcut in ((open_action, 'Ctrl+O'), (save_action, 'Ctrl+S'),
                                 (save_as_action, 'Ctrl+Shift+S')):
            action.setShortcut(QtGui.QKeySequence(shortcut))
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._action(file_menu, '重命名工作流…', lambda: self.save_to_library(rename=True))
        file_menu.addSeparator()
        self._action(file_menu, '导入 JSON…', self.open_file)
        self._action(file_menu, '导出 JSON…', lambda: self.export_file(True))
        edit_menu = self.menu.addMenu("编辑")
        for operation, title in (('copy', '复制节点（Ctrl+C）'), ('paste', '粘贴节点（Ctrl+V）'),
                                 ('cut', '剪切节点（Ctrl+X）'), ('duplicate', '复制一份（Ctrl+D）'),
                                 ('select_all', '全选节点（Ctrl+A）')):
            self._action(edit_menu, title, partial(self.clipboard_operation, operation))
        edit_menu.addSeparator()
        self._action(edit_menu, '添加便签', lambda: self.add_annotation('note'))
        self._action(edit_menu, '选中节点建立注释分组', lambda: self.add_annotation('group'))
        self._action(edit_menu, '选中节点封装为子图…', self.collapse_subgraph)
        self._action(edit_menu, '展开所选子图', self.expand_subgraph)
        self.undo_action = self._action(edit_menu, "撤销", self.undo)
        self.undo_action.setShortcut(QtGui.QKeySequence.StandardKey.Undo)
        self.redo_action = self._action(edit_menu, "重做", self.redo)
        self.redo_action.setShortcuts([QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Redo), QtGui.QKeySequence("Ctrl+Shift+Z")])
        for action in (self.undo_action, self.redo_action):
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
            action.setEnabled(False)
        self.history.stack.canUndoChanged.connect(self.undo_action.setEnabled)
        self.history.stack.canRedoChanged.connect(self.redo_action.setEnabled)
        self.history.stack.undoTextChanged.connect(lambda title: self.undo_action.setText("撤销" + (" · " + title if title else "")))
        self.history.stack.redoTextChanged.connect(lambda title: self.redo_action.setText("重做" + (" · " + title if title else "")))
        nodes_menu = self.menu.addMenu("节点")
        self._action(nodes_menu, "查找／添加节点…（画布按 Tab）", self.show_node_search)
        nodes_menu.addSeparator()
        groups = {}
        for definition in visible_nodes(self.registry):
            parent_menu, path = nodes_menu, ""
            for part in node_category(definition).split("/"):
                path = path + "/" + part
                if path not in groups:
                    groups[path] = parent_menu.addMenu(part)
                parent_menu = groups[path]
            self._action(parent_menu, definition.title, partial(self.add_node, definition.kind))
        self._action(groups["/Python 原生/流程控制/Begin · End 循环块"], "添加成对循环块 · Begin + End", self.add_loop_block)
        run_menu = self.menu.addMenu("运行")
        self.run_action = self._action(run_menu,"执行并导出成品",self.run)
        self.preview_action = self._action(run_menu,"打开所选预览节点",self.preview_selected)
        self._action(run_menu, '纯色模拟预览输出', self.simulate_output)
        self._action(run_menu, '模拟背景与规格…', self.simulation_settings)
        self.task_action = self._action(run_menu,"使用主程序所选任务",self.bind_selected_tasks)
        self.stop_on_error = run_menu.addAction("任务失败后停止本次批次")
        self.stop_on_error.setCheckable(True)
        self.cancel_action = self._action(run_menu,"取消本面板提交的任务",self.cancel)
        self.cancel_action.setEnabled(False)
        self._action(run_menu,"打开成品目录",self.open_output)
        view_menu = self.menu.addMenu("视图")
        self._action(view_menu,"适应画布",self.fit)
        direction_menu = view_menu.addMenu('节点方向')
        self.orientation_group = QtGui.QActionGroup(self)
        self.orientation_actions = {}
        for key, title in (('horizontal', '横向 · 左入右出'), ('vertical', '竖向 · 上入下出')):
            action = direction_menu.addAction(title); action.setCheckable(True)
            self.orientation_group.addAction(action); self.orientation_actions[key] = action
            action.setChecked(key == self.graph.get('view', {}).get('orientation', 'horizontal'))
            action.triggered.connect(lambda _, key=key: self.set_orientation(key))
        self._action(view_menu, '自动整理整图（只在点击时执行）', lambda: self.auto_arrange(False))
        self._action(view_menu, '仅整理选中节点', lambda: self.auto_arrange(True))
        self._action(view_menu, '端口颜色图例', self.color_legend)
        styles_menu = view_menu.addMenu("连线样式")
        self.style_group = QtGui.QActionGroup(self)
        self.style_group.setExclusive(True)
        self.style_actions = {}
        for key, title in WIRE_STYLES:
            action = styles_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(key == self.graph.get("view", {}).get("connection_style", "smooth"))
            self.style_group.addAction(action)
            action.triggered.connect(lambda _checked, key=key: self.scene.set_connection_style(key))
            self.style_actions[key] = action
        root.addWidget(self.menu)
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel("工作流类型"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setObjectName("workflow_execution_mode")
        for mode, title in EXECUTION_MODES:
            self.mode_combo.addItem(title, mode)
        for key, definition in self.registry.workflow_types.items():
            self.mode_combo.addItem(definition.title, key)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(execution_mode(self.graph)))
        self.mode_combo.setToolTip('Detail：全局一次；Line：逐行任务。插件注册的专用类型有自己的输入／输出，由对应插件注入数据；这里只模拟预览。切换类型不删除已有节点，不兼容连线会标红。')
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        root.addLayout(mode_row)
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        task_row = QtWidgets.QHBoxLayout()
        self.task_label = QtWidgets.QLabel("尚未载入任务")
        self.task_label.setWordWrap(True)
        task_row.addWidget(self.task_label, 1)
        self.task_button = QtWidgets.QPushButton("使用主程序所选任务")
        self.task_button.clicked.connect(lambda: self.bind_selected_tasks())
        task_row.addWidget(self.task_button)
        root.addLayout(task_row)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal,self)
        self.scene = NodeScene(self.graph,self.registry,self)
        self.view = NodeView(self.scene,splitter)
        self.view.searchRequested.connect(self.show_node_search)
        self.inspector = QtWidgets.QScrollArea(splitter)
        self.inspector.setWidgetResizable(True)
        self.inspector.setMinimumWidth(220)
        self.inspector.setMaximumWidth(350)
        splitter.addWidget(self.view)
        splitter.addWidget(self.inspector)
        splitter.setStretchFactor(0,4)
        splitter.setStretchFactor(1,1)
        splitter.setSizes([750,270])
        root.addWidget(splitter,1)
        self.results = WorkflowResults(self)
        root.addWidget(self.results)
        self.host.background_tasks.changed.connect(self.task_record_changed)
        bottom = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setFixedWidth(130)
        self.progress.setRange(0,100)
        self.run_button = QtWidgets.QPushButton("执行工作流")
        self.run_button.clicked.connect(self.run)
        bottom.addWidget(self.status,1)
        bottom.addWidget(self.progress)
        bottom.addWidget(self.run_button)
        root.addLayout(bottom)
        self.scene.graphChanged.connect(self.changed)
        self.scene.selectionChanged.connect(self.inspect_selection)
        self.scene.connectionError.connect(self.error)
        self.scene.previewRequested.connect(self.preview_node)
        self.scene.subgraphRequested.connect(self.open_subgraph)
        self.scene.clipboardRequested.connect(self.clipboard_operation)
        self.inspect_selection()
        self.validate_ui()
        QtCore.QTimer.singleShot(0,self.fit)

    @staticmethod
    def _action(menu,title,callback):
        action = menu.addAction(title)
        action.triggered.connect(lambda _checked=False:callback())
        return action

    def error(self,message):
        self.status.setText(str(message))
        self.host.main_window.appendLog("[节点工作流] "+str(message),end="")

    def changed(self, title="编辑节点图", merge_key=None):
        self.history.record(self.graph, title, merge_key)
        self.dirty = not self.history.stack.isClean()
        self.sync_connection_style()
        self.validate_ui()
        self.update_preview_states()

    def history_clean_changed(self, clean):
        self.dirty = not clean

    def flush_canvas_changes(self):
        self._finish_inspector_edits()
        # Connection/deletion refresh is deferred until Qt finishes dispatching.
        if self.scene.refresh_timer.isActive():
            self.scene.refresh_timer.stop()
            self.scene.refresh_changed()

    def undo(self):
        self.flush_canvas_changes()
        self.history.stack.undo()

    def redo(self):
        self.flush_canvas_changes()
        self.history.stack.redo()

    def clipboard_operation(self, operation, position=None):
        from model.node_workflow.clipboard import capture_nodes, paste_nodes, MIME_TYPE
        self.flush_canvas_changes()
        if operation == 'select_all':
            for item in list(self.scene.nodes.values()) + list(self.scene.annotations.values()):
                item.setSelected(True)
            return
        try:
            clipboard = QtWidgets.QApplication.clipboard()
            if operation in {'copy', 'cut', 'duplicate'}:
                selected = {item.data['id'] for item in self.scene.selectedItems() if isinstance(item, (NodeItem, AnnotationItem))}
                data, expanded = capture_nodes(self.graph, selected)
                if operation != 'duplicate':
                    mime = QtCore.QMimeData()
                    mime.setData(MIME_TYPE, data)
                    mime.setText(data.decode('utf-8'))
                    clipboard.setMimeData(mime)
                if operation == 'copy':
                    self.status.setText(f'已复制 {len(expanded)} 个节点及内部连线；选择 Begin/End 时包含完整循环块。')
                    return
                if operation == 'cut':
                    self.graph['nodes'] = [node for node in self.graph['nodes'] if node['id'] not in expanded]
                    self.graph['connections'] = [edge for edge in self.graph['connections']
                                                if edge[0] not in expanded and edge[2] not in expanded]
                    if 'annotations' in self.graph:
                        self.graph['annotations'] = [item for item in self.graph['annotations'] if item['id'] not in expanded]
                    for item in self.graph.get('annotations', []):
                        item['members'] = [key for key in item.get('members', []) if key not in expanded]
                    if self.graph.get('output_node') in expanded:
                        self.graph['output_node'] = ''
                    self.scene.cancel_wire()
                    self.scene.rebuild()
                    self.changed('剪切节点')
                    return
                nodes = [node for node in self.graph['nodes'] + self.graph.get('annotations', []) if node['id'] in expanded]
                position = QtCore.QPointF(min(n.get('position', [0, 0])[0] for n in nodes) + 50,
                                         min(n.get('position', [0, 0])[1] for n in nodes) + 50)
            elif operation == 'paste':
                mime = clipboard.mimeData()
                if mime is not None and mime.hasFormat(MIME_TYPE):
                    data = bytes(mime.data(MIME_TYPE))
                else:
                    data = clipboard.text().encode('utf-8')
                if position is None:
                    if data != self._paste_signature:
                        self._paste_count = 0
                    self._paste_count += 1
                    position = self.view.mapToScene(self.view.viewport().rect().center())
                    position += QtCore.QPointF(30 * self._paste_count, 30 * self._paste_count)
                self._paste_signature = data
            else:
                return
            graph, pasted = paste_nodes(self.graph, data, self.registry, (position.x(), position.y()))
            self.scene.cancel_wire()
            self._inspector_generation += 1
            self.graph.clear(); self.graph.update(graph)
            self.scene.rebuild()
            for node_id in pasted:
                (self.scene.nodes.get(node_id) or self.scene.annotations[node_id]).setSelected(True)
            self.changed('复制一份节点' if operation == 'duplicate' else '粘贴节点')
            self.status.setText(f'已添加 {len(pasted)} 个节点；Ctrl+Z 可整次撤销。')
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            self.error(error)

    def restore_history(self, graph):
        self._inspector_generation += 1
        selected = {item.data["id"] for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        self.scene.refresh_timer.stop()
        self.scene.cancel_wire()
        self.graph = graph
        self.scene.graph = graph
        self.scene.rebuild()
        for node_id in selected:
            if node_id in self.scene.nodes:
                self.scene.nodes[node_id].setSelected(True)
        self.sync_connection_style()
        self.validate_ui()
        self.inspect_selection()
        self.update_preview_states()

    def show_node_search(self, position=None):
        if self.node_picker is not None:
            self.node_picker.close()
            self.node_picker.deleteLater()
        picker = self.node_picker = NodePicker(self.registry, self)
        picker.nodeSelected.connect(lambda kind: self.add_node(kind, position))
        picker.move(self.view.mapToGlobal(self.view.viewport().rect().center()) - QtCore.QPoint(280, 100))
        picker.show()

    def sync_connection_style(self):
        self.style_actions[self.scene.connection_style()].setChecked(True)
        if hasattr(self, 'orientation_actions'):
            self.orientation_actions[self.graph.get('view', {}).get('orientation', 'horizontal')].setChecked(True)

    def set_orientation(self, key):
        self.flush_canvas_changes()
        self.graph.setdefault('view', {})['orientation'] = key
        self.scene.rebuild(); self.changed('切换节点方向')

    def auto_arrange(self, only_selected):
        from model.node_workflow.presentation import arrange
        self.flush_canvas_changes()
        selected = {item.data['id'] for item in self.scene.selectedItems() if isinstance(item, NodeItem)} if only_selected else None
        if only_selected and not selected:
            self.error('请先选中需要整理的节点。'); return
        sizes = {key: (item.boundingRect().width(), item.boundingRect().height()) for key, item in self.scene.nodes.items()}
        try:
            arrange(self.graph, sizes, selected, self.graph.get('view', {}).get('orientation', 'horizontal'))
            self.scene.rebuild(); self.changed('自动整理选中节点' if only_selected else '自动整理整图')
        except ValueError as error:
            self.error(error)

    def add_annotation(self, kind):
        from model.node_workflow.presentation import annotation
        self.flush_canvas_changes()
        if len(self.graph.get('annotations', [])) >= 128:
            self.error('便签／注释分组最多128个。'); return
        selected = [item for item in self.scene.selectedItems() if isinstance(item, NodeItem)] if kind == 'group' else []
        owned = {key for item in self.graph.get('annotations', []) for key in item.get('members', [])}
        if any(item.data['id'] in owned for item in selected):
            self.error('所选节点已属于注释分组，请先移出原分组。'); return
        if selected:
            rect = selected[0].sceneBoundingRect()
            for item in selected[1:]: rect = rect.united(item.sceneBoundingRect())
            rect = rect.adjusted(-30, -55, 30, 30)
            point, size = rect.topLeft(), (rect.width(), rect.height())
        else:
            point, size = self.view.mapToScene(self.view.viewport().rect().center()), (300, 200)
        self.graph.setdefault('annotations', []).append(annotation(kind, (point.x(), point.y()), members=[n.data['id'] for n in selected], size=size))
        self.scene.rebuild(); self.changed('添加便签' if kind == 'note' else '建立注释分组')

    def color_legend(self):
        from model.node_workflow.port_colors import TYPE_COLORS
        dialog = QtWidgets.QDialog(self); dialog.setWindowTitle('端口类型颜色'); layout = QtWidgets.QFormLayout(dialog)
        for dtype, title in (('integer', '整数'), ('float', '浮点数'), ('number', '通用数值'), ('text', '字符串'),
                ('boolean', '布尔值'), ('color', 'RGBA颜色'), ('data', '列表／字典／复杂数据'), ('video', '媒体／处理计划'), ('any', '待推断')):
            label = QtWidgets.QLabel(dtype); label.setStyleSheet('color:' + TYPE_COLORS[dtype] + '; background:#192331; padding:6px')
            layout.addRow(title, label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(dialog.reject); layout.addRow(buttons)
        dialog.exec()

    def collapse_subgraph(self):
        from model.node_workflow.subgraphs import collapse
        self.flush_canvas_changes()
        selected = {item.data['id'] for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        name, ok = QtWidgets.QInputDialog.getText(self, '封装子图', '子图名称', text='子图')
        if not ok or not name.strip(): return
        try:
            wrapper = collapse(self.graph, self.registry, selected, name.strip())
            self.scene.rebuild(); self.scene.nodes[wrapper['id']].setSelected(True); self.changed('封装子图')
        except ValueError as error:
            self.error(error)

    def expand_subgraph(self):
        from model.node_workflow.subgraphs import expand, KIND
        self.flush_canvas_changes()
        data = next((n for n in self.graph['nodes'] if n['id'] == self.inspected and n['kind'] == KIND), None)
        if not data: self.error('请先选中子图节点。'); return
        try:
            ids = expand(self.graph, self.registry, data['id'])
            self.scene.rebuild()
            for key in ids: self.scene.nodes[key].setSelected(True)
            self.changed('展开子图')
        except ValueError as error:
            self.error(error)

    def open_subgraph(self, node_id):
        from .node_subgraph_editor import open_editor
        self.flush_canvas_changes()
        open_editor(self, node_id)

    def mode_changed(self, _index):
        value = self.mode_combo.currentData()
        if value in self.registry.workflow_types:
            self.graph.update(workflow_type=value, workflow_type_version=self.registry.workflow_types[value].version,
                              execution_mode=self.registry.workflow_types[value].execution_mode)
        else:
            self.graph['execution_mode'] = value
            self.graph.pop('workflow_type', None)
            self.graph.pop('workflow_type_version', None)
        self.changed("切换工作流类型")
        self.inspect_selection()

    def sync_execution_mode(self):
        mode = execution_mode(self.graph)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(self.graph.get('workflow_type') or mode))
        self.mode_combo.blockSignals(False)
        line = mode == "line"
        self.task_action.setEnabled(line and not bool(self.graph.get('workflow_type')))
        self.task_button.setEnabled(line and not bool(self.graph.get('workflow_type')))
        self.task_button.setVisible(not bool(self.graph.get('workflow_type')))
        self.stop_on_error.setEnabled(line)
        self.hint.setText(("Line · 每行任务分别执行，当前任务的数据与结果相互独立；预览使用首项已载入任务。" if line else
                           "Detail · 全局执行一次，不自动遍历任务、不读取当前任务行。") +
                          " 圆形：必填；三角形：可选；竖向长圆角矩形：可连接多个。")
        if line:
            self.task_label.setText(f"已载入 {len(self.bound_tasks)} 项任务 · " + ", ".join(task.label for task in self.bound_tasks[:3])
                                    if self.bound_tasks else "尚未载入任务")
            self.task_label.setToolTip("\n".join(task.label + "\n" + task.directory for task in self.bound_tasks))
        else:
            self.task_label.setText("全局运行 · 只提交一次，不随任务行重复")
            self.task_label.setToolTip("已载入的任务选择会保留；切回 Line 后继续使用，不会带入本次 Detail 执行。")
        if self.graph.get('workflow_type'):
            self.hint.setText('插件提供真实素材并逐个调用；本面板只使用纯色模拟预览。保存到工作流库后，在插件对应的工作流选择框中选择。')
            self.task_label.setText('真实执行由插件启动 · 不读取主程序所选任务行')
            self.run_action.setText('纯色模拟预览输出')
            self.run_button.setText('纯色模拟预览')
        else:
            self.run_action.setText('执行并导出成品')
            self.run_button.setText('执行并导出成品')

    def validate_ui(self):
        self.sync_execution_mode()
        errors,order,_ = validate(self.graph,self.registry)
        from model.node_workflow.typing import resolve_port_types
        self.scene.port_types, _type_conflicts = resolve_port_types(self.graph, self.registry)
        self.scene.update_edges()
        task_mode = self.is_line_mode() and not bool(self.graph.get('workflow_type'))
        if task_mode and not self.bound_tasks:
            entry = next((node["id"] for node in self.graph["nodes"] if node["id"] in order
                          and node["kind"] in {"core.task", "core.task_field"}), self.graph["output_node"])
            errors.append((entry, "请先在主程序选择任务，点击“使用主程序所选任务”。"))
        self.current_errors = errors
        data_output = any(n["id"] == self.graph["output_node"] and n["kind"] == "core.py_output" for n in self.graph["nodes"])
        if data_output and not self.graph.get("workflow_type"):
            self.run_action.setText("执行并查看数据")
            self.run_button.setText("执行数据流程")
        for item in self.scene.nodes.values():
            item.errors = [message for node_id,message in errors if node_id == item.data["id"]]
            item.setToolTip(getattr(item, "base_tooltip", item.definition.description) + ("\n"+"\n".join(item.errors) if item.errors else ""))
            item.update()
        runnable = not errors and (task_mode or not (self.handle and self.handle.isRunning()))
        self.run_action.setEnabled(runnable)
        self.run_button.setEnabled(runnable)
        if task_mode or not self.handle or not self.handle.isRunning():
            self.status.setText((f"需修正 {len(errors)} 项："+errors[0][1]) if errors else
                                f"已就绪 · {len(order)} 个有效节点" +
                                (f" · Line：{len(self.bound_tasks)} 行任务，可提交后台队列" if task_mode else
                                 ' · 插件调用／纯色预览' if self.graph.get('workflow_type') else " · Detail：全局执行一次"))

    def _inspector_callback(self, callback):
        """A retired Qt editor must never write its old snapshot into the graph."""
        generation = self._inspector_generation
        def guarded(*args, **kwargs):
            if generation != self._inspector_generation:
                return False
            return callback(*args, **kwargs)
        return guarded

    def _finish_inspector_edits(self, *, release_focus=False):
        old = self.inspector.widget()
        if old is None:
            return
        # Commit valid, typed-but-not-yet-accepted spinbox text while this
        # editor's generation is still active, before invalidating callbacks.
        for spin in old.findChildren(QtWidgets.QAbstractSpinBox):
            spin.interpretText()
        focus = QtWidgets.QApplication.focusWidget()
        if release_focus and focus is not None and (focus is old or old.isAncestorOf(focus)):
            focus.clearFocus()

    def _update_preview_controls(self):
        node = next((n for n in self.graph['nodes'] if n['id'] == self.inspected), None)
        available = bool(node and self.can_preview_node(node)
                         and not (self.preview_handle and self.preview_handle.isRunning()))
        self.preview_action.setEnabled(available)
        if self._preview_button is not None:
            self._preview_button.setEnabled(available)

    def can_preview_node(self, node):
        from model.node_workflow.workflow_types import can_preview_operations, preview_available
        return preview_available(self.graph, self.registry) and (
            is_preview(node['kind']) or can_preview_operations(self.graph, self.registry, node))

    def inspect_selection(self):
        self._finish_inspector_edits(release_focus=True)
        self._inspector_generation += 1
        self._preview_button = None
        selected = next((i for i in self.scene.selectedItems() if isinstance(i,NodeItem)),None)
        self.inspected = selected.data["id"] if selected else None
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        if selected is None:
            label = QtWidgets.QLabel("点击节点设置参数。\n\n画布按 Tab／右键查找节点；中键移动，滚轮缩放；Alt 点连线添加接点。Ctrl+C/V 复制粘贴，Ctrl+X 剪切，Ctrl+D 复制一份，Ctrl+A 全选；Ctrl+Z 撤销，Ctrl+Y 重做。选择 Begin/End 复制会包含完整循环块。\n\n节点 → Python 原生：流程控制、逻辑判断、列表、字典、JSON、字符串、数学等。循环在“流程控制 → Begin · End 循环块”；条件选择只计算选中的分支。列表索引从 0 开始；取项后用类型转换接回插件。文件菜单有循环与批量分组示例。\n\n节点 → 预览：接上数据后双击查看。文件 → 保存：只填名称，保存在程序工作流库；JSON 导入/导出用于交换副本。关闭面板不取消后台渲染。")
            label.setWordWrap(True)
            layout.addWidget(label)
        else:
            # The graph is authoritative. A scene item may have outlived an
            # earlier by-value snapshot (e.g. save/restore); never seed a new
            # inspector from that item's retired parameter dictionary.
            data = next(node for node in self.graph['nodes'] if node['id'] == selected.data['id'])
            selected.data = data
            definition = self.registry.definitions.get(data['kind'], selected.definition)
            title = QtWidgets.QLabel(definition.title)
            title.setStyleSheet("font-weight:600;")
            layout.addWidget(title)
            description = QtWidgets.QLabel(definition.description)
            description.setWordWrap(True)
            layout.addWidget(description)
            if definition.execution_scope != "any":
                layout.addWidget(QtWidgets.QLabel("适用模式：" + definition.execution_scope.title()))
            if any(port.type_group for port in definition.input_ports(data['params']) + definition.output_ports(data['params'])):
                dtype = self.scene.port_types.get((data["id"], True, "value")) or "尚未确定，接入数据后自动识别"
                layout.addWidget(QtWidgets.QLabel("数据类型：" + dtype))
            if definition.inspector_factory is not None:
                try:
                    content.workflow_services = self.host
                    editor = definition.inspector_factory(dict(data["params"]),
                        self._inspector_callback(partial(self.set_node_parameters, data)), content)
                    if not isinstance(editor, QtWidgets.QWidget):
                        raise TypeError("插件节点编辑器必须返回 QWidget。")
                    layout.addWidget(editor)
                except Exception as error:
                    label = QtWidgets.QLabel("节点设置加载失败：" + str(error))
                    label.setWordWrap(True)
                    layout.addWidget(label)
                    self.error(error)
            for parameter in (() if definition.inspector_factory is not None else definition.parameters):
                if (data["kind"] == "core.preview" and parameter.key == "time"
                        and self.scene.port_types.get((data["id"], True, "value")) not in {None, "", "video", "render_plan"}):
                    continue
                layout.addWidget(QtWidgets.QLabel(parameter.title))
                value = data["params"].get(parameter.key,parameter.default)
                if parameter.kind == 'object':
                    control = QtWidgets.QLabel('公共结构数据 · 由专用编辑器／接口维护')
                    control.setWordWrap(True); layout.addWidget(control)
                elif parameter.kind == "integer":
                    control = QtWidgets.QSpinBox()
                    control.setRange(parameter.minimum,parameter.maximum)
                    control.setValue(value if type(value) is int and parameter.minimum <= value <= parameter.maximum else parameter.default)
                    control.valueChanged.connect(self._inspector_callback(
                        lambda value, d=data, k=parameter.key, c=control: self.set_parameter(d, k, value, c)))
                    layout.addWidget(control)
                elif parameter.kind == "number":
                    control = QtWidgets.QDoubleSpinBox()
                    control.setDecimals(6)
                    control.setRange(parameter.minimum, parameter.maximum)
                    control.setValue(value if type(value) in (int, float) and parameter.minimum <= value <= parameter.maximum
                                     and math.isfinite(value) else parameter.default)
                    control.valueChanged.connect(self._inspector_callback(partial(self.set_parameter,data,parameter.key)))
                    layout.addWidget(control)
                elif parameter.kind == "multiline":
                    control = QtWidgets.QPlainTextEdit(str(value))
                    control.setMinimumHeight(140)
                    control.textChanged.connect(self._inspector_callback(
                        lambda c=control, d=data, k=parameter.key: self.set_parameter(d, k, c.toPlainText())))
                    layout.addWidget(control)
                elif parameter.kind == "choice":
                    control = QtWidgets.QComboBox()
                    for key,text in parameter.choices:
                        control.addItem(text,key)
                    control.setCurrentIndex(max(0,control.findData(value)))
                    control.currentIndexChanged.connect(self._inspector_callback(
                        lambda _i,c=control,d=data,k=parameter.key: self.set_parameter(d,k,c.currentData())))
                    layout.addWidget(control)
                else:
                    row = QtWidgets.QHBoxLayout()
                    control = QtWidgets.QLineEdit(str(value))
                    control.setToolTip(str(value))
                    control.textChanged.connect(self._inspector_callback(partial(self.set_parameter,data,parameter.key)))
                    row.addWidget(control,1)
                    if parameter.kind in {"video_file","subtitle_file","output_file","directory_file","color"}:
                        button = QtWidgets.QToolButton()
                        button.setText("…")
                        button.clicked.connect(self._inspector_callback(partial(self.choose_parameter,control,parameter.kind)))
                        row.addWidget(button)
                    layout.addLayout(row)
            if data["kind"] == "core.import":
                button = QtWidgets.QPushButton("使用当前所选任务的成品 / 字幕…")
                button.clicked.connect(self._inspector_callback(partial(self.import_selected_task,data)))
                layout.addWidget(button)
            if data["kind"] == "core.task":
                button = QtWidgets.QPushButton("使用主程序所选任务")
                button.setEnabled(self.is_line_mode())
                button.clicked.connect(self._inspector_callback(lambda: self.bind_selected_tasks()))
                layout.addWidget(button)
                if self.is_line_mode() and self.bound_tasks:
                    view = QtWidgets.QTreeWidget()
                    view.setHeaderLabels(["输出端", "当前值"])
                    values = _task({}, {}, SimpleNamespace(task=self.bound_tasks[0]))
                    for port in definition.output_ports(data['params']):
                        value = values.get(port.key)
                        value = getattr(value, "path", value)
                        item = QtWidgets.QTreeWidgetItem([port.title, str(value) if value not in (None, "") else "（空）"])
                        item.setToolTip(1, item.text(1))
                        view.addTopLevelItem(item)
                    view.setMinimumHeight(140)
                    layout.addWidget(QtWidgets.QLabel("首项任务的各输出端（只读；任务分别执行）"))
                    layout.addWidget(view)
            if data["kind"] == "core.global_data":
                layout.addWidget(QtWidgets.QLabel("以下为首轮预览示例；预览不会推进循环。"))
                if self.is_line_mode() and not self.bound_tasks:
                    layout.addWidget(QtWidgets.QLabel("先载入任务，才能确定本次循环总数。"))
                else:
                    values = self.global_preview_snapshot().values()
                    values.update(batch_id="提交执行时生成 · 同批次一致", started_at="提交批次时固定", run_date="提交批次的日期")
                    view = QtWidgets.QTreeWidget()
                    view.setHeaderLabels(["输出端", "首轮示例"])
                    for key, title, _kind in GLOBAL_OUTPUTS:
                        value = values[key]
                        text = ("是" if value else "否") if type(value) is bool else str(value)
                        QtWidgets.QTreeWidgetItem(view, [title, text])
                    view.setMinimumHeight(180)
                    layout.addWidget(view)
            if data["kind"] == "core.merge":
                edges = [e for e in self.graph["connections"] if e[2] == data["id"]]
                for index,edge in enumerate(edges,1):
                    source = self.scene.nodes[edge[0]]
                    layout.addWidget(QtWidgets.QLabel(f"{index}. {source.definition.title}"))
            if self.can_preview_node(data):
                button = QtWidgets.QPushButton("打开预览内容" if is_preview(data['kind']) else '纯色模拟此效果')
                self._preview_button = button
                button.setToolTip('执行此节点与上游，在纯色模拟背景上显示效果；不使用真实任务、不导出完整视频。' if self.graph.get('workflow_type') else
                    "执行该节点与上游；使用首项已载入任务，不执行下游导出。可能需要模型分析时间。")
                button.setEnabled(not (self.preview_handle and self.preview_handle.isRunning()))
                button.clicked.connect(self._inspector_callback(partial(self.preview_node, data["id"])))
                layout.addWidget(button)
            messages = [message for node_id,message in self.current_errors if node_id == data["id"]]
            if messages:
                label = QtWidgets.QLabel("\n".join(messages))
                label.setWordWrap(True)
                label.setStyleSheet("color:#c33442;")
                layout.addWidget(label)
        layout.addStretch(1)
        old = self.inspector.takeWidget()
        if old is not None:
            old.deleteLater()
        self.inspector.setWidget(content)
        self._update_preview_controls()

    def set_parameter(self,data,key,value,control=None):
        data = next((node for node in self.graph["nodes"] if node["id"] == data["id"]), None)
        if data is None:
            return
        values = dict(data['params'], **{key: value})
        values.pop(key + '_stamp', None)
        if not self._apply_parameters(data, values, '修改节点参数', key):
            if control is not None:
                with QtCore.QSignalBlocker(control):
                    control.setValue(data['params'][key])
            return False
        return True

    def _apply_parameters(self, data, values, title, merge_key):
        definition = self.registry.definitions[data['kind']]
        old_ports = (definition.input_ports(data['params']), definition.output_ports(data['params']))
        new_ports = (definition.input_ports(values), definition.output_ports(values))
        for output, (old, new) in enumerate(zip(old_ports, new_ports)):
            removed = {p.key for p in old} - {p.key for p in new}
            if any(e[0 if output else 2] == data['id'] and e[1 if output else 3] in removed for e in self.graph['connections']):
                self.error('将删除的端口仍有连线，请先断开；本次未修改或丢弃连线。')
                return False
        if old_ports != new_ports:
            from model.node_workflow.typing import resolve_port_types
            import copy
            candidate = copy.deepcopy(self.graph)
            next(n for n in candidate['nodes'] if n['id'] == data['id'])['params'] = values
            from model.node_workflow.subgraphs import flatten, has_subgraphs
            try:
                old_graph = flatten(self.graph, self.registry)[0] if has_subgraphs(self.graph) else self.graph
                new_graph = flatten(candidate, self.registry)[0] if has_subgraphs(candidate) else candidate
                _, old_conflicts = resolve_port_types(old_graph, self.registry)
                _, new_conflicts = resolve_port_types(new_graph, self.registry)
            except (ValueError, KeyError, TypeError) as error:
                self.error('接口修改未应用：' + str(error)); return False
            if set(new_conflicts) - set(old_conflicts):
                self.error('端口修改会让现有连接类型冲突，请先断开受影响连线；本次未应用。')
                return False
        data['params'] = values
        if old_ports != new_ports:
            self.scene.schedule_refresh()
        self.changed(title, (data['id'], merge_key))
        return True

    def set_node_parameters(self, data, values):
        import copy
        import json
        if not isinstance(values, dict):
            raise TypeError("节点设置必须是普通数据。")
        json.dumps(values, ensure_ascii=False, allow_nan=False)
        data = next((node for node in self.graph["nodes"] if node["id"] == data["id"]), None)
        if data is not None:
            return self._apply_parameters(data, {**data['params'], **copy.deepcopy(values)}, '修改节点设置', 'settings')

    def choose_parameter(self,control,kind,_checked=False):
        if kind == "color":
            node = next((n for n in self.graph['nodes'] if n['id'] == self.inspected), None)
            rgba = node is not None and node['kind'] in {'core.background', 'core.transform', 'core.color_from_hex'}
            value = QtGui.QColor(control.text())
            if rgba:
                from model.node_workflow.contracts import ColorValue
                try:
                    parsed = ColorValue.from_hex(control.text())
                    value = QtGui.QColor.fromRgbF(parsed.red, parsed.green, parsed.blue, parsed.alpha)
                except ValueError:
                    pass
            color = QtWidgets.QColorDialog.getColor(value, self, 'sRGB颜色',
                QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel if rgba else QtWidgets.QColorDialog.ColorDialogOption(0))
            if color.isValid(): control.setText(color.name() + (f'{color.alpha():02x}' if rgba and color.alpha() != 255 else ''))
            return
        root = str(getattr(self.host.main_window,"loaded_project_dir",None) or Path.home())
        if kind == "output_file":
            path,_ = QtWidgets.QFileDialog.getSaveFileName(self,"成品另存到任务目录",control.text() or root,"视频 (*.mp4)")
        elif kind == "directory_file":
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择目录", control.text() or root)
        else:
            filters = "字幕 (*.srt)" if kind == "subtitle_file" else "视频 (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts)"
            path,_ = QtWidgets.QFileDialog.getOpenFileName(self,"选择本地文件",control.text() or root,filters)
        if path:
            control.setText(path)
            if kind == "video_file":
                imported = next((n for n in self.graph["nodes"] if n["id"] == self.inspected),None)
                sibling = Path(path).with_suffix(".srt")
                if imported and not imported["params"].get("subtitles") and sibling.is_file():
                    imported["params"]["subtitles"] = str(sibling)
                self.default_output(path)
                self.inspect_selection()

    def default_output(self,video):
        for node in self.graph["nodes"]:
            if (node["kind"] == "core.render" or node["kind"] == "core.export" and node["params"].get("destination") == "file") and not node["params"].get("output"):
                path = Path(video)
                node["params"]["output"] = str(path.with_name(path.stem+"_字幕.mp4"))
        self.changed()

    def import_selected_task(self,node,_checked=False):
        rows = self.host.main_window.selectedTaskRows()
        targets = self.host.main_window.taskTargetsForRows(rows)
        if len(targets) != 1:
            self.error("原型一次处理一个任务，请在主程序选中一个任务。")
            return
        directory = Path(targets[0]["target_dir"])
        # Never guess which source clip is an edited final. User confirms inside task directory.
        path,_ = QtWidgets.QFileDialog.getOpenFileName(self,"选择此任务已剪辑好的成品视频",str(directory),
                                                      "视频 (*.mp4 *.mkv *.mov *.avi *.webm)")
        if not path: return
        node["params"]["video"] = path
        sibling = Path(path).with_suffix(".srt")
        subtitle = sibling if sibling.is_file() else directory/"task_audio.srt"
        if subtitle.is_file():
            node["params"]["subtitles"] = str(subtitle)
        else:
            node["params"]["subtitles"] = ""
        self.default_output(path)
        self.inspect_selection()

    def add_node(self,kind,position=None):
        try:
            valid_outputs = output_kinds(self.graph, self.registry)
        except ValueError as error:
            self.error(error)
            return
        if len(self.graph["nodes"]) >= MAX_NODES:
            self.error("原型最多支持 128 个节点。")
            return
        self.flush_canvas_changes()
        point = position if isinstance(position, QtCore.QPointF) else self.view.mapToScene(self.view.viewport().rect().center())
        node = new_node(self.registry.definitions[kind],(point.x(),point.y()))
        self.graph["nodes"].append(node)
        if kind in valid_outputs: self.graph["output_node"] = node["id"]
        self.scene.append_nodes((node,), select=node['id'])
        self.changed("添加 · " + self.registry.definitions[kind].title)

    def discard_confirmed(self):
        return not self.dirty or QtWidgets.QMessageBox.question(self,"未保存工作流",
            "当前修改尚未保存，是否放弃这些修改？") == QtWidgets.QMessageBox.StandardButton.Yes

    def replace_graph(self,graph):
        self._inspector_generation += 1
        self.scene.refresh_timer.stop()
        self.graph = graph
        self.scene.graph = graph
        self.scene.rebuild()
        self.history.reset(graph)
        self.sync_connection_style()
        self.dirty = False
        self.validate_ui()
        self.inspect_selection()
        self.fit()
        self.update_preview_states()

    def new_example(self,transport=""):
        if self.discard_confirmed():
            self.path = None
            self.replace_graph(example_graph(self.registry,transport))

    def new_python_example(self):
        if self.discard_confirmed():
            self.path = None
            self.replace_graph(python_example_graph(self.registry))

    def new_batch_data_example(self):
        if self.discard_confirmed():
            self.path = None
            self.replace_graph(batch_example_graph(self.registry))

    def new_loop_example(self, mode="count"):
        if self.discard_confirmed():
            self.path = None
            self.replace_graph(loop_example_graph(self.registry, mode))

    def add_loop_block(self):
        self.flush_canvas_changes()
        point = self.view.mapToScene(self.view.viewport().rect().center())
        try:
            begin, end = add_loop_pair(self.graph, self.registry, (point.x(), point.y()))
        except ValueError as error:
            self.error(error)
            return
        self.scene.append_nodes((begin, end), select=begin['id'])
        self.changed("添加 Begin／End 循环块")

    def new_workflow(self):
        titles = [title for _key, title in EXECUTION_MODES] + [v.title for v in self.registry.workflow_types.values()]
        keys = [key for key, _title in EXECUTION_MODES] + list(self.registry.workflow_types)
        title, ok = QtWidgets.QInputDialog.getItem(self, '新建工作流', '工作流类型', titles, 0, False)
        if not ok or not self.discard_confirmed():
            return
        key = keys[titles.index(title)]
        if key in self.registry.workflow_types:
            graph = self.registry.workflow_types[key].example_factory(self.registry)
        else:
            graph = example_graph(self.registry)
            graph['execution_mode'] = key
        self.path = None
        self.replace_graph(graph)

    def save_to_library(self, save_as=False, *, rename=False):
        self.flush_canvas_changes()
        name = self.graph.get('workflow_name') or (Path(self.path).stem if self.path else '我的工作流')
        if not self.graph.get('workflow_id') or save_as or rename:
            name, ok = QtWidgets.QInputDialog.getText(self, '保存工作流', '工作流名称',
                text=name + (' 副本' if save_as else ''))
            if not ok:
                return False
        try:
            library = WorkflowLibrary()
            value = dict(self.graph)
            if save_as:
                value.pop('workflow_id', None)
            saved = library.save(value, name, self.registry)
            # The library returns a detached persistence snapshot. Replacing
            # nodes here leaves every live canvas item/editor on the old copy:
            # edits write the graph, then reselecting displays obsolete values.
            # Saving only assigns library identity/name; keep live node/edge
            # object identities and the user's selection/layout untouched.
            self.graph.update(workflow_id=saved['workflow_id'], workflow_name=saved['workflow_name'])
            self.path = str(library._path(self.graph['workflow_id']))
            self.history.record(self.graph, "保存工作流信息")
            self.history.stack.setClean()
            self.dirty = False
            self.status.setText('已保存：' + name + '（程序工作流库）；专用类型可在对应插件的“后续处理”中选择。')
            return True
        except Exception as error:
            self.error(error)
            return False

    def open_library(self):
        try:
            rows = WorkflowLibrary().list()
        except (OSError, ValueError, TypeError) as error:
            self.error(error)
            return
        if not rows:
            self.error('工作流库为空，请先使用“文件 → 保存”，填写名称即可。')
            return
        titles = [row['name'] + ' · ' + (row['type'] or '通用') + ' · ' + row['id'][:6] for row in rows]
        title, ok = QtWidgets.QInputDialog.getItem(self, '工作流库', '选择工作流', titles, 0, False)
        if ok and self.discard_confirmed():
            try:
                row = rows[titles.index(title)]
                self.replace_graph(WorkflowLibrary().load(row['id'], self.registry))
                self.path = row['path']
            except Exception as error:
                self.error(error)

    def simulation_settings(self):
        if not self.graph.get('workflow_type'):
            self.error('纯色模拟仅用于插件提供真实输入的工作流。')
            return
        orientation, ok = QtWidgets.QInputDialog.getItem(self, '模拟规格', '画面', ['竖屏 720×1280', '横屏 1280×720', '方形 720×720'], 0, False)
        if not ok: return
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.graph.get('simulation', {}).get('color', '#334155')), self, '模拟背景颜色')
        if not color.isValid(): return
        duration, ok = QtWidgets.QInputDialog.getInt(self, '模拟时长', '秒（仅用于预览）', 5, 1, 30)
        if not ok: return
        sizes = {'竖屏 720×1280': (720, 1280), '横屏 1280×720': (1280, 720), '方形 720×720': (720, 720)}
        width, height = sizes[orientation]
        self.graph['simulation'] = dict(width=width, height=height, color=color.name(), duration=duration)
        self.changed()

    def simulate_output(self):
        if not self.graph.get('workflow_type'):
            self.error('此工作流没有插件模拟输入，请使用普通预览节点。')
            return
        self.preview_node(self.graph['output_node'])

    def new_task_example(self):
        try:
            graph = export_example_graph(self.registry, True)
            if self.discard_confirmed():
                self.path = None
                self.replace_graph(graph)
        except Exception as error:
            self.error(error)

    def new_export_example(self, task_mode=False):
        try:
            graph = export_example_graph(self.registry, task_mode)
            if self.discard_confirmed():
                self.path = None
                self.replace_graph(graph)
        except Exception as error:
            self.error(error)

    def is_line_mode(self):
        return execution_mode(self.graph) == "line"

    def bind_selected_tasks(self, rows=None):
        if not self.is_line_mode():
            self.error("当前是 Detail 全局模式；逐行处理任务请先切换为 Line。")
            return False
        try:
            window = self.host.main_window
            rows = window.selectedTaskRows() if rows is None else rows
            targets = window.taskTargetsForRows(list(rows))
            if not targets:
                self.error("请先在主程序加载任务表格并选中需要执行的任务。")
                return False
            from app_plugins.host import PluginContext
            service = PluginContext(self.host, "node_workflow")
            table = service.current_task_table_path()
            tasks = [capture_task(target, table, service.task_language(target["task"])) for target in targets]
            if len({task.key for task in tasks}) != len(tasks):
                raise ValueError("所选任务有重复目录，请先核对任务编号和类型。")
            self.bound_tasks = tasks
            self.update_preview_states()
            self.task_label.setText(f"已载入 {len(tasks)} 项任务 · " + ", ".join(task.label for task in tasks[:3]))
            self.task_label.setToolTip("\n".join(task.label + "\n" + task.directory for task in tasks))
            self.validate_ui()
            self.inspect_selection()
            return True
        except Exception as error:
            self.error(error)
            return False

    def open_file(self):
        if not self.discard_confirmed(): return
        path,_ = QtWidgets.QFileDialog.getOpenFileName(self,"打开工作流","","节点工作流 (*.json)")
        if not path: return
        try:
            graph = load_graph(path)
            from model.node_workflow.code_node import clear_code_approvals
            clear_code_approvals(graph)
            from model.node_workflow.workflow_types import upgrade_workflow
            graph = upgrade_workflow(graph, self.registry)
            graph, migrated = migrate_task_ports(graph, self.registry)
            # Imported JSON is a new library item on first save, never a silent
            # overwrite of the original library item's exported identity.
            graph.pop('workflow_id', None)
            self.replace_graph(graph)
            self.path = path
            if migrated:
                self.history.stack.resetClean()
                self.dirty = True
                self.error("旧任务工作流已升级为独立字段端口与 listdir；原文件未修改，可另存新版本。")
        except Exception as error:
            self.error(error)

    def save_file(self,save_as=False):
        return self.save_to_library(save_as)

    def export_file(self,save_as=False):
        self.flush_canvas_changes()
        path = self.path
        if not path or save_as:
            root = str(getattr(self.host.main_window,"loaded_project_dir",None) or Path.home())
            path,_ = QtWidgets.QFileDialog.getSaveFileName(self,"保存节点工作流",path or root,"节点工作流 (*.json)")
        if not path: return
        try:
            save_graph(path,self.graph)
            self.status.setText("JSON 副本已导出："+str(path)+"；工作流仍以程序库中的保存状态为准。")
        except Exception as error:
            self.error(error)

    def fit(self):
        rect = self.scene.itemsBoundingRect().adjusted(-30,-30,30,30)
        if not rect.isEmpty():
            self.view.fitInView(rect,QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def run(self):
        if self.graph.get('workflow_type'):
            return self.simulate_output()
        if self.is_line_mode():
            return self.run_tasks()
        if self.handle and self.handle.isRunning(): return
        try:
            payload, resources = prepare_job(self.graph, self.registry, None, self.host)
            graph = payload["graph"]
            endpoint = next(n for n in graph["nodes"] if n["id"] == graph["output_node"])
            if endpoint["kind"] == "core.py_output":
                self.handle = self.host.background_tasks.submit(owner="node_workflow", title="Python 节点 · 数据处理",
                    runner=partial(run_data_workflow, registry=self.registry), payload=payload, kind="cpu", resources=resources)
            else:
                output = endpoint["params"]["output"]
                ffmpeg,_ = resolve_tools()
                payload["ffmpeg"] = ffmpeg
                # Registry callbacks never enter the persisted/deep-copied payload.
                self.handle = self.host.background_tasks.submit(owner="node_workflow",title="节点工作流 · "+Path(output).name,
                    runner=partial(run_workflow,registry=self.registry),payload=payload,kind="media",
                    resources=tuple(sorted(set(resources) | {directory_resource(Path(output).parent)})))
            self.handle.progressChanged.connect(self.on_progress)
            self.handle.completed.connect(self.on_completed)
            self.handle.failed.connect(self.error)
            self.handle.finished.connect(self.on_finished)
            self.cancel_action.setEnabled(True)
            self.progress.setValue(0)
            self.status.setText("已提交后台任务；可隐藏或拖出面板。")
            self.validate_ui()
        except Exception as error:
            self.error(error)

    def run_tasks(self):
        if not self.is_line_mode():
            self.error("逐行执行需要 Line 模式。")
            return
        if not self.bound_tasks:
            self.error("尚未载入主程序所选任务。")
            return
        errors = validate(self.graph, self.registry)[0]
        if errors:
            self.error("\n".join(message for _, message in errors))
            return
        tasks = tuple(self.bound_tasks)
        globals_value = WorkflowGlobals.create("line", len(tasks))
        batch = {"ids": [], "stop_on_error": self.stop_on_error.isChecked()}
        submitted = 0
        for task_index, task in enumerate(tasks):
            key = f"preparation-{self.results.topLevelItemCount()}"
            item = self.results.add(key, task.label, task.directory)
            try:
                payload, resources = prepare_job(self.graph, self.registry, task, self.host,
                                                 global_data=globals_value.at_iteration(task_index + 1))
                ffmpeg, _ = resolve_tools()
                payload["ffmpeg"] = ffmpeg
                output_dir = Path(task.directory) / "节点工作流结果"
                output_node = next(node for node in self.graph["nodes"] if node["id"] == self.graph["output_node"])
                if output_node["kind"] == "core.render" or output_node["kind"] == "core.export" and output_node["params"].get("destination") == "file":
                    output_dir = Path(output_node["params"]["output"]).expanduser().resolve().parent
                    if not output_dir.is_relative_to(Path(task.directory).resolve()):
                        raise ValueError("任务工作流成品必须输出到该任务目录内。")
                resources = tuple(sorted(set(resources) | {directory_resource(output_dir)}))
                handle = self.host.background_tasks.submit(owner="node_workflow", title="任务工作流 · " + task.label,
                    runner=partial(run_task_workflow, registry=self.registry), payload=payload, kind="media", resources=resources)
                self.results.rows[handle.task_id] = self.results.rows.pop(key)
                item.handle = handle
                self.task_handles[handle.task_id] = handle
                batch["ids"].append(handle.task_id)
                self.batch_for_handle[handle.task_id] = batch
                handle.completed.connect(partial(self.task_completed, handle.task_id))
                handle.failed.connect(partial(self.task_failed, handle.task_id))
                handle.finished.connect(self.tasks_finished)
                handle.set_result_handler(partial(self.results.details, item))
                self.task_record_changed(handle.task_id)
                submitted += 1
            except Exception as error:
                self.results.update_result(key, {"status": "failed", "error": str(error), "failed_node": {"title": "准备任务"}})
                self.error(task.label + "：" + str(error))
                if batch["stop_on_error"]:
                    self.host.background_tasks.cancel_many(batch["ids"])
                    for pending in tasks[task_index + 1:]:
                        pending_key = f"preparation-{self.results.topLevelItemCount()}"
                        self.results.add(pending_key, pending.label, pending.directory)
                        self.results.update_result(pending_key, {"status": "skipped", "error": "批次已按遇错停止设置结束。"})
                    break
        self.cancel_action.setEnabled(bool(submitted))
        self.status.setText(f"已提交 {submitted} 项任务；结果将在下方逐项更新，可继续操作主程序。")

    def task_record_changed(self, task_id):
        if task_id not in self.task_handles:
            return
        record = self.host.background_tasks.get(task_id)
        if record["status"] in {"queued", "running", "canceling", "canceled"}:
            self.results.update_result(task_id, {"status": record["status"], "stage": record["stage"]})

    def task_completed(self, task_id, result):
        self.results.update_result(task_id, {**result, "stage": ""})
        if result.get("video"):
            self.last_result = result
        if result.get("notes") and result.get("video"):
            self.host.main_window.appendLog(f"[节点工作流] {result['task_label']}：完成，有 {len(result['notes'])} 条提醒。", end="")

    def task_failed(self, task_id, error):
        record = self.host.background_tasks.get(task_id)
        self.results.update_result(task_id, {"status": "canceled" if record["status"] == "canceled" else "failed", "error": str(error)})
        batch = self.batch_for_handle[task_id]
        if batch["stop_on_error"] and record["status"] == "failed":
            self.host.background_tasks.cancel_many([key for key in batch["ids"] if self.task_handles[key].isRunning()])

    def tasks_finished(self):
        active = sum(handle.isRunning() for handle in self.task_handles.values())
        self.cancel_action.setEnabled(bool(active) or bool(self.handle and self.handle.isRunning())
                                      or bool(self.preview_handle and self.preview_handle.isRunning()))
        if not active:
            self.progress.setValue(100)
            self.status.setText("本面板任务执行结束；下方可查看成品、提醒和失败原因。")

    @QtCore.pyqtSlot(str,object)
    def on_progress(self,message,percent):
        self.status.setText(message)
        if percent is not None: self.progress.setValue(percent)

    @QtCore.pyqtSlot(object)
    def on_completed(self,result):
        if result.get("result_kind") == "data":
            self.progress.setValue(100)
            self.show_preview(result)
            self.status.setText("数据处理完成 · 已打开结果；没有导出媒体文件。")
            return
        self.last_result = result
        self.progress.setValue(100)
        storage = {"file":"文件", "memory":"内存压缩 MP4", "stream":"管道 MP4"}.get(result.get("input_storage"),"文件")
        peak = result.get("compressed_memory_peak",0) / (1024 * 1024)
        self.status.setText(f"已完成 · {result['seconds']:.2f} 秒 · {storage} · 压缩缓存峰值 {peak:.2f} MB · 无中间视频")
        self.host.main_window.appendLog("[节点工作流] 成品："+result["video"],end="")

    @QtCore.pyqtSlot()
    def on_finished(self):
        self.cancel_action.setEnabled(bool(self.preview_handle and self.preview_handle.isRunning())
                                      or any(h.isRunning() for h in self.task_handles.values()))
        # Keep completion/error message instead of replacing it with preflight status.
        message = self.status.text()
        self.validate_ui()
        self.status.setText(message)

    def cancel(self):
        self._queued_previews.clear()
        self._preview_queue_timer.stop()
        for dialog in self.preview_dialogs:
            with QtCore.QSignalBlocker(dialog.auto_refresh):
                dialog.auto_refresh.setChecked(False)
            dialog.refresh_timer.stop()
            if dialog.busy and dialog.node_id != self._active_preview_node:
                dialog.set_busy(False)
        active = [key for key, handle in self.task_handles.items() if handle.isRunning()]
        if active:
            self.host.background_tasks.cancel_many(active)
        if self.handle and self.handle.isRunning():
            self.handle.requestInterruption()
        if self.preview_handle and self.preview_handle.isRunning():
            self.preview_handle.requestInterruption()

    def preview_selected(self):
        node = next((n for n in self.graph["nodes"] if n["id"] == self.inspected), None)
        if node and self.can_preview_node(node):
            self.preview_node(self.inspected)

    def global_preview_snapshot(self):
        mode = execution_mode(self.graph)
        if self.graph.get('workflow_type'):
            return WorkflowGlobals.create(mode, is_preview=True)
        if mode == "line" and not self.bound_tasks:
            raise ValueError("Line 模式请先载入任务，才能预览当前循环数据。")
        return WorkflowGlobals.create(mode, len(self.bound_tasks) if mode == "line" else 1, is_preview=True)

    def preview_signature(self, node_id):
        from dataclasses import asdict
        import json
        try:
            from model.node_workflow.subgraphs import flatten
            flat, resolved, _ = flatten(self.graph, self.registry, node_id)
            _errors, order, _incoming = validate(flat, self.registry, root_id=resolved)
        except ValueError:
            order = [node['id'] for node in self.graph['nodes']]
        ids = set(order)
        value = {'nodes': [{key: node.get(key) for key in ('id', 'kind', 'version', 'params')}
                           for node in (flat if 'flat' in locals() else self.graph)['nodes'] if node['id'] in ids],
                 'connections': [edge for edge in (flat if 'flat' in locals() else self.graph)['connections'] if edge[0] in ids and edge[2] in ids],
                 'scope': {key: self.graph.get(key) for key in ('execution_mode', 'workflow_type', 'workflow_type_version', 'simulation')},
                 'tasks': {'first': asdict(self.bound_tasks[0]), 'count': len(self.bound_tasks)}
                          if self.is_line_mode() and self.bound_tasks else {}}
        dialog = self.preview_dialog(node_id)
        value['preview_max_edge'] = dialog.quality.currentData() if dialog else 1080
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def preview_dialog(self, node_id):
        return next((dialog for dialog in self.preview_dialogs if dialog.node_id == node_id), None)

    def update_preview_states(self):
        from model.node_workflow.subgraphs import find_node
        for dialog in self.preview_dialogs:
            if dialog.node_id:
                valid = find_node(self.graph, dialog.node_id) is not None
                dialog.mark_stale(not valid or dialog.last_signature != self.preview_signature(dialog.node_id), valid=valid)

    def preview_node(self, node_id, _checked=False):
        self.flush_canvas_changes()
        self._closed_previews.discard(node_id)
        if self.preview_handle and self.preview_handle.isRunning():
            self._queued_previews[node_id] = None
            dialog = self.preview_dialog(node_id)
            if dialog:
                dialog.set_busy(True, '预览已排队，前一个完成后刷新；重复修改只保留最新状态。')
            self.status.setText('已加入预览队列。')
            return
        try:
            dialog = self.preview_dialog(node_id)
            signature = self.preview_signature(node_id)
            task = self.bound_tasks[0] if self.is_line_mode() and self.bound_tasks else None
            if self.graph.get('workflow_type'):
                from model.node_workflow.workflow_types import selected_type
                provider = selected_type(self.graph, self.registry)
                if provider.preview_factory is None:
                    raise ValueError('此插件工作流尚未提供预览模拟；可以保存和执行。')
                task = provider.preview_task_factory(self.graph) if provider.preview_task_factory else None
            payload, resources = prepare_job(self.graph, self.registry, task, self.host, root_id=node_id,
                                             global_data=self.global_preview_snapshot())
            payload["preview_node"] = payload.get('resolved_root') or node_id
            payload['preview_alias'] = node_id
            payload['preview_max_edge'] = dialog.quality.currentData() if dialog else 1080
            from model.node_workflow.subgraphs import find_node
            node = find_node(self.graph, node_id)
            title = self.registry.definitions[node["kind"]].title
            self.preview_handle = self.host.background_tasks.submit(owner="node_workflow", title="节点预览 · " + title,
                runner=partial(run_preview, registry=self.registry), payload=payload, kind="media", resources=resources)
            self._preview_signatures[node_id] = signature
            self._active_preview_node = node_id
            if dialog:
                dialog.set_busy(True)
            self.preview_handle.completed.connect(self.show_preview)
            self.preview_handle.failed.connect(self.preview_failed)
            self.preview_handle.finished.connect(self.preview_finished)
            self.preview_handle.progressChanged.connect(self.on_progress)
            self.cancel_action.setEnabled(True)
            self.status.setText("正在预览到此节点，不执行下游、不导出完整视频。")
            self._update_preview_controls()
        except Exception as error:
            self.error(error)
            dialog = self.preview_dialog(node_id)
            if dialog:
                dialog.set_busy(False)
                dialog.show_error(error)

    @QtCore.pyqtSlot(str)
    def preview_failed(self, error):
        self.error(error)
        dialog = self.preview_dialog(self._active_preview_node)
        if dialog:
            dialog.show_error(error)

    @QtCore.pyqtSlot(object)
    def show_preview(self, result):
        try:
            node_id = result.get('node_id', '')
            if node_id in self._closed_previews:
                return
            from model.node_workflow.subgraphs import find_node
            if node_id and find_node(self.graph, node_id) is None:
                return
            dialog = self.preview_dialog(node_id) if node_id else None
            if dialog is None:
                dialog = NodePreviewDialog(result, self)
                self.preview_dialogs.append(dialog)
                dialog.refreshRequested.connect(lambda d=dialog: self.preview_node(d.node_id))
                dialog.finished.connect(lambda _code, d=dialog: self.preview_closed(d))
            else:
                dialog.update_result(result)
            dialog.last_signature = self._preview_signatures.get(node_id, self.preview_signature(node_id)) if node_id else None
            if node_id:
                dialog.mark_stale(dialog.last_signature != self.preview_signature(node_id))
            dialog.show()
            self.status.setText("预览完成；可继续修改节点或执行导出。")
        except Exception as error:
            self.error("显示预览失败：" + str(error))

    def preview_closed(self, dialog):
        if dialog in self.preview_dialogs:
            self.preview_dialogs.remove(dialog)
        if dialog.node_id:
            self._queued_previews.pop(dialog.node_id, None)
            self._closed_previews.add(dialog.node_id)

    @QtCore.pyqtSlot()
    def preview_finished(self):
        dialog = self.preview_dialog(self._active_preview_node)
        if dialog:
            dialog.set_busy(False)
        self.preview_handle = None
        self._active_preview_node = None
        self.cancel_action.setEnabled(bool(self.handle and self.handle.isRunning()) or any(h.isRunning() for h in self.task_handles.values()))
        self._update_preview_controls()
        self.update_preview_states()
        if self._queued_previews:
            self._preview_queue_timer.start(0)

    def start_next_preview(self):
        if self._queued_previews:
            node_id = next(iter(self._queued_previews))
            self._queued_previews.pop(node_id)
            if node_id not in self._closed_previews and any(node['id'] == node_id for node in self.graph['nodes']):
                self.preview_node(node_id)
            elif self._queued_previews:
                self._preview_queue_timer.start(0)

    def open_output(self):
        if self.last_result:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(self.last_result["video"]).parent)))
        else:
            self.error("尚无已完成的成品。")


class NodeWindowController:
    def __init__(self,window):
        self.window = window
        self.frame = self.panel = None
        self.menu = window.main_menu_bar.addMenu("工作流")
        self.action = self.menu.addAction("节点工作流…")
        self.action.triggered.connect(self.show)
        self.attach_action = self.menu.addAction("节点窗口吸附主窗口")
        self.attach_action.setCheckable(True)
        self.attach_action.triggered.connect(self.toggle_attachment)

    def create(self):
        if self.frame is not None: return
        frame = NodeWindow(self.window)
        frame.placed = False
        panel = NodeWorkspace(self.window.plugin_host, frame)
        frame.setCentralWidget(panel)
        self.frame, self.panel = frame, panel
        frame.attachmentChanged.connect(self.attach_action.setChecked)
        window_menu = panel.menu.addMenu('窗口')
        local_attach = window_menu.addAction('吸附主窗口 · 移动时跟随')
        local_attach.setCheckable(True)
        local_attach.triggered.connect(self.toggle_attachment)
        frame.attachmentChanged.connect(local_attach.setChecked)
        window_menu.addAction('脱离主窗口', lambda: frame.set_attached(False))
        screen = self.window.screen().availableGeometry()
        frame.resize(min(1180, screen.width() - 40), min(780, screen.height() - 80))

    def show(self,_checked=False):
        try:
            self.create()
            first_show = not self.frame.placed
            self.frame.show()
            if first_show and not self.frame.attached:
                self.frame.move_frame(self.window.frameGeometry().topLeft() + QtCore.QPoint(40, 40))
            self.frame.placed = True
            self.frame.follow_owner()
            self.frame.raise_()
            self.frame.activateWindow()
            if first_show:
                self.panel.fit()
        except Exception as error:
            self.window.appendLog("[节点工作流] "+str(error),end="")
            QtWidgets.QMessageBox.warning(self.window,"节点面板无法打开",str(error))

    def toggle_attachment(self, checked=False):
        self.show()
        if self.frame is not None:
            self.frame.set_attached(checked)

    def open_workflow(self, workflow_id='', workflow_type=''):
        """Public GUI entry; plugins never reparent this independent window."""
        self.create()
        registry = self.panel.registry
        if workflow_type not in registry.workflow_types:
            raise ValueError('工作流类型不可用，请启用对应插件并重启。')
        if workflow_id and self.panel.graph.get('workflow_id') == workflow_id:
            self.show(); return True
        graph = (WorkflowLibrary().load(workflow_id, registry) if workflow_id else
                 registry.workflow_types[workflow_type].example_factory(registry))
        if graph.get('workflow_type') != workflow_type:
            raise ValueError('工作流类型不匹配，未替换当前编辑内容。')
        if not self.panel.discard_confirmed(): return False
        self.panel.path = None
        self.panel.replace_graph(graph)
        self.show(); self.panel.fit()
        return True

    def populate_task_menu(self, menu, rows):
        registry = self.window.plugin_host.available_nodes()
        if "smart_video_editor.edit" not in registry.definitions or "batch_text_video.caption" not in registry.definitions:
            return
        submenu = menu.addMenu("节点工作流")
        submenu.addAction("载入所选任务", lambda: self.use_tasks(rows))
        submenu.addAction("运行所选任务（当前工作流）", lambda: self.use_tasks(rows, True))

    def use_tasks(self, rows, run=False):
        self.show()
        if self.panel is None:
            return
        if not self.panel.is_line_mode():
            self.panel.new_task_example()
        if self.panel.is_line_mode() and self.panel.bind_selected_tasks(rows) and run:
            self.panel.run()

    def can_close(self):
        # Main application owns cancellation/wait for active background jobs.
        return self.panel is None or self.panel.discard_confirmed()

    def hide(self):
        if self.frame is not None:
            self.frame._hidden_for_owner = False
            self.frame.hide()
