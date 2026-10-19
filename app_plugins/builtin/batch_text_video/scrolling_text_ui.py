"""Text motion controls shared by the batch editor and text node inspector."""
import copy
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo
from .scrolling_text import DEFAULTS, normalize_scroll


class MotionSpin(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class ScrollTextProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer, self.canvas_height, self.loading = None, 1920, False
        self._last_direction = {}
        self.enable_control = QtWidgets.QCheckBox('启用文字滚动')
        self.enable_control.toggled.connect(self._toggle_enabled)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        form = QtWidgets.QFormLayout()
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mode = Combo()
        for label, value in (("静态文字", "none"), ("向上滚动 ↑", "up"), ("向下滚动 ↓", "down")):
            self.mode.addItem(label, value)
        form.addRow("文字动画", self.mode)
        self.start_anchor, self.end_anchor = Combo(), Combo()
        for widget, outside_label in ((self.start_anchor, '从框外进入'), (self.end_anchor, '滚出框外')):
            for label, value in ((outside_label,'outside'), ('首行贴顶','top'), ('整段居中','center'), ('末行贴底','bottom')):
                widget.addItem(label, value)
            widget.currentIndexChanged.connect(self._edit)
        form.addRow('起点位置', self.start_anchor)
        form.addRow('终点位置', self.end_anchor)
        self.start_anchor.setToolTip('先选起点，再用起点偏移微调。向上滚动的框外起点在下方，向下滚动则在上方。')
        self.end_anchor.setToolTip('按实际文案高度计算终点；选择末行贴底可在最后一段仍可见时结束滚动。')
        self.units = Combo()
        self.units.addItem("框高度百分比 %", "percent")
        self.units.addItem("像素 px", "pixels")
        self.units.setToolTip("位置偏移相对上面选定的起点／终点，正数向下，负数向上。单位切换不改变动画。")
        form.addRow("位置偏移单位", self.units)
        self.controls = {}
        for key, label, low, high, suffix in (
            ("scroll_start_delay", "开始延迟", 0, 86400, " 秒"),
            ("scroll_end_early", "提前结束", 0, 86400, " 秒"),
            ("scroll_start_offset", "文字起点偏移", -10000, 10000, " %"),
            ("scroll_end_offset", "文字终点偏移", -10000, 10000, " %"),
            ("scroll_edge_fade", "边缘渐隐范围", 0, 50, " %"),
        ):
            spin = MotionSpin()
            spin.setRange(low, high)
            spin.setDecimals(2)
            spin.setSingleStep(.1 if suffix == " 秒" else 1)
            spin.setSuffix(suffix)
            self.controls[key] = spin
            form.addRow(label, spin)
            spin.valueChanged.connect(self._edit)
        self.controls["scroll_start_delay"].setToolTip("先停在起点，再开始滚动。按最终视频时长计算。")
        self.controls['scroll_start_offset'].setToolTip('相对“起点位置”微调；正数向下、负数向上。100%等于一个文字框高度，动画快览选择“开头”可检查实际起点。')
        self.controls['scroll_end_offset'].setToolTip('相对“终点位置”微调；正数向下、负数向上。文案视频画面下方点击动画快览“结尾”即可检查终点，不必先渲染视频。')
        self.controls["scroll_end_early"].setToolTip("提前到达终点，余下时间停在终点。两项之和必须小于最终视频时长。")
        self.controls["scroll_edge_fade"].setToolTip("框顶部和底部各占框高度的此比例；0 = 硬裁切。只渐隐文字，不渐隐背景底板。")
        from .property_pairs import pair_form_fields
        pair_form_fields(form,'滚动位置',[('start','起点',self.start_anchor),('end','终点',self.end_anchor)])
        pair_form_fields(form,'动画时间',[('start','延迟',self.controls['scroll_start_delay']),
            ('end','提前结束',self.controls['scroll_end_early'])])
        pair_form_fields(form,'文字偏移',[('start','起点',self.controls['scroll_start_offset']),
            ('end','终点',self.controls['scroll_end_offset'])])
        root.addLayout(form)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        root.addWidget(self.info)
        note = QtWidgets.QLabel(
            "起点／终点可选框外、首行贴顶、整段居中或末行贴底；偏移在所选位置上继续微调。\n"
            "选择框外时，向上＝框下方→框上方；向下反向。正数向下，负数向上。\n"
            "自定义起终点后，实际运动方向由两者位置决定。\n"
            "框外文字始终裁切。字号使用“样式”的最大字号，不因长文缩小；横向对齐仍有效。\n"
            "竖排时每次换行开始新列，各列整段上下滚动；列宽放不下会提示调整框宽或字号。\n"
            "运动覆盖最终成品时长（启用人声时跟随人声），扣除开始延迟和提前结束。\n"
            "文案视频画面下方拖动动画快览可检查中途／结尾；默认在开头，框外起点可能看不到文字。\n"
            "快览背景保持首帧；节点文字使用已保存工作流。复杂视频／字幕效果用播放预览核对。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        from .property_sections import PropertySection
        self.help_section=PropertySection('滚动说明',note)
        root.addWidget(self.help_section)
        self.mode.currentIndexChanged.connect(self._edit)
        self.units.currentIndexChanged.connect(self._units_changed)
        self.set_layer(None)

    def set_layer(self, layer, canvas_height=1920):
        self.loading = True
        self.layer = copy.deepcopy(layer) if layer else None
        self.canvas_height = canvas_height
        self.setEnabled(bool(layer))
        if layer:
            normalize_scroll(self.layer)
            self.mode.setCurrentIndex(self.mode.findData(self.layer["text_animation"]))
            blocker = QtCore.QSignalBlocker(self.enable_control)
            self.enable_control.setChecked(self.layer['text_animation'] != 'none')
            del blocker
            self.start_anchor.setCurrentIndex(self.start_anchor.findData(self.layer['scroll_start_anchor']))
            self.end_anchor.setCurrentIndex(self.end_anchor.findData(self.layer['scroll_end_anchor']))
            self.units.setCurrentIndex(self.units.findData(self.layer["scroll_offset_units"]))
            for key, spin in self.controls.items():
                if key not in {"scroll_start_offset", "scroll_end_offset"}:
                    spin.setValue(self.layer[key])
            self._refresh_offsets()
            self._enabled()
        self.loading = False

    def _toggle_enabled(self, enabled):
        if self.loading or not self.layer:
            return
        identifier = self.layer['id']
        if self.mode.currentData() != 'none':
            self._last_direction[identifier] = self.mode.currentData()
        self.mode.setCurrentIndex(self.mode.findData(self._last_direction.get(identifier, 'up') if enabled else 'none'))

    def _factor(self):
        return self.canvas_height*self.layer["height"]/10000 if self.units.currentData() == "pixels" else 1

    def _refresh_offsets(self):
        factor = self._factor()
        for key in ("scroll_start_offset", "scroll_end_offset"):
            spin = self.controls[key]
            spin.setRange(-10000*factor, 10000*factor)
            spin.setDecimals(3)
            spin.setSuffix(" px" if self.units.currentData() == "pixels" else " %")
            spin.setValue(self.layer[key]*factor)
        self.info.setText(f"当前框高度：{self.canvas_height*self.layer['height']/100:.1f} px；100% 偏移 = 一个框高度。正数向下，负数向上。")

    def _enabled(self):
        active = self.mode.currentData() != "none"
        blocker = QtCore.QSignalBlocker(self.enable_control)
        self.enable_control.setChecked(active)
        del blocker
        if active and self.layer:
            self._last_direction[self.layer['id']] = self.mode.currentData()
        for widget in (self.units, self.start_anchor, self.end_anchor, *self.controls.values()):
            widget.setEnabled(active)

    def _units_changed(self):
        if self.loading or not self.layer:
            return
        self.loading = True
        self.layer["scroll_offset_units"] = self.units.currentData()
        self._refresh_offsets()
        self.loading = False
        self.changed.emit({key: self.layer[key] for key in DEFAULTS})

    def _edit(self, *_):
        if self.loading or not self.layer:
            return
        factor = self._factor()
        for key, spin in self.controls.items():
            self.layer[key] = spin.value()/factor if key in {"scroll_start_offset", "scroll_end_offset"} else spin.value()
        self.layer["text_animation"] = self.mode.currentData()
        self.layer['scroll_start_anchor'] = self.start_anchor.currentData()
        self.layer['scroll_end_anchor'] = self.end_anchor.currentData()
        self._enabled()
        self.changed.emit({key: self.layer[key] for key in DEFAULTS})
