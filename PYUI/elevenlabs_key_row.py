"""Compact editable credential row; all text uses a masked key only."""
from qt_compat import QtCore, QtGui, QtWidgets


class KeyRowDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        option.text = ''  # Accessible model text must not show behind the row widget.
        style = option.widget.style() if option.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)


class ElevenLabsKeyRow(QtWidgets.QWidget):
    paidChanged = QtCore.pyqtSignal(bool)
    labelEdited = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(4)
        top = QtWidgets.QHBoxLayout()
        self.key = QtWidgets.QLabel(self)
        self.key.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        top.addWidget(self.key)
        self.label = QtWidgets.QLineEdit(self)
        self.label.setMaxLength(80)
        self.label.setPlaceholderText('备注／账号别名（可选）')
        self.label.setToolTip('只保存到现有程序配置，不发送给ElevenLabs。')
        top.addWidget(self.label, 1)
        self.paid = QtWidgets.QCheckBox('付费备用', self)
        self.paid.setToolTip('勾选后退出普通轮换；仅明确权限／订阅限制时备用。普通额度不足、限流和网络错误不启用。保存后生效。')
        top.addWidget(self.paid)
        layout.addLayout(top)
        self.state = QtWidgets.QLabel(self)
        self.permissions = QtWidgets.QLabel(self)
        for label in (self.state, self.permissions):
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        self.paid.toggled.connect(self.paidChanged.emit)
        self.label.textEdited.connect(self.labelEdited.emit)

    def refresh(self, masked, preferences, status, permissions, quota=''):
        self.key.setText(masked)
        blocker = QtCore.QSignalBlocker(self.paid)
        self.paid.setChecked(preferences.get('paid') is True)
        del blocker
        if not self.label.hasFocus():
            self.label.setText(str(preferences.get('label') or ''))
        role = '付费备用 · 仅权限受限时使用' if preferences.get('paid') is True else '普通 Key · 优先使用'
        self.state.setText(role + '　｜　' + status + (('　｜　' + quota) if quota else ''))
        self.permissions.setText(permissions)

    def set_selected(self, selected):
        palette = QtWidgets.QApplication.palette()
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, palette.color(
            QtGui.QPalette.ColorRole.HighlightedText if selected else QtGui.QPalette.ColorRole.Text))
        self.setPalette(palette)
