"""Shared, ID-bound position editor; geometry remains in normalized percentages."""
import copy
from qt_compat import QtCore, QtWidgets
from .geometry_anchors import anchor, center, distance, distance_range
from .layers import MAX_VISUAL_SIZE_PERCENT, normalize_layers

GEOMETRY_KEYS = ('x', 'y', 'width', 'height')


class GeometryEditor(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)
    unitChanged = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str)

    def __init__(self, unit='percent', parent=None):
        super().__init__(parent)
        self.layer, self.loading = None, False
        self.canvas_size = QtCore.QSize(1080, 1920)
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(3, 3, 3, 3)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.units = QtWidgets.QComboBox()
        self.units.addItem('百分比 %', 'percent'); self.units.addItem('像素 px', 'pixels')
        self.units.setCurrentIndex(max(0, self.units.findData(unit)))
        self.units.currentIndexChanged.connect(self._unit_changed)
        form.addRow('位置单位', self.units)
        self.canvas_label = QtWidgets.QLabel()
        self.canvas_label.setWordWrap(True)
        self.canvas_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(self.canvas_label)
        self.anchor_controls, self.properties, self.geometry_labels = {}, {}, {}
        for axis, label, choices in (
                ('x', '横向基准', (('框中心坐标', 'center'), ('距画面左边', 'left'), ('距画面右边', 'right'))),
                ('y', '纵向基准', (('框中心坐标', 'center'), ('距画面上边', 'top'), ('距画面下边', 'bottom')))):
            combo = QtWidgets.QComboBox()
            for text, value in choices: combo.addItem(text, value)
            combo.currentIndexChanged.connect(self._anchor_changed)
            combo.setToolTip('切换基准不移动控件；边距0即贴边，负数表示部分超出画面。')
            self.anchor_controls[axis] = combo
            form.addRow(label, combo)
        for key, title in (('x', '横向中心'), ('y', '纵向中心'), ('width', '框宽度'), ('height', '框高度')):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setKeyboardTracking(False)
            spin.setMinimumWidth(0)
            spin.valueChanged.connect(lambda _value, key=key: self._geometry_changed(key))
            self.properties[key] = spin
            label = QtWidgets.QLabel(title); self.geometry_labels[key] = label
            form.addRow(label, spin)
        self.snap_button = QtWidgets.QToolButton()
        self.snap_button.setText('一键贴边／居中')
        self.snap_button.setPopupMode(self.snap_button.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.snap_button)
        for text, horizontal, vertical in (('贴左边', 'left', None), ('贴右边', 'right', None),
                ('贴上边', None, 'top'), ('贴下边', None, 'bottom'), ('贴左上角', 'left', 'top'),
                ('贴右上角', 'right', 'top'), ('贴左下角', 'left', 'bottom'), ('贴右下角', 'right', 'bottom'),
                ('水平居中', 'center', None), ('垂直居中', None, 'center'), ('画布居中', 'center', 'center')):
            menu.addAction(text, lambda _checked=False, h=horizontal, v=vertical: self.snap_to_edges(h, v))
        self.snap_button.setMenu(menu); form.addRow(self.snap_button)

    def bind(self, layer, canvas_size=None, unit=None):
        self.layer = copy.deepcopy(layer) if layer else None
        if canvas_size is not None: self.canvas_size = QtCore.QSize(canvas_size)
        self.loading = True
        if unit is not None: self.units.setCurrentIndex(max(0, self.units.findData(unit)))
        self.setEnabled(bool(layer))
        self.canvas_label.setText(f'画布 {self.canvas_size.width()} × {self.canvas_size.height()} px')
        if layer:
            pixels = self.units.currentData() == 'pixels'
            for axis, combo in self.anchor_controls.items():
                combo.setCurrentIndex(combo.findData(anchor(layer, axis)))
                self.geometry_labels[axis].setText({'left':'距左边', 'right':'距右边', 'top':'距上边',
                    'bottom':'距下边', 'center':'横向中心' if axis == 'x' else '纵向中心'}[anchor(layer, axis)])
            for key, spin in self.properties.items():
                extent = self.canvas_size.width() if key in {'x','width'} else self.canvas_size.height()
                factor = extent / 100 if pixels else 1
                maximum = MAX_VISUAL_SIZE_PERCENT if layer['kind'] == 'image' else 100
                low, high = distance_range(layer, key) if key in {'x','y'} else (1, maximum)
                spin.setDecimals(2 if pixels else 3)
                spin.setRange(low * factor, high * factor)
                spin.setSuffix(' px' if pixels else ' %')
                spin.setSingleStep(1 if pixels else .1)
                value = distance(layer, key) if key in {'x','y'} else layer[key]
                spin.setValue(value * factor)
                spin.setToolTip(f'实际值：{value*extent/100:.2f} px；{value:.3f}%' +
                    ('；允许超出画面，超出部分裁切。' if maximum > 100 else ''))
        self.loading = False

    def _unit_changed(self):
        if not self.loading:
            unit = self.units.currentData()
            self.bind(self.layer)
            self.unitChanged.emit(unit)

    def _emit(self, candidate):
        try:
            normalized = normalize_layers([candidate])[0]
        except ValueError as error:
            self.bind(self.layer)
            self.message.emit('位置调整未应用：'+str(error))
            return
        values = {key: normalized[key] for key in (*GEOMETRY_KEYS, 'position_anchor_x', 'position_anchor_y') if key in normalized}
        self.layer.update(values)
        self.bind(self.layer)
        self.changed.emit(values)

    def _geometry_changed(self, key):
        if self.loading or not self.layer: return
        candidate = copy.deepcopy(self.layer)
        extent = self.canvas_size.width() if key in {'x','width'} else self.canvas_size.height()
        value = self.properties[key].value()
        if self.units.currentData() == 'pixels': value = value * 100 / extent
        if key in {'x','y'}:
            candidate[key] = center(candidate, key, value)
        else:
            axis = 'x' if key == 'width' else 'y'
            candidate[key] = value
            if anchor(candidate, axis) != 'center': candidate[axis] = center(candidate, axis, distance(self.layer, axis))
        self._emit(candidate)

    def _anchor_changed(self, *_):
        if not self.loading and self.layer:
            candidate = copy.deepcopy(self.layer)
            candidate.update({f'position_anchor_{axis}': combo.currentData() for axis, combo in self.anchor_controls.items()})
            self._emit(candidate)

    def snap_to_edges(self, horizontal=None, vertical=None):
        if not self.layer: return
        candidate = copy.deepcopy(self.layer)
        for axis, side in (('x', horizontal), ('y', vertical)):
            if side is not None:
                candidate['position_anchor_' + axis] = side
                candidate[axis] = 50 if side == 'center' else center(candidate, axis, 0)
        self._emit(candidate)
