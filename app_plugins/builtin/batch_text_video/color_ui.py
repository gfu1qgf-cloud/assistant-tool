"""Visual, alpha-aware color/list editor using the existing caption color schema."""
from qt_compat import QtCore, QtGui, QtWidgets
from .caption_styles import colors


class EffectColorEditor(QtWidgets.QWidget):
    def __init__(self, value, parent=None):
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.swatches = QtWidgets.QVBoxLayout()
        root.addLayout(self.swatches)
        tools = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton('＋添加颜色')
        self.add_button.setAutoDefault(False)
        self.add_button.setToolTip('添加渐变色或多层描边色；最多8种，顺序与画面效果一致。')
        self.add_button.clicked.connect(self.add_color)
        tools.addWidget(self.add_button)
        self.advanced = QtWidgets.QToolButton()
        self.advanced.setText('色值（可选）')
        self.advanced.setCheckable(True)
        tools.addWidget(self.advanced)
        tools.addStretch()
        root.addLayout(tools)
        self.field = QtWidgets.QLineEdit(','.join(map(str, value)) if isinstance(value, list) else str(value))
        self.field.setPlaceholderText('#RRGGBB；多个颜色用逗号分隔')
        root.addWidget(self.field)
        self.field.hide()
        self.advanced.toggled.connect(self.field.setVisible)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color:#c33442;')
        root.addWidget(self.error)
        self.buttons = []
        self.field.textChanged.connect(self.refresh)
        self.refresh()

    def text(self):
        return self.field.text()

    def setText(self, value):
        self.field.setText(value)

    def refresh(self):
        while self.swatches.count():
            widget = self.swatches.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self.buttons = []
        try:
            values = colors(self.text())
        except ValueError:
            self.advanced.setChecked(True)
            self.error.setText('颜色无效，请在上方色值框修正，或取消本次修改。')
            self.error.show()
            self.add_button.setEnabled(False)
            return
        self.error.hide()
        self.add_button.setEnabled(len(values) < 8)
        for index, value in enumerate(values):
            row = QtWidgets.QWidget(self)
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            button = QtWidgets.QPushButton('选择颜色' if len(values) == 1 else f'颜色 {index + 1}')
            button.setAutoDefault(False)
            button.setToolTip('点击打开取色器；当前颜色：' + value)
            pixmap = QtGui.QPixmap(36, 20)
            pixmap.fill(QtGui.QColor(value))
            button.setIcon(QtGui.QIcon(pixmap))
            button.setIconSize(QtCore.QSize(36, 20))
            button.clicked.connect(lambda _checked=False, index=index: self.choose_color(index))
            layout.addWidget(button, 1)
            remove = QtWidgets.QToolButton()
            remove.setText('×')
            remove.setToolTip(f'移除第 {index + 1} 个颜色')
            remove.setEnabled(len(values) > 1)
            remove.clicked.connect(lambda _checked=False, index=index: self.remove_color(index))
            layout.addWidget(remove)
            self.swatches.addWidget(row)
            self.buttons.append(button)

    def pick(self, initial):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(initial), self, '选择字幕效果颜色',
            QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if not color.isValid():
            return None
        return color.name(QtGui.QColor.NameFormat.HexArgb if color.alpha() != 255 else QtGui.QColor.NameFormat.HexRgb)

    def choose_color(self, index):
        values = colors(self.text())
        value = self.pick(values[index])
        if value is not None:
            values[index] = value
            self.setText(','.join(values))

    def add_color(self):
        values = colors(self.text())
        if len(values) >= 8:
            return
        value = self.pick(values[-1])
        if value is not None:
            self.setText(','.join([*values, value]))

    def remove_color(self, index):
        values = colors(self.text())
        if len(values) > 1:
            del values[index]
            self.setText(','.join(values))
