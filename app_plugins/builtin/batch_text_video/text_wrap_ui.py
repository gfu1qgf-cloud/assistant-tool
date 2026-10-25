"""Object selection for paragraph flow; uses the current layer catalog only."""
import copy
from qt_compat import QtCore, QtWidgets
from .text_wrap import DEFAULTS, SIDES, supported, targetable


class TextWrapProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer, self.layers, self.loading = None, [], False
        self.enable_control = QtWidgets.QCheckBox('启用文字环绕／避让控件')
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        self.targets = QtWidgets.QListWidget()
        self.targets.setAccessibleName('文字要避让的控件，可多选')
        self.targets.setMinimumHeight(94)
        self.targets.setMaximumHeight(158)
        root.addWidget(QtWidgets.QLabel('勾选文字需要躲开的控件（可多选）'))
        root.addWidget(self.targets)
        form = QtWidgets.QFormLayout()
        from .component_ui import Combo
        from .scrolling_text_ui import MotionSpin
        self.side, self.gap = Combo(), MotionSpin()
        for label, value in SIDES:
            self.side.addItem(label, value)
        self.gap.setRange(0, 300)
        self.gap.setDecimals(1)
        self.gap.setSuffix(' px')
        self.gap.setToolTip('文字与所选控件外框的间距，按1080宽画布缩放。')
        form.addRow('环绕方式', self.side)
        form.addRow('与控件间距', self.gap)
        root.addLayout(form)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        root.addWidget(self.info)
        self.enable_control.toggled.connect(self._edit)
        self.targets.itemChanged.connect(self._edit)
        self.side.currentIndexChanged.connect(self._edit)
        self.gap.valueChanged.connect(self._edit)

    def set_layers(self, layers):
        self.layers = layers

    def set_layer(self, layer):
        self.loading = True
        self.layer = copy.deepcopy(layer)
        self.targets.clear()
        chosen = layer.get('wrap_targets', []) if layer else []
        shown = set()
        for item in self.layers:
            if not targetable(item) or not layer or item['id'] == layer['id']:
                continue
            shown.add(item['id'])
            suffix = '（已隐藏，暂不避让）' if not item.get('enabled', True) or not item.get('opacity', 100) else ''
            self._add_item(item['name']+suffix, item['id'], item['id'] in chosen)
        for identifier in chosen:
            if identifier not in shown:
                self._add_item('已移除的控件（可取消勾选） · '+identifier[:12], identifier, True)
        self.targets.setFixedHeight(min(158, max(72, self.targets.count()*24+6)))
        self.enable_control.setChecked(bool(layer and layer.get('wrap_enabled', False)))
        self.enable_control.setEnabled(bool(layer and supported(layer)))
        self.gap.setValue(layer.get('wrap_gap', DEFAULTS['wrap_gap']) if layer else DEFAULTS['wrap_gap'])
        self.side.setCurrentIndex(self.side.findData(layer.get('wrap_side', 'both') if layer else 'both'))
        self.info.setText('适用于普通横排文字；竖排或滚动文字请先关闭对应动画／方向。' if layer and not supported(layer) else
            '按控件外框避让，移动或缩放控件后自动重排；隐藏的控件不占位。图片动画按静态外框计算。'
            '\n字号会在设定范围内自动缩小；放不下时明确提示。预览与导出使用同一排版。')
        self.loading = False

    def _add_item(self, name, identifier, checked):
        item = QtWidgets.QListWidgetItem(name, self.targets)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, identifier)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def _edit(self, *_):
        if self.loading or not self.layer:
            return
        values = {'wrap_enabled': self.enable_control.isChecked(), 'wrap_gap': self.gap.value(),
                  'wrap_side': self.side.currentData(), 'wrap_targets': [self.targets.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                    for i in range(self.targets.count()) if self.targets.item(i).checkState() == QtCore.Qt.CheckState.Checked]}
        self.changed.emit(values)
