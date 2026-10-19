"""One encapsulated four-corner watermark, with private rendering elements."""
import copy
import math
from qt_compat import QtCore, QtWidgets
from .components import new_text_component


CORNERS = (("top_left", "左上", "left", "top", 25, 7),
           ("top_right", "右上", "right", "top", 75, 7),
           ("bottom_left", "左下", "left", "bottom", 25, 93),
           ("bottom_right", "右下", "right", "bottom", 75, 93))
PREFIX = "corner_watermark_"
OBJECT_ID = "corner_watermarks"
POSITION_DEFAULTS = {"watermark_placement": "free", "watermark_inset_unit": "pixels",
                     "watermark_inset_x": 12., "watermark_inset_y": 12.}


def new_watermark():
    corners = {}
    for key, label, align, vertical, x, y in CORNERS:
        child = new_text_component()
        child.update(id="watermark_part_"+key, name="四角水印 · "+label,
            enabled=False, font="Poppins", font_weight=500, font_min=20, font_max=32,
            x=x, y=y, width=44, height=8, align=align, vertical=vertical,
            color="#fff8ee", opacity=92, outline_width=1, outline_color="#8b3545",
            outline_opacity=80, shadow_enabled=False, shadow_color="#8b3545",
            background_enabled=False, background_color="#8b3545", padding=4, line_spacing=1.1,
            word_spacing=0, word_spacing_version=2)
        child.update(POSITION_DEFAULTS)
        child["watermark_placement"] = "corner"
        corners[key] = child
    return {"id": OBJECT_ID, "kind": "watermark", "name": "四角水印", "enabled": False,
            "opacity": 100, "source": "fixed", "corners": corners}


def migrate_watermarks(layers):
    """Collapse only known legacy helper layers; never mutate user input.

    Retain individual content, style and position. The group occupies the old
    topmost watermark's stacking position; unrelated layers retain their order.
    Ambiguous identifiers or non-fixed user layers are deliberately left alone.
    """
    result = copy.deepcopy(layers)
    if not isinstance(result, list) or any(isinstance(x, dict) and x.get("kind") == "watermark" for x in result):
        return result
    old_ids = {PREFIX+key: key for key, *_ in CORNERS}
    legacy = [(i, old_ids[item["id"]], item) for i, item in enumerate(result)
              if isinstance(item, dict) and item.get("id") in old_ids
              and item.get("kind") == "text" and item.get("source", "fixed") == "fixed"]
    if not legacy or any(isinstance(x, dict) and x.get("id") == OBJECT_ID for x in result):
        return result
    if len({key for _, key, _ in legacy}) != len(legacy):
        raise ValueError("旧水印图层编号重复，未自动合并。")
    obj = new_watermark()
    for _index, key, child in legacy:
        child["id"] = "watermark_part_"+key
        obj["corners"][key] = child
    obj["enabled"] = any(child.get("enabled", True) and str(child.get("text", "")).strip()
                         for child in obj["corners"].values())
    indices = {index for index, _, _ in legacy}
    migrated = []
    for index, item in enumerate(result):
        if index not in indices:
            migrated.append(item)
        if index == max(indices):
            migrated.append(obj)
    return migrated


def normalize_watermark(obj):
    from .layers import normalize_layers
    corners = obj.get("corners")
    keys = {key for key, *_ in CORNERS}
    if not isinstance(corners, dict) or set(corners) != keys:
        raise ValueError("四角水印内容格式错误。")
    for key in keys:
        child = corners[key]
        if not isinstance(child, dict) or child.get("kind") != "text":
            raise ValueError("水印内部只能使用文字控件。")
        child = copy.deepcopy(child)
        child["id"] = "watermark_part_"+key
        if child.get('word_spacing_version') != 2:
            # Old generated parts inherited the text-box default, not a
            # watermark-specific choice. Other explicit values are retained.
            if child.get('word_spacing') == 20:
                child['word_spacing'] = 0
            child['word_spacing_version'] = 2
        for field, default in POSITION_DEFAULTS.items():
            child.setdefault(field, default)
        if (not isinstance(child["watermark_placement"], str) or child["watermark_placement"] not in {"free", "corner"}
                or not isinstance(child["watermark_inset_unit"], str) or child["watermark_inset_unit"] not in {"pixels", "percent"}):
            raise ValueError("水印贴边方式或内缩单位无效。")
        maximum = 100 if child["watermark_inset_unit"] == "percent" else 16384
        for field in ("watermark_inset_x", "watermark_inset_y"):
            value = child[field]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f"水印内缩距离须为 0–{maximum}，且不能为负数。")
        corners[key] = normalize_layers([child])[0]
        if corners[key]["source"] == "fixed" and not corners[key]["text"].strip():
            corners[key]["enabled"] = False


