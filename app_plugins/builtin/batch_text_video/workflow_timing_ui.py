"""GUI-only, compact two-ended window editor. Rendering remains headless."""
from qt_compat import QtCore, QtWidgets
from .component_ui import Spin, Combo
from .overlay_timing import DEFAULTS, ENTER_CHOICES, EXIT_CHOICES, normalize_timing


class TimingNodeEditor(QtWidgets.QWidget):
    def __init__(self, params, callback, parent=None, *, mode='node'):
        super().__init__(parent)
        self.callback, self.loading = callback, True
        self.params = {key: params.get(key, value) for key, value in DEFAULTS.items()}
        self.controls = {}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form = self.group(root, "显示范围")
        self.number(form, "start", "开始", 86400, " 秒")
        self.number(form, "end", "结束", 86400, " 秒")
        self.controls["end"].setSpecialValueText("直到视频末尾")
        self.note(form, "开始前隐藏；结束是退场完成时刻。" + (
            "\n连接时间输入端时，端口数值优先。" if mode == 'node' else ''))
        form = self.group(root, "入场与退场")
        self.choice(form, "enter", "入场", ENTER_CHOICES)
        self.number(form, "enter_duration", "用时", 60, " 秒")
        self.choice(form, "exit", "退场", EXIT_CHOICES)
        self.number(form, "exit_duration", "用时", 60, " 秒")
        self.number(form, "distance", "运动幅度", 80, " %")
        self.controls["distance"].setToolTip("滑动距离按画面宽／高计算；缩放幅度按元素原尺寸计算。只移动叠加内容，不移动背景。")
        form = self.group(root, "素材播放")
        self.choice(form, "clock", "时钟", (("auto", "自动（推荐）"),
            ("restart", "出现时从头播放"), ("global", "跟随全片时间")))
        self.note(form, "自动：素材出现时从头播放；字幕保留SRT原时间。\n从头播放：字幕时间相对开始秒；跟随全片：隐藏时也继续。" if mode == 'node' else
                  "GIF／视频默认从出现时播放；图片不受时钟选择影响。\n动画快览显示素材首帧，完整动作请用播放预览。")
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True); self.error.setStyleSheet("color:#c33442")
        root.addWidget(self.error)
        self.loading = False
        self.update_controls()

    def set_values(self, values):
        values = normalize_timing(values)
        self.loading = True
        try:
            for key, control in self.controls.items():
                blocker = QtCore.QSignalBlocker(control)
                if isinstance(control, QtWidgets.QComboBox):
                    control.setCurrentIndex(control.findData(values[key]))
                else:
                    control.setValue(values[key])
                del blocker
            self.params = values
            self.error.clear()
            self.update_controls()
        finally:
            self.loading = False

    def group(self, root, title):
        box = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(7, 7, 7, 7)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        root.addWidget(box)
        return form

    def note(self, form, text):
        label = QtWidgets.QLabel(text); label.setWordWrap(True)
        label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(label)

    def number(self, form, key, title, maximum, suffix):
        control = Spin()
        control.setRange(0, maximum); control.setDecimals(2)
        control.setSingleStep(1 if suffix == " %" else .1); control.setSuffix(suffix)
        control.setValue(self.params[key]); control.valueChanged.connect(self.edit)
        self.controls[key] = control
        form.addRow(title, control)

    def choice(self, form, key, title, choices):
        control = Combo()
        for value, text in choices: control.addItem(text, value)
        control.setCurrentIndex(max(0, control.findData(self.params[key])))
        control.currentIndexChanged.connect(self.edit)
        self.controls[key] = control
        form.addRow(title, control)

    def update_controls(self):
        for phase in ("enter", "exit"):
            self.controls[phase + "_duration"].setEnabled(self.controls[phase].currentData() != "cut")
        self.controls["distance"].setEnabled(any(self.controls[key].currentData() not in {"cut", "fade"}
                                                 for key in ("enter", "exit")))

    def edit(self, *_):
        if self.loading: return
        values = {key: control.currentData() if isinstance(control, QtWidgets.QComboBox) else control.value()
                  for key, control in self.controls.items()}
        self.update_controls()
        try:
            normalize_timing(values)
            self.error.clear()
        except Exception as error:
            self.error.setText(str(error))
        try:
            # Preserve an invalid draft too: the user may still be changing its
            # other end. Preflight prevents execution until it is consistent.
            self.callback(values)
        except Exception as error:
            self.error.setText("设置未应用：" + str(error))


def timing_inspector(params, changed, parent=None):
    return TimingNodeEditor(params, changed, parent)
