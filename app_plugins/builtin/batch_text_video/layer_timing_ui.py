"""Compact always-available times, independent from transition animation switches."""
import copy
from qt_compat import QtCore, QtWidgets
from .component_ui import Spin
from .layer_timing import normalize


class LayerTimingEditor(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.layer, self.loading, self.controls = None, False, {}
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for key, label in (('show_start', '起始时间'), ('show_end', '结束时间')):
            control = Spin()
            control.setRange(-86400, 86400)
            control.setDecimals(3); control.setSingleStep(.1); control.setSuffix(' 秒')
            control.setToolTip('正数：距片头的秒数；负数：从片尾倒算。'+
                               ('0：从片头开始。' if key == 'show_start' else '0：持续到片尾。'))
            control.setAccessibleName(label)
            control.valueChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True); self.error.setStyleSheet('color:#c33442'); self.error.hide()
        form.addRow(self.error)

    def set_layer(self, layer):
        self.loading = True
        try:
            self.layer = normalize(copy.deepcopy(layer)) if layer else None
            for key, control in self.controls.items():
                control.setValue(self.layer[key] if self.layer else 0.)
            self.error.clear(); self.error.hide()
            self.setEnabled(layer is not None)
        finally:
            self.loading = False

    def edit(self, *_):
        if self.loading or self.layer is None:
            return
        values = {key: control.value() for key, control in self.controls.items()}
        try:
            normalize(values)
        except ValueError as error:
            self.error.setText(str(error)); self.error.show()
            return
        self.error.clear(); self.error.hide()
        self.changed.emit(dict(values, id=self.layer['id']))
