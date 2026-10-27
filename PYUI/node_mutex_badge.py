"""Small vector padlock: no emoji font or disabled parameter controls."""
from qt_compat import QtCore, QtGui, QtWidgets


class NodeMutexBadge(QtWidgets.QGraphicsObject):
    def __init__(self, name, parent):
        super().__init__(parent)
        self.name = name
        self.setPos(parent.WIDTH - 29, 8)
        self.setZValue(5)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setToolTip('执行互斥锁：' + name + '\n同一程序内同名节点互斥；同线程可重入。'
                        '\n仅保护本节点执行，不锁定参数，也不代表整个渲染过程已加锁。')

    def boundingRect(self):
        return QtCore.QRectF(0, 0, 20, 24)

    def paint(self, painter, _option, _widget=None):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor('#f2cf78'), 1.8))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawArc(QtCore.QRectF(5, 3, 10, 12), 0, 180 * 16)
        painter.setBrush(QtGui.QColor('#f2cf78'))
        painter.drawRoundedRect(QtCore.QRectF(3, 10, 14, 11), 2, 2)
        painter.setPen(QtGui.QPen(QtGui.QColor('#273446'), 2))
        painter.drawLine(QtCore.QPointF(10, 14), QtCore.QPointF(10, 18))
