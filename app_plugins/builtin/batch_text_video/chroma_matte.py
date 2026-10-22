"""Shared keyed-alpha levels: a small LUT, not a Python per-frame renderer."""
import math
from qt_compat import QtCore, QtGui, QtWidgets

DEFAULTS = {"key_alpha_low": 0.0, "key_alpha_high": 100.0}
from .chroma_key import KEY_COLORS, key_color, despill_filter


def softness_width(layer):
    """UI strength 0..1 controls only a narrow RGB-distance transition band.

    Passing the strength directly as colorkey.blend makes even solid foreground
    colours partially transparent. Keep Qt/native math identical and leave
    similarity, source alpha and user opacity independent.
    """
    return float(layer.get('key_softness', .08)) * .1


def levels(layer):
    low, high = (layer.get(key, default) for key, default in DEFAULTS.items())
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) for value in (low, high)) or not 0 <= low < high <= 100:
        raise ValueError("抠像透明度两端必须满足：0 ≤ 全透明阈值 < 不透明阈值 ≤ 100。")
    if high-low < .000001:
        raise ValueError("抠像透明度两端距离过小；界面可调最小间隔为0.1%。")
    return float(low), float(high)


def remap(alpha, layer):
    """Numpy matte in 0..1; native source alpha and user opacity stay separate."""
    import numpy as np
    low, high = levels(layer)
    return np.clip((alpha-low/100)/((high-low)/100), 0, 1)


def key_filters(layer):
    # Alpha primitives and legacy despill only. Edge despill needs the final
    # source-alpha/trimap result and is composed by add_keyed_source instead.
    mode = layer.get("key_mode", "none")
    if mode == "none":
        return []
    color = "0x" + "".join(f"{channel:02x}" for channel in key_color(layer))
    result = [f"colorkey=color={color}:similarity={layer['key_similarity']:.8f}:blend={softness_width(layer):.8f}"]
    low, high = levels(layer)
    if (low, high) != (0, 100):
        # LUT only changes alpha; RGB remains untouched. Applied before resize
        # to avoid hardening the new antialiased edges created by scaling.
        result.append(f"lutrgb=a='clip((val-{low*2.55:.9f})*{100/(high-low):.9f},0,255)'")
    spill = despill_filter(layer)
    if spill:
        result.append(spill)
    return result


class MatteCurve(QtWidgets.QWidget):
    """Two draggable horizontal endpoints of a clipped alpha transfer line."""
    levelsChanged = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.low, self.high, self.active = 0.0, 100.0, None
        self.setMinimumHeight(96)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("抠像透明度双端调节线")
        self.setToolTip("左下端向右：清除半透明背景。右上端向左：恢复前景不透明。\n"
                        "可拖动端点，或使用下面的数值；方向键微调，Shift加快。")

    def set_levels(self, low, high):
        self.low, self.high = levels({"key_alpha_low": low, "key_alpha_high": high})
        self.update()

    def plot(self):
        return QtCore.QRectF(14, 13, max(1, self.width()-28), 52)

    def endpoints(self):
        box = self.plot()
        return (QtCore.QPointF(box.left()+box.width()*self.low/100, box.bottom()),
                QtCore.QPointF(box.left()+box.width()*self.high/100, box.top()))

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        box = self.plot()
        painter.fillRect(box, QtGui.QColor("#192535"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#344257"), 1))
        for fraction in (0, .25, .5, .75, 1):
            x = box.left()+box.width()*fraction
            painter.drawLine(QtCore.QPointF(x, box.top()), QtCore.QPointF(x, box.bottom()))
        gradient = QtGui.QLinearGradient(box.left(), 0, box.right(), 0)
        gradient.setColorAt(0, QtGui.QColor("#283140"))
        gradient.setColorAt(1, QtGui.QColor("#eff4fa"))
        painter.fillRect(QtCore.QRectF(box.left(), 72, box.width(), 7), gradient)
        left, right = self.endpoints()
        path = QtGui.QPainterPath(QtCore.QPointF(box.left(), box.bottom()))
        path.lineTo(left); path.lineTo(right); path.lineTo(QtCore.QPointF(box.right(), box.top()))
        painter.setPen(QtGui.QPen(QtGui.QColor("#6edac6"), 2))
        painter.drawPath(path)
        for index, point in enumerate((left, right)):
            painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 2 if self.active == index else 1))
            painter.setBrush(QtGui.QColor("#ee9d62" if index == 0 else "#6edac6"))
            painter.drawEllipse(point, 5, 5)
        painter.end()

    def move_endpoint(self, value):
        value = round(min(100, max(0, value)), 1)
        low, high = self.low, self.high
        if self.active == 0:
            low = min(high-.1, value)
        else:
            high = max(low+.1, value)
        if (low, high) != (self.low, self.high):
            self.set_levels(low, high)
            self.levelsChanged.emit(low, high)

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        points = self.endpoints()
        self.active = min(range(2), key=lambda i: (event.position()-points[i]).manhattanLength())
        self.setFocus()
        self.mouseMoveEvent(event)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.active is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            box = self.plot()
            self.move_endpoint((event.position().x()-box.left())/box.width()*100)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.update(); event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in {QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right}:
            if self.active is None: self.active = 1
            step = 5 if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier else .5
            current = self.low if self.active == 0 else self.high
            self.move_endpoint(current + (step if event.key() == QtCore.Qt.Key.Key_Right else -step))
            event.accept()
        else:
            super().keyPressEvent(event)


class MatteLevels(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(3)
        self.curve = MatteCurve()
        root.addWidget(self.curve)
        form = QtWidgets.QFormLayout()
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.controls = {}
        for key, title in (("key_alpha_low", "左端 · 全透明阈值"),
                           ("key_alpha_high", "右端 · 不透明阈值")):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0, 100); spin.setDecimals(1); spin.setSingleStep(1); spin.setSuffix(" %")
            spin.valueChanged.connect(lambda value, key=key: self.edit(key, value))
            self.controls[key] = spin
            form.addRow(title, spin)
        root.addLayout(form)
        self.controls["key_alpha_low"].setToolTip("原扣像不透明度低于此值时变为完全透明。向右提高可以去除残留背景。")
        self.controls["key_alpha_high"].setToolTip("原扣像不透明度高于此值时恢复完全不透明。向左降低可缓解前景发虚；过低可能留下背景。")
        self.reset = QtWidgets.QPushButton("重置透明度两端（0 → 100）")
        self.reset.clicked.connect(self.reset_levels)
        root.addWidget(self.reset)
        self.curve.levelsChanged.connect(self.curve_changed)
        self.set_levels(0, 100)

    def set_levels(self, low, high):
        levels({"key_alpha_low": low, "key_alpha_high": high})
        # External workflows may contain more precision than the UI. Keep the
        # displayed values valid even when both ends round to the same tenth.
        low, high = round(low, 1), round(high, 1)
        if low >= high:
            low, high = (min(99.9, low), min(100, max(.1, high)))
            if low >= high: low = round(high-.1, 1)
        self.curve.set_levels(low, high)
        for key, value in zip(DEFAULTS, (low, high)):
            blocker = QtCore.QSignalBlocker(self.controls[key])
            self.controls[key].setValue(value)
            del blocker

    def curve_changed(self, low, high):
        self.set_levels(low, high)
        self.changed.emit()

    def edit(self, key, value):
        low, high = self.curve.low, self.curve.high
        if key == "key_alpha_low": low = min(value, high-.1)
        else: high = max(value, low+.1)
        self.curve_changed(low, high)

    def reset_levels(self):
        self.curve_changed(0, 100)
