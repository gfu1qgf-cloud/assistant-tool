"""Automatic, click-through Begin/End backgrounds, inspired by Houdini blobs."""
from qt_compat import QtCore, QtGui, QtWidgets


class LoopRegionItem(QtWidgets.QGraphicsPathItem):
    def __init__(self, scene, region):
        super().__init__()
        self.region = region
        self.label_rect = QtCore.QRectF()
        self.setZValue(-20 + min(region.depth, 16))
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        scene.addItem(self)

    def shape(self):
        # A background must not intercept blank-canvas menus, selection or pan.
        return QtGui.QPainterPath()

    def boundingRect(self):
        # QGraphicsPathItem's default bounds call virtual shape(); ours is empty
        # intentionally for hit testing, so painting needs independent bounds.
        return self.path().boundingRect().adjusted(-2, -2, 2, 2)

    def refresh(self, nodes, edges, maximum_depth):
        padding = 22 + 6 * (maximum_depth - self.region.depth)
        path = QtGui.QPainterPath()
        for node_id in self.region.members:
            item = nodes.get(node_id)
            if item is None:
                continue
            rect = item.sceneBoundingRect().adjusted(-padding, -padding, padding, padding)
            if item.data['kind'] == 'core.loop_begin':
                rect.adjust(0, -30, 0, 0)
            part = QtGui.QPainterPath()
            part.addRoundedRect(rect, 22, 22)
            path = path.united(part)
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(padding * 2)
        stroker.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        for edge in edges:
            if edge.edge[0] in self.region.members and edge.edge[2] in self.region.members:
                path = path.united(stroker.createStroke(edge.path()))
        begin = nodes.get(self.region.begin)
        if begin is not None:
            rect = begin.sceneBoundingRect()
            self.label_rect = QtCore.QRectF(rect.left() - padding + 12,
                                           rect.top() - padding - 26, 310, 24)
            label = QtGui.QPainterPath()
            label.addRoundedRect(self.label_rect.adjusted(-8, -4, 8, 4), 9, 9)
            path = path.united(label)
        self.setPath(path)

    def paint(self, painter, _option, _widget=None):
        color = QtGui.QColor(self.region.color)
        fill = QtGui.QColor(color)
        fill.setAlpha(26)
        color.setAlpha(155)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(fill)
        painter.setPen(QtGui.QPen(color, 1.5))
        painter.drawPath(self.path())
        painter.setPen(QtGui.QColor(self.region.color))
        font = painter.font()
        font.setPointSizeF(9)
        font.setBold(True)
        painter.setFont(font)
        caption = painter.fontMetrics().elidedText(self.region.title,
            QtCore.Qt.TextElideMode.ElideRight, int(self.label_rect.width()))
        painter.drawText(self.label_rect, QtCore.Qt.AlignmentFlag.AlignVCenter, caption)
