"""One explicit preset action shared by selection, settings and review."""
from qt_compat import QtCore, QtWidgets
from .breath_presets import BREATH_PRESETS, get_preset


class BreathPresetPicker(QtWidgets.QWidget):
    presetApplied = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        self.choice = QtWidgets.QComboBox(self)
        self.choice.addItem("选择气口预设…", "")
        for preset in BREATH_PRESETS:
            self.choice.addItem(preset.name, preset.id)
        self.apply_button = QtWidgets.QPushButton("应用预设", self)
        self.apply_button.setAutoDefault(False)
        self.apply_button.setEnabled(False)
        row.addWidget(self.choice, 1)
        row.addWidget(self.apply_button)
        layout.addLayout(row)
        self.description = QtWidgets.QLabel("选择后点“应用预设”，参数仍可手动调整。", self)
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color:#616B7E;")
        layout.addWidget(self.description)
        self.setToolTip("灵敏度对应现有的人声检测开始阈值；越高越不容易把底噪当作人声。"
                        "预设同时设置最短静音、内部停顿阈值、片头片尾及内部切点的留白，启用内部停顿裁剪。"
                        "快节奏版取参考范围的中间值：170 ms、68%。应用预设后需按当前入口确认分析、检测或保存。")
        self.choice.currentIndexChanged.connect(self._selection_changed)
        self.apply_button.clicked.connect(self._apply)

    def _selection_changed(self):
        key = self.choice.currentData()
        self.apply_button.setEnabled(bool(key))
        self.description.setText("待应用：" + get_preset(key).description if key else "选择后点“应用预设”，参数仍可手动调整。")

    def _apply(self):
        key = self.choice.currentData()
        if key:
            self.presetApplied.emit(key)
            self.description.setText("已应用：" + get_preset(key).description)


def apply_preset_to_controls(owner, preset_id):
    values = get_preset(preset_id).values()
    mode = owner.breath_detection_mode_combo
    mode.setCurrentIndex(mode.findData(values["breath_detection_mode"]))
    for attribute, key in (
        ("silence_detection_checkbox", "silence_detection_enabled"),
        ("voice_detection_checkbox", "voice_detection_enabled"),
        ("compress_pauses_checkbox", "compress_internal_pauses"),
    ):
        getattr(owner, attribute).setChecked(values[key])
    for attribute, key in (
        ("min_silence_spinbox", "min_silence_ms"),
        ("pause_threshold_spinbox", "pause_threshold_ms"),
        ("lead_padding_spinbox", "lead_padding_ms"),
        ("tail_padding_spinbox", "tail_padding_ms"),
        ("pause_keep_before_spinbox", "pause_keep_before_ms"),
        ("pause_keep_after_spinbox", "pause_keep_after_ms"),
        ("vad_threshold_spinbox", "vad_threshold"),
        ("vad_neg_threshold_spinbox", "vad_neg_threshold"),
    ):
        getattr(owner, attribute).setValue(values[key])
    return values
