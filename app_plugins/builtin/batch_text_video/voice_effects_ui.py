"""Compact narration space presets with optional fine controls and A/B audition."""
from qt_compat import QtCore, QtWidgets
from .voice_effects import DEFAULTS, PRESETS


class VoiceEffectPanel(QtWidgets.QWidget):
    auditionRequested = QtCore.pyqtSignal(bool)
    stopRequested = QtCore.pyqtSignal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(0, 6, 0, 0)
        root.addWidget(QtWidgets.QLabel('<b>人声空间／回声</b>'))
        self.controls = {}
        preset = QtWidgets.QComboBox(); self.controls['voice_fx_preset'] = preset
        preset.setMinimumContentsLength(8); preset.setMinimumWidth(0)
        preset.setSizeAdjustPolicy(preset.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        preset.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        for key, value in PRESETS.items():
            preset.addItem(value['name'], key)
            preset.setItemData(preset.count()-1, value['note'], QtCore.Qt.ItemDataRole.ToolTipRole)
        root.addWidget(preset)
        form = QtWidgets.QFormLayout(); form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        amount = QtWidgets.QDoubleSpinBox(); amount.setRange(0, 150); amount.setDecimals(0); amount.setSuffix(' %'); amount.setSingleStep(5)
        amount.setToolTip('100%为预设建议强度，0%保留原声；原声始终保留，增加的是空间尾音。')
        self.controls['voice_fx_amount'] = amount; form.addRow('效果强度', amount); root.addLayout(form)
        self.note = QtWidgets.QLabel(); self.note.setWordWrap(True)
        self.note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred); root.addWidget(self.note)
        self.details = QtWidgets.QWidget(); details = QtWidgets.QFormLayout(self.details); details.setContentsMargins(0, 0, 0, 0)
        for key, label, low, high, step, suffix in (
            ('voice_fx_decay', '尾音时长', 0, 8, .1, ' 秒'),
            ('voice_fx_predelay', '原声与尾音间隔', -1, 200, 5, ' ms'),
            ('voice_fx_width', '空间立体宽度', 0, 100, 5, ' %'),
        ):
            control = QtWidgets.QSpinBox() if key == 'voice_fx_predelay' else QtWidgets.QDoubleSpinBox()
            if isinstance(control, QtWidgets.QDoubleSpinBox): control.setDecimals(1 if key == 'voice_fx_decay' else 0)
            control.setRange(low, high); control.setSingleStep(step); control.setSuffix(suffix)
            if key != 'voice_fx_width': control.setSpecialValueText('跟随预设')
            self.controls[key] = control; details.addRow(label, control)
        self.controls['voice_fx_predelay'].setToolTip('只延后空间尾音；25–85ms可让原声先说清，再铺开空间。不会移动原声或字幕。')
        reset = QtWidgets.QPushButton('恢复本预设建议值'); reset.setAutoDefault(False); reset.clicked.connect(self.reset_values); details.addRow(reset)
        toggle = QtWidgets.QToolButton(); toggle.setText('精细调节'); toggle.setCheckable(True)
        toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon); toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        toggle.toggled.connect(self.details.setVisible)
        toggle.toggled.connect(lambda opened: toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if opened else QtCore.Qt.ArrowType.RightArrow))
        root.addWidget(toggle); root.addWidget(self.details); self.details.hide()
        row = QtWidgets.QHBoxLayout()
        self.audition = QtWidgets.QPushButton('试听效果'); self.original = QtWidgets.QPushButton('原声对比'); self.stop = QtWidgets.QPushButton('停止')
        for button in (self.audition, self.original, self.stop): button.setAutoDefault(False); row.addWidget(button)
        self.audition.clicked.connect(lambda: self.auditionRequested.emit(True))
        self.original.clicked.connect(lambda: self.auditionRequested.emit(False)); self.stop.clicked.connect(self.stopRequested)
        root.addLayout(row)
        self.position = QtWidgets.QDoubleSpinBox(); self.position.setRange(0, 86400); self.position.setDecimals(1); self.position.setSuffix(' 秒')
        self.position.setToolTip('试听当前条目人声，从这里开始取最多12秒，并补一小段尾音；不修改源文件或轮换进度。')
        seek = QtWidgets.QFormLayout(); seek.addRow('试听起点', self.position); root.addLayout(seek)
        self.status = QtWidgets.QLabel('仅处理人声。成品时长不变，结尾空间尾音柔和淡出。'); self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred); root.addWidget(self.status)
        for key, control in self.controls.items():
            value = settings.get(key, DEFAULTS[key])
            if isinstance(control, QtWidgets.QComboBox): control.setCurrentIndex(max(0, control.findData(value)))
            else: control.setValue(value)
        for control in self.controls.values():
            (control.currentIndexChanged if isinstance(control, QtWidgets.QComboBox) else control.valueChanged).connect(self.update_note)
        preset.activated.connect(self.reset_values)
        self.update_note()

    def reset_values(self, *_):
        for key, value in DEFAULTS.items():
            if key != 'voice_fx_preset': self.controls[key].setValue(value)
        self.update_note()

    def update_note(self, *_):
        preset = self.controls['voice_fx_preset'].currentData()
        enabled = preset != 'none'
        self.controls['voice_fx_amount'].setEnabled(enabled); self.details.setEnabled(enabled)
        self.note.setText(PRESETS[preset]['note'])
