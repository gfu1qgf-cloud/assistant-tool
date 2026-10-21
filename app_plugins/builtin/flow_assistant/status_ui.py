"""Fixed status lines with elision and full hover text; no scroll containers."""
from qt_compat import QtCore, QtGui, QtWidgets


class _StatusText(QtCore.QObject):
    def __init__(self, label, lines):
        super().__init__(label)
        self.lines = lines

    def eventFilter(self, label, event):
        if event.type() == QtCore.QEvent.Type.ToolTip:
            QtWidgets.QToolTip.showText(event.globalPos(), label.text(), label)
            return True
        if event.type() != QtCore.QEvent.Type.Paint:
            return False
        label.setToolTip(label.text())
        painter = QtWidgets.QStylePainter(label)
        option = QtWidgets.QStyleOption()
        option.initFrom(label)
        painter.drawPrimitive(QtWidgets.QStyle.PrimitiveElement.PE_Widget, option)
        lines = label.text().splitlines()
        if len(lines) > self.lines:
            lines = lines[:self.lines - 1] + [' · '.join(lines[self.lines - 1:])]
        rect = label.contentsRect().adjusted(0, 2, 0, -2)
        height = label.fontMetrics().lineSpacing()
        for index, text in enumerate(lines):
            line = QtCore.QRect(rect.x(), rect.y() + index * height, rect.width(), height)
            text = label.fontMetrics().elidedText(text, QtCore.Qt.TextElideMode.ElideRight, line.width())
            painter.drawItemText(line, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                                 label.palette(), label.isEnabled(), text, QtGui.QPalette.ColorRole.WindowText)
        painter.end()
        return True


def status_area(label, lines=2):
    label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
    label.setMinimumWidth(0)
    label.setFixedHeight(label.fontMetrics().lineSpacing() * lines + 4)
    label.setWordWrap(False)
    label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    label._status_text = _StatusText(label, lines)
    label.installEventFilter(label._status_text)
    return label
