"""Numeric RGBA, native picker and swatch; connected ports override these defaults."""
from qt_compat import QtCore, QtGui, QtWidgets
from model.node_workflow.value_nodes import rgb


class RgbEditor(QtWidgets.QWidget):
    def __init__(self, params, callback, parent=None):
        super().__init__(parent)
        self.callback = callback
        root = QtWidgets.QFormLayout(self); self.channels = {}
        self.unit = QtWidgets.QComboBox(); self.unit.addItem('0–1', 'normalized'); self.unit.addItem('0–255', 'byte')
        self.unit.setCurrentIndex(max(0, self.unit.findData(params['unit']))); root.addRow('单位', self.unit)
        for key, title in (('red', 'R'), ('green', 'G'), ('blue', 'B'), ('alpha', 'Alpha')):
            control = QtWidgets.QDoubleSpinBox(); control.setRange(-1000, 1000); control.setDecimals(5); control.setValue(params[key])
            root.addRow(title, control); self.channels[key] = control
        self.range = QtWidgets.QComboBox(); self.range.addItem('超范围报错', 'error'); self.range.addItem('夹紧', 'clamp')
        self.range.setCurrentIndex(max(0, self.range.findData(params['range']))); root.addRow(self.range)
        self.swatch = QtWidgets.QPushButton('选择颜色…'); root.addRow(self.swatch)
        self.error = QtWidgets.QLabel(); self.error.setWordWrap(True); root.addRow(self.error)
        self.old_unit = self.unit.currentData()
        self.unit.currentIndexChanged.connect(self.change_unit)
        self.range.currentIndexChanged.connect(self.apply)
        for control in self.channels.values(): control.valueChanged.connect(self.apply)
        self.swatch.clicked.connect(self.pick); self.update_swatch()

    def values(self):
        return {**{key: c.value() for key, c in self.channels.items()}, 'unit': self.unit.currentData(), 'range': self.range.currentData()}

    def update_swatch(self):
        try:
            value = rgb({}, self.values(), None)['color']
            self.swatch.setStyleSheet('background:'+value.hex_rgb+'; color:'+('#101018' if value.red+value.green+value.blue > 1.5 else '#ffffff'))
            self.swatch.setText(value.hex_rgb + f' · Alpha {value.alpha:.2f} · 选择…'); self.error.clear()
        except ValueError as error:
            self.error.setText(str(error))

    def apply(self, *_):
        self.callback(self.values()); self.update_swatch()

    def change_unit(self, *_):
        factor = 255 if self.unit.currentData() == 'byte' else 1/255
        if self.unit.currentData() == self.old_unit: return
        for control in self.channels.values():
            with QtCore.QSignalBlocker(control): control.setValue(control.value()*factor)
        self.old_unit = self.unit.currentData(); self.apply()

    def pick(self):
        try: value = rgb({}, self.values(), None)['color']
        except ValueError: return
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(value.red, value.green, value.blue, value.alpha), self,
            'sRGB颜色', QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            scale = 255 if self.unit.currentData() == 'byte' else 1
            for key, value in zip(self.channels, color.getRgbF()):
                with QtCore.QSignalBlocker(self.channels[key]): self.channels[key].setValue(value*scale)
            self.apply()