def expand_watermarks(layers):
    """Render adapter only. Private parts never become top-level UI/state layers."""
    expanded = []
    for layer in layers:
        if layer["kind"] != "watermark":
            expanded.append(layer)
            continue
        for key, label, *_ in CORNERS:
            child = copy.deepcopy(layer["corners"][key])
            child.update(id=layer["id"]+":"+key, name=layer["name"]+" · "+label)
            child["_watermark_corner"] = key
            child["enabled"] = bool(layer["enabled"] and child["enabled"] and (child["source"] != "fixed" or child["text"].strip()))
            child["opacity"] *= layer["opacity"]/100
            expanded.append(child)
    return expanded


def corner_texts(layers):
    obj = next((x for x in migrate_watermarks(layers) if x["kind"] == "watermark"), None)
    return {key: obj["corners"][key].get("text", "") if obj else "" for key, *_ in CORNERS}


def apply_corner_watermarks(layers, texts):
    """Update one composite; empty corners disable without discarding their style."""
    from .layers import normalize_layers
    result = migrate_watermarks(layers)
    obj = next((x for x in result if x["kind"] == "watermark"), None)
    if obj is None:
        if not any(str(texts.get(key, "")).strip() for key, *_ in CORNERS):
            return normalize_layers(result)
        if any(x["id"] == OBJECT_ID for x in result):
            raise ValueError("水印编号被其他图层占用。")
        obj = new_watermark()
        result.append(obj)
    for key, *_ in CORNERS:
        text = str(texts.get(key, "")).strip()
        obj["corners"][key].update(text=text, enabled=bool(text))
    obj["enabled"] = any(child["enabled"] for child in obj["corners"].values())
    return normalize_layers(result)


class WatermarkProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from .component_ui import Combo, TextBoxProperties, ContentSourceEditor
        self.layer, self._loading = None, False
        self.canvas_size = QtCore.QSize(1080, 1920)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.corner = Combo()
        for key, label, *_ in CORNERS:
            self.corner.addItem(label, key)
        self.corner.currentIndexChanged.connect(self._select_corner)
        selector = QtWidgets.QFormLayout()
        selector.addRow("当前调整角落", self.corner)
        layout.addLayout(selector)
        self.source_editor = ContentSourceEditor()
        self.source_editor.allow_copy_pick = False
        self.source_editor.show_fixed_text = False
        self.source_editor.changed.connect(self._source_changed)
        layout.addWidget(self.source_editor)
        form = QtWidgets.QFormLayout()
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.inputs = {}
        for key, label, *_ in CORNERS:
            entry = QtWidgets.QLineEdit()
            entry.setMaxLength(40000)
            entry.setPlaceholderText("留空不显示")
            entry.setClearButtonEnabled(True)
            entry.textChanged.connect(self._content_changed)
            form.addRow(label, entry)
            self.inputs[key] = entry
        layout.addLayout(form)
        position = QtWidgets.QFormLayout()
        position.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        position.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.placement = Combo()
        self.placement.addItem("四角贴边（按可见水印）", "corner")
        self.placement.addItem("自由定位（按文本框）", "free")
        self.placement.currentIndexChanged.connect(self._placement_changed)
        position.addRow("定位方式", self.placement)
        self.inset_unit = Combo()
        self.inset_unit.addItem("像素 px", "pixels")
        self.inset_unit.addItem("百分比 %", "percent")
        self.inset_unit.currentIndexChanged.connect(self._inset_unit_changed)
        position.addRow("内缩单位", self.inset_unit)
        self.insets = {}
        from .layer_ui import Spin
        for key, label in (("watermark_inset_x", "水平内缩"), ("watermark_inset_y", "垂直内缩")):
            spin = Spin()
            spin.valueChanged.connect(lambda _value, field=key: self._position_changed(field))
            position.addRow(label, spin)
            self.insets[key] = spin
        self.canvas_label = QtWidgets.QLabel()
        self.canvas_label.setWordWrap(True)
        self.canvas_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        position.addRow(self.canvas_label)
        from .property_pairs import pair_form_fields
        pair_form_fields(position,'内缩',[('x','X',self.insets['watermark_inset_x']),
            ('y','Y',self.insets['watermark_inset_y'])])
        layout.addLayout(position)
        self.apply_positions_button = QtWidgets.QPushButton("将贴边与内缩设置应用到四角")
        self.apply_positions_button.clicked.connect(self._apply_positions)
        layout.addWidget(self.apply_positions_button)
        hint = QtWidgets.QLabel("0 = 可见水印贴边；底色、描边和阴影也计入边界。")
        hint.setWordWrap(True)
        hint.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(hint)
        self.advanced_positions = QtWidgets.QWidget()
        advanced_form = QtWidgets.QFormLayout(self.advanced_positions)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.positions = {}
        for key, label, minimum in (("x", "横向中心", 0), ("y", "纵向中心", 0),
                                    ("width", "框宽度", 1), ("height", "框高度", 1)):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(minimum, 100)
            spin.setDecimals(2)
            spin.setSuffix(" %")
            spin.valueChanged.connect(lambda _value, field=key: self._position_changed(field))
            self.positions[key] = spin
            advanced_form.addRow(label, spin)
        pair_form_fields(advanced_form,'中心',[('x','X',self.positions['x']),('y','Y',self.positions['y'])])
        pair_form_fields(advanced_form,'尺寸',[('width','宽',self.positions['width']),('height','高',self.positions['height'])])
        from .property_sections import PropertySections
        self.sections=PropertySections(); layout.addWidget(self.sections)
        frame=self.sections.add_section('watermark_frame','文字框尺寸／自由坐标',self.advanced_positions)
        self.advanced_position_toggle=frame.toggle
        self.text_style = TextBoxProperties()
        self.text_style.changed.connect(self._style_changed)
        self.text_style.style_sections.sections['font'].set_expanded(False)
        layout.addWidget(self.text_style)

    def set_layer(self, layer):
        self._loading = True
        self.layer = copy.deepcopy(layer)
        self.setEnabled(bool(layer))
        for key, entry in self.inputs.items():
            entry.setText(layer["corners"][key]["text"] if layer else "")
            entry.setEnabled(not layer or layer["corners"][key]["source"] == "fixed")
            entry.setToolTip("此角落绑定了其他来源；在上方选择这个角落后修改来源。" if layer and layer["corners"][key]["source"] != "fixed" else "固定水印文字；留空不显示。")
        self._loading = False
        self._select_corner()

    def _select_corner(self, *_):
        previous, self._loading = self._loading, True
        child = self.layer["corners"][self.corner.currentData()] if self.layer else None
        self.text_style.set_layer(child)
        self.source_editor.set_layer(child)
        for key, spin in self.positions.items():
            if child:
                spin.setValue(child[key])
        if child:
            self.placement.setCurrentIndex(self.placement.findData(child.get("watermark_placement", "free")))
            self.inset_unit.setCurrentIndex(self.inset_unit.findData(child.get("watermark_inset_unit", "pixels")))
        self._refresh_insets(child)
        self._loading = previous

    def set_canvas_size(self, width, height):
        self.canvas_size = QtCore.QSize(int(width), int(height))
        self._select_corner()

    def _refresh_insets(self, child):
        anchored = bool(child and child.get("watermark_placement", "free") == "corner")
        pixels = self.inset_unit.currentData() == "pixels"
        self.canvas_label.setText(f"画布：{self.canvas_size.width()} × {self.canvas_size.height()} px；内缩按当前单位换算。")
        self.canvas_label.setToolTip("像素按实际导出画布计算；水平百分比按画布宽度、垂直百分比按画布高度。框宽高只控制换行和字号，不决定贴边距离。")
        for key, spin in self.insets.items():
            spin.setEnabled(anchored)
            spin.setRange(0, 16384 if pixels else 100)
            spin.setDecimals(2 if pixels else 4)
            spin.setSingleStep(1 if pixels else .1)
            spin.setSuffix(" px" if pixels else " %")
            if child:
                spin.setValue(child.get(key, 12))
        self.inset_unit.setEnabled(anchored)
        self.positions["x"].setEnabled(not anchored)
        self.positions["y"].setEnabled(not anchored)
        self.apply_positions_button.setEnabled(anchored)
        if child and not anchored:
            self.advanced_position_toggle.setChecked(True)

    def _placement_changed(self, *_):
        if self._loading or not self.layer: return
        self.layer["corners"][self.corner.currentData()]["watermark_placement"] = self.placement.currentData()
        self.advanced_position_toggle.setChecked(self.placement.currentData() == "free")
        self._select_corner()
        self._emit_position()

    def _inset_unit_changed(self, *_):
        if self._loading or not self.layer: return
        child = self.layer["corners"][self.corner.currentData()]
        old, new = child.get("watermark_inset_unit", "pixels"), self.inset_unit.currentData()
        if old == new: return
        for key, extent in (("watermark_inset_x", self.canvas_size.width()), ("watermark_inset_y", self.canvas_size.height())):
            value = child.get(key, 12)
            child[key] = value * extent / 100 if new == "pixels" else value * 100 / extent
        child["watermark_inset_unit"] = new
        self._select_corner()
        self._emit_position()

    def _emit_position(self):
        self.source_editor.set_layer(self.layer["corners"][self.corner.currentData()])
        self.changed.emit({"id": self.layer["id"], "corners": copy.deepcopy(self.layer["corners"])})

    def _apply_positions(self):
        if self._loading or not self.layer: return
        selected = self.layer["corners"][self.corner.currentData()]
        values = {key: selected.get(key, default) for key, default in POSITION_DEFAULTS.items()}
        if values["watermark_placement"] != "corner": return
        for child in self.layer["corners"].values():
            child.update(values)
        self._select_corner()
        self._emit_position()

    def set_pool_state(self, state):
        self.source_editor.set_state(state)

    def _source_changed(self, child):
        if self._loading or not self.layer: return
        key = self.corner.currentData()
        child["enabled"] = bool(child["source"] != "fixed" or child.get("text", "").strip())
        self.layer["corners"][key] = copy.deepcopy(child)
        self.layer["enabled"] = any(item["enabled"] for item in self.layer["corners"].values())
        self._loading = True
        self.inputs[key].setText(child.get("text", ""))
        self.inputs[key].setEnabled(child["source"] == "fixed")
        self._loading = False
        self.changed.emit({"id": self.layer["id"], "corners": copy.deepcopy(self.layer["corners"]), "enabled": self.layer["enabled"]})

    def _content_changed(self, *_):
        if self._loading or not self.layer:
            return
        for key, entry in self.inputs.items():
            if self.layer["corners"][key]["source"] == "fixed":
                self.layer["corners"][key].update(text=entry.text(), enabled=bool(entry.text().strip()))
        self.layer["enabled"] = any(child["enabled"] for child in self.layer["corners"].values())
        self._select_corner()
        self.changed.emit({"id": self.layer["id"], "corners": copy.deepcopy(self.layer["corners"]),
                           "enabled": self.layer["enabled"]})

    def _style_changed(self, values):
        if not self._loading and self.layer:
            self.layer["corners"][self.corner.currentData()].update(values)
            self.source_editor.set_layer(self.layer["corners"][self.corner.currentData()])
            self.changed.emit({"id": self.layer["id"], "corners": copy.deepcopy(self.layer["corners"])})

    def _position_changed(self, field):
        if not self._loading and self.layer:
            controls = self.positions if field in self.positions else self.insets
            self.layer["corners"][self.corner.currentData()][field] = controls[field].value()
            self._emit_position()


def watermark_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    panel = ComponentPropertyPanel(mode=mode)
    from .inspector_layout_contract import require_flat_layout
    require_flat_layout(panel)
    panel.watermark_properties = panel.add_bound('watermark', '', WatermarkProperties(), section=False)
    panel.targets['watermark']=panel.watermark_properties
    panel.targets['source']=panel.watermark_properties.source_editor
    panel.sections.update(panel.watermark_properties.sections.sections)
    panel.sections.update(panel.watermark_properties.text_style.style_sections.sections)
    return panel
