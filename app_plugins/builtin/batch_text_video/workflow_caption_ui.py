"""Node properties reuse the batch plugin's real caption/style editors."""
import copy

from qt_compat import QtCore, QtGui, QtWidgets
from .layer_ui import LayerPanel
from .workflow_caption import normalize_node_caption


class CaptionLayerPanel(LayerPanel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,inspector_mode='node_caption',**kwargs)

    def select_layer(self, row):
        super().select_layer(row)
        # The subtitle input port owns the source. Do not offer competing
        # task/audio/model/file pickers or unrelated layer types here.
        self.hint.setText("字幕框按实际输入视频的比例适配。把字幕操作接入视频解算器，再接预览节点查看真实动画。")


class CaptionNodeEditor(QtWidgets.QWidget):
    def __init__(self, params, changed, parent=None):
        super().__init__(parent)
        from .layout import ensure_fonts
        from .captions import new_caption
        ensure_fonts()
        self.params, self.callback = copy.deepcopy(params), changed
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("基础静态字幕", "basic")
        self.mode.addItem("文案视频字幕 · 完整样式库", "batch")
        root.addWidget(self.mode)
        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color:#c33442;")
        self.error_label.hide()
        root.addWidget(self.error_label)
        self.basic = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.basic)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.basic_controls = {}
        for key, title, low, high, default in (("size", "字号（ASS基准）", 8, 120, 24),
                                               ("outline", "暗红描边厚度", 0, 8, 2)):
            spin = QtWidgets.QSpinBox()
            spin.setRange(low, high)
            spin.setValue(params.get(key, default))
            spin.valueChanged.connect(lambda value, key=key: self.basic_change(key, value))
            self.basic_controls[key] = spin
            form.addRow(title, spin)
        for key, title, default in (("font", "字体", "Arial"), ("color", "文字颜色", "#ffffff")):
            field = QtWidgets.QLineEdit(str(params.get(key, default)))
            field.textChanged.connect(lambda value, key=key: self.basic_change(key, value))
            self.basic_controls[key] = field
            if key == "color":
                row = QtWidgets.QHBoxLayout()
                row.addWidget(field)
                button = QtWidgets.QToolButton()
                button.setText("…")
                button.clicked.connect(self.choose_color)
                row.addWidget(button)
                form.addRow(title, row)
            else:
                form.addRow(title, field)
        position = QtWidgets.QComboBox()
        for title, value in (("下方", "bottom"), ("中间", "middle"), ("上方", "top")):
            position.addItem(title, value)
        position.setCurrentIndex(max(0, position.findData(params.get("position", "bottom"))))
        position.currentIndexChanged.connect(lambda _i: self.basic_change("position", position.currentData()))
        self.basic_controls["position"] = position
        form.addRow("垂直位置", position)
        root.addWidget(self.basic)
        layer = copy.deepcopy(params.get("caption")) if params.get("caption") is not None else new_caption()
        layer["caption_timing"] = layer.get("caption_timing", "task") if params.get("caption") is not None else "task"
        layer = normalize_node_caption(layer)
        self.panel = CaptionLayerPanel([layer], "", self)
        self.panel.select_layer_id(layer["id"])
        self.panel.hide_catalog()
        properties = self.panel.caption_properties
        timing = properties.controls["caption_timing"]
        timing.blockSignals(True)
        timing.clear()
        timing.addItem("直接使用输入字幕原时间戳", "task")
        timing.addItem("匀速动画 · 块内近似逐词", "uniform")
        timing.setCurrentIndex(timing.findData(layer["caption_timing"]))
        timing.blockSignals(False)
        for key in ("caption_source", "caption_language"):
            control = properties.controls[key]
            control.hide()
            label = properties.form_for(control).labelForField(control)
            if label is not None:
                label.hide()
        for label in properties.findChildren(QtWidgets.QLabel):
            if label.text().startswith("固定 SRT"):
                label.setText("字幕来源由节点输入端决定；不加载对齐模型，不读取隐含任务数据。")
            elif label.text().startswith("原有模式逐词"):
                label.setText("同文案视频的完整字幕样式、图库和效果参数。输入是整块SRT时整块一起出现；需要近似逐词可选“匀速动画”。"
                              "随机字号对比度大于1×会强化大小差异，仍受字号范围与框尺寸限制；位置比例按解算器的视频规格计算。")
        self.panel.canvas_label.setToolTip("像素值以此参考画布换算，保存的是比例；最终字幕采用实际输入视频尺寸。")
        self.panel.changed.connect(self.caption_changed)
        root.addWidget(self.panel)
        self.mode.setCurrentIndex(1 if params.get("caption") is not None else 0)
        self.update_visibility()
        self.mode.currentIndexChanged.connect(self.mode_changed)

    def update_visibility(self):
        styled = self.mode.currentData() == "batch"
        self.basic.setVisible(not styled)
        self.panel.setVisible(styled)

    def basic_change(self, key, value):
        self.params[key] = value
        self.notify({key: value})

    def notify(self, values):
        try:
            self.callback(values)
        except Exception as error:
            self.error_label.setText("设置未应用：" + str(error))
            self.error_label.show()

    def choose_color(self):
        field = self.basic_controls["color"]
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(field.text()), self, "字幕颜色")
        if color.isValid():
            field.setText(color.name())

    def mode_changed(self, _index):
        self.update_visibility()
        if self.mode.currentData() == "basic":
            self.error_label.hide()
            self.notify({"caption": None})
        else:
            self.caption_changed()

    def caption_changed(self):
        layer = next(item for item in self.panel.values() if item["kind"] == "caption")
        try:
            layer = normalize_node_caption(layer)
            self.error_label.hide()
        except Exception as error:
            # Keep the invalid draft visible and let graph preflight disable
            # execution, never fall back silently to an older valid style.
            self.error_label.setText(str(error))
            self.error_label.show()
        self.notify({"caption": layer})


def caption_inspector(params, changed, parent=None):
    from .component_inspector import require_editor_api
    require_editor_api(LayerPanel)
    return CaptionNodeEditor(params, changed, parent)
