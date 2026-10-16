"""Node inspector reuses the original layer, typography and visual editors."""
import copy
from qt_compat import QtWidgets
from .layer_ui import LayerPanel
from .workflow_layers import normalize_layer


class NodeLayerPanel(LayerPanel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,inspector_mode='node_layer',**kwargs)

    def select_layer(self, row):
        if self._loading: return
        super().select_layer(row)
        layer = self.current()
        if not layer: return
        text = layer["kind"] == "text"
        source = self.content_editor.source
        source.blockSignals(True)
        for index in range(source.count()-1, -1, -1):
            if source.itemData(index) not in {"fixed", "file"}: source.removeItem(index)
        source.blockSignals(False)
        self.content_editor.allow_copy_pick = False
        self.content_editor.show_mode()
        self.content_editor.pool_field.setVisible(text)
        self.hint.setText("连接资源池优先，其次文字／素材路径输入；未连接时使用这里的固定来源。"
                          "位置按实际视频比例计算；像素值以参考画布换算。预览请接到解算器之后。")


class LayerNodeEditor(QtWidgets.QWidget):
    def __init__(self, params, callback, parent=None):
        super().__init__(parent)
        from .layout import ensure_fonts
        ensure_fonts()
        self.params, self.callback = copy.deepcopy(params), callback
        self.kind = params["layer"]["kind"]
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#c33442")
        self.error.hide()
        root.addWidget(self.error)
        enabled = QtWidgets.QCheckBox("启用此图层")
        enabled.setChecked(params["layer"].get("enabled", True))
        root.addWidget(enabled)
        form = QtWidgets.QFormLayout()
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.pool_mode = QtWidgets.QComboBox()
        self.pool_mode.addItem("随当前循环次数轮换", "rotate")
        self.pool_mode.addItem("固定使用第一项", "first")
        self.pool_mode.setCurrentIndex(max(0, self.pool_mode.findData(params.get("pool_mode", "rotate"))))
        form.addRow("已连接资源池时", self.pool_mode)
        self.offset = QtWidgets.QSpinBox()
        self.offset.setRange(0, 100000)
        self.offset.setValue(params.get("offset", 0))
        form.addRow("起点偏移（0为第一项）", self.offset)
        self.skip_empty = QtWidgets.QCheckBox('空白文字时跳过，不阻止本视频') if self.kind == 'text' else None
        if self.skip_empty is not None:
            self.skip_empty.setChecked(params.get('skip_empty', False))
            self.skip_empty.toggled.connect(self.edit)
            form.addRow(self.skip_empty)
        root.addLayout(form)
        layer = normalize_layer(params["layer"], self.kind)
        self.panel = NodeLayerPanel([layer], "", self)
        self.panel.select_layer_id(layer["id"])
        self.panel.hide_catalog()
        root.addWidget(self.panel)
        self.panel.changed.connect(self.edit)
        self.pool_mode.currentIndexChanged.connect(self.edit)
        self.offset.valueChanged.connect(self.edit)
        enabled.toggled.connect(self.toggle)

    def toggle(self, value):
        self.panel.current()["enabled"] = value
        self.edit()

    def edit(self, *_):
        layer = next(item for item in self.panel.values() if item["kind"] == self.kind)
        self.panel.content_editor.pool_field.setVisible(self.kind == "text")
        try:
            layer = normalize_layer(layer, self.kind)
            self.error.hide()
        except Exception as error:
            self.error.setText(str(error)); self.error.show()
        try:
            patch = {"layer": layer, "pool_mode": self.pool_mode.currentData(), "offset": self.offset.value()}
            if self.skip_empty is not None: patch['skip_empty'] = self.skip_empty.isChecked()
            self.callback(patch)
        except Exception as error:
            self.error.setText("设置未应用：" + str(error)); self.error.show()


def layer_inspector(params, changed, parent=None):
    from .component_inspector import require_editor_api
    require_editor_api(LayerPanel)
    return LayerNodeEditor(params, changed, parent)
