"""GUI-only named workflow selection; no graph execution in an editor signal."""
from qt_compat import QtCore, QtWidgets
from .node_effects import TYPE_ID


class NodeEffectProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)
    message = QtCore.pyqtSignal(str)

    def __init__(self, services=None, parent=None):
        super().__init__(parent)
        self.services, self.layer, self.loading = services, None, False
        layout = QtWidgets.QVBoxLayout(self)
        self.workflow = QtWidgets.QComboBox()
        self.workflow.setMinimumWidth(0)
        self.workflow.setSizeAdjustPolicy(self.workflow.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.workflow.setMinimumContentsLength(12)
        self.workflow.currentIndexChanged.connect(self.edit)
        layout.addWidget(QtWidgets.QLabel('来源：程序工作流库 · 文案视频节点效果'))
        layout.addWidget(self.workflow)
        row = QtWidgets.QHBoxLayout()
        self.refresh = QtWidgets.QPushButton('刷新列表')
        self.open = QtWidgets.QPushButton('编辑工作流…')
        self.new = QtWidgets.QPushButton('新建工作流…')
        for button in (self.refresh, self.open, self.new):
            button.setAutoDefault(False); row.addWidget(button)
        self.refresh.clicked.connect(self.reload)
        self.open.clicked.connect(lambda: self.open_editor(False))
        self.new.clicked.connect(lambda: self.open_editor(True))
        layout.addLayout(row)
        self.status = QtWidgets.QLabel(); self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status)
        note = QtWidgets.QLabel('输入是当前已分配的背景视频、任务／人声SRT、标题正文、最终时长与画布。\n'
            '“返回视频操作”可以连接多个操作节点，按连线顺序合成；不用再接解算或导出节点。\n\n'
            '此控件所在位置决定操作覆盖顺序，上方图层在节点效果之后再叠加。位置、字体等在工作流里设置。\n'
            '默认按视频身份随机，同一视频播放预览与导出可复现；全局循环值按当前完整队列位置提供。\n\n'
            '编辑首帧不执行节点。请点击“播放预览”查看真实效果；节点面板的模拟预览不使用真实任务。\n'
            '效果节点可双击／右键预览，或在属性中点“纯色模拟此效果”；最终输出也可模拟。\n'
            '已有控件可在图层列表右键“转换为节点效果…”：保留设置、自动接线并替换，不用从零创建。\n'
            '随机配色节点可接文字／字幕的“文字颜色、底色”等颜色端口。\n'
            '手工新建后，在节点面板“文件 → 保存”填写名称，再回到这里刷新并选择。')
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(note); layout.addStretch()
        self.setEnabled(False)

    def library(self):
        from model.node_workflow.library import WorkflowLibrary
        return self.services.workflow_library if self.services is not None and hasattr(self.services, 'workflow_library') else WorkflowLibrary()

    def reload(self):
        wanted = self.layer.get('workflow_id', '') if self.layer else ''
        self.loading = True
        self.workflow.clear(); self.workflow.addItem('请选择已保存的节点效果工作流', '')
        try:
            rows = self.library().list(TYPE_ID)
            for row in rows: self.workflow.addItem(row['name'], row['id'])
            self.status.setText('已找到 '+str(len(rows))+' 个匹配的工作流。' if rows else '尚无此类型工作流，可直接点击“新建工作流”。')
        except Exception as error:
            self.status.setText('读取工作流库失败：'+str(error))
        index = self.workflow.findData(wanted)
        if wanted and index < 0:
            self.workflow.addItem('原工作流暂不可用 · '+wanted[:8], wanted)
            index = self.workflow.count()-1
            self.status.setText('原选择已保留；请检查工作流是否删除、插件是否启用、加密数据盘是否可用。')
        self.workflow.setCurrentIndex(max(0,index))
        self.loading = False

    def set_layer(self, layer):
        self.layer = dict(layer) if layer else None
        self.setEnabled(bool(layer))
        if layer: self.reload()

    def edit(self, _index):
        if self.loading or not self.layer: return
        value = self.workflow.currentData() or ''
        if value != self.layer['workflow_id']:
            self.layer['workflow_id'] = value
            self.changed.emit(dict(self.layer))

    def open_editor(self, create):
        try:
            callback = getattr(self.services, 'open_workflow_editor', None)
            if not callable(callback): raise ValueError('请从主程序打开此插件，再使用节点工作流编辑入口。')
            value = '' if create else self.workflow.currentData()
            if not create and not value: raise ValueError('请先选择一个已保存的工作流。')
            callback(workflow_id=value or '', workflow_type=TYPE_ID)
        except Exception as error:
            self.status.setText(str(error)); self.message.emit(str(error))


def node_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    panel = ComponentPropertyPanel(mode=mode)
    panel.node_effect_properties = panel.add_bound('node', '节点效果工作流', NodeEffectProperties(services),
        fields={'workflow_id'}, expanded=True)
    panel.node_effect_properties.message.connect(panel.message)
    return panel
