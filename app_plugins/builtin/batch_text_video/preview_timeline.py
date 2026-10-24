"""An editor-only animation clock: no encoding, persistence or pool rotation."""
import math

from qt_compat import QtCore, QtWidgets
from .adaptation import target_duration


def editor_duration(settings, source_duration=None, voice_duration=None, *, has_voice=False):
    if has_voice:
        value = voice_duration
    else:
        value = target_duration(source_duration, dict(settings))
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


class TimeSlider(QtWidgets.QSlider):
    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            option = QtWidgets.QStyleOptionSlider()
            self.initStyleOption(option)
            style = self.style()
            groove = style.subControlRect(style.ComplexControl.CC_Slider, option, style.SubControl.SC_SliderGroove, self)
            handle = style.subControlRect(style.ComplexControl.CC_Slider, option, style.SubControl.SC_SliderHandle, self)
            point = event.position().toPoint()
            if not handle.contains(point):
                value = style.sliderValueFromPosition(self.minimum(), self.maximum(),
                    point.x()-groove.left()-handle.width()//2, max(1, groove.width()-handle.width()), option.upsideDown)
                self.setValue(value)
        super().mousePressEvent(event)


class AnimationTimeline(QtWidgets.QWidget):
    timeChanged = QtCore.pyqtSignal()
    STEPS = 10000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = None
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(QtWidgets.QLabel('动画快览 · 不渲染整片'))
        heading.addStretch(1)
        self.time_label = QtWidgets.QLabel()
        heading.addWidget(self.time_label)
        root.addLayout(heading)
        row = QtWidgets.QHBoxLayout()
        self.start_button, self.end_button = QtWidgets.QToolButton(), QtWidgets.QToolButton()
        self.middle_button = QtWidgets.QToolButton()
        self.middle_button.setText('中间')
        self.middle_button.setToolTip('查看视频中间时刻；便于检查入场后的完整图片')
        self.start_button.setText('开头'); self.end_button.setText('结尾')
        self.start_button.setToolTip('直接查看动画起点')
        self.end_button.setToolTip('直接查看动画终点，包括提前结束后的停留位置')
        self.slider = TimeSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.STEPS)
        self.slider.setPageStep(self.STEPS//10)
        self.slider.setToolTip('拖动查看该时刻的滚动文字、图片入场／退场与持续动画；使用完整成品时长，不生成视频或推进资源池。图片背景可快览缩放；视频与其他视觉素材仍是首帧。节点使用已保存的工作流；AI、脚本和模型计算不会自动执行。')
        row.addWidget(self.start_button); row.addWidget(self.slider, 1); row.addWidget(self.middle_button); row.addWidget(self.end_button)
        root.addLayout(row)
        self.start_button.clicked.connect(lambda: self.jump(0))
        self.middle_button.clicked.connect(lambda: self.jump(self.STEPS//2))
        self.end_button.clicked.connect(lambda: self.jump(self.STEPS))
        self.slider.valueChanged.connect(self._changed)
        self.set_duration(None)

    def set_duration(self, value):
        self.duration = float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
        for control in (self.slider, self.start_button, self.middle_button, self.end_button):
            control.setEnabled(self.duration is not None)
        self._label()

    def snapshot(self):
        if self.duration is None:
            return None
        return {'duration': self.duration, 'time': self.duration * self.slider.value() / self.STEPS}

    def _label(self):
        value = self.snapshot()
        self.time_label.setText(f"{value['time']:.2f} / {value['duration']:.2f} 秒" if value else '等待读取成品时长')

    def _changed(self, _value):
        self._label()
        if self.duration is not None:
            self.timeChanged.emit()

    def jump(self, position):
        if self.slider.value() == position and self.duration is not None:
            self.timeChanged.emit()  # Recheck a newly saved node workflow even if already at the end.
        else:
            self.slider.setValue(position)
