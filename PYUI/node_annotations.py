"""Selectable visual notes/backdrops, separate from executable nodes."""
from qt_compat import QtCore, QtGui, QtWidgets


class AnnotationItem(QtWidgets.QGraphicsObject):
    def __init__(self, scene, data):
        super().__init__()
        self.data = data
        self.setFlags(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setZValue(-30 if data['kind'] == 'group' else -10)
        scene.addItem(self); self.setPos(*data['position'])

    def boundingRect(self):
        width, height = (240, 44) if self.data.get('collapsed') else self.data['size']
        return QtCore.QRectF(0, 0, width, height)

    def shape(self):
        path = QtGui.QPainterPath()
        if self.data['kind'] == 'group' and not self.data.get('collapsed'):
            # Only the frame/header hit-test, never block the interior wires.
            outer = self.boundingRect(); inner = outer.adjusted(10, 40, -10, -10)
            path.addRect(outer); hole = QtGui.QPainterPath(); hole.addRect(inner)
            return path.subtracted(hole)
        path.addRect(self.boundingRect()); return path

    def paint(self, painter, _option, _widget=None):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        color = QtGui.QColor(self.data['color'])
        painter.setPen(QtGui.QPen(color.lighter(145) if self.isSelected() else color, 3 if self.isSelected() else 1.5))
        fill = QtGui.QColor(color); fill.setAlpha(55 if self.data['kind'] == 'group' else 225)
        painter.setBrush(fill); painter.drawRoundedRect(self.boundingRect(), 8, 8)
        painter.setPen(QtGui.QColor('#f4f6ff'))
        title = ('▶ ' if self.data.get('collapsed') else '▼ ' if self.data['kind'] == 'group' else '') + self.data['title']
        painter.drawText(self.boundingRect().adjusted(12, 4, -12, -4), QtCore.Qt.AlignmentFlag.AlignTop, title)
        if not self.data.get('collapsed'):
            painter.drawText(self.boundingRect().adjusted(12, 40, -12, -12), QtCore.Qt.TextFlag.TextWordWrap, self.data.get('text', ''))
            rect = self.boundingRect(); painter.drawLine(rect.bottomRight()-QtCore.QPointF(15, 0), rect.bottomRight()-QtCore.QPointF(0, 15))

    def mousePressEvent(self, event):
        self.origin = QtCore.QPointF(self.pos())
        self.original_size = list(self.data['size'])
        self.members = {key: QtCore.QPointF(self.scene().nodes[key].pos()) for key in self.data.get('members', []) if key in self.scene().nodes}
        self.resizing = not self.data.get('collapsed') and (event.pos() - self.boundingRect().bottomRight()).manhattanLength() < 28
        if self.resizing:
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.resizing:
            self.prepareGeometryChange()
            self.data['size'] = [max(120, event.pos().x()), max(80, event.pos().y())]
            self.update(); event.accept(); return
        super().mouseMoveEvent(event)
        if self.data.get('move_members'):
            delta = self.pos() - self.origin
            for key, point in self.members.items():
                self.scene().nodes[key].setPos(point + delta)

    def mouseReleaseEvent(self, event):
        if not self.resizing:
            super().mouseReleaseEvent(event)
        changed = self.data['size'] != self.original_size or self.pos() != self.origin
        self.data['position'] = [self.pos().x(), self.pos().y()]
        changed_nodes = set(self.members) if self.data.get('move_members') else set()
        changed_nodes.update(item.data['id'] for item in self.scene().selectedItems() if hasattr(item, 'data')
                             and isinstance(item.data, dict) and item.data.get('id') in self.scene().nodes)
        for key in changed_nodes:
                node = self.scene().nodes[key]
                node.data['position'] = [node.pos().x(), node.pos().y()]
        if changed:
            self.scene().graphChanged.emit()

    def mouseDoubleClickEvent(self, event):
        view = self.scene().views()[0] if self.scene().views() else None
        text, ok = QtWidgets.QInputDialog.getMultiLineText(view, '编辑注释', '内容', self.data.get('text', ''))
        if ok and len(text) <= 8000:
            self.data['text'] = text; self.update(); self.scene().graphChanged.emit()
        event.accept()

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu()
        rename = menu.addAction('重命名…'); color = menu.addAction('颜色…')
        fit = menu.addAction('贴合分组成员') if self.data['kind'] == 'group' else None
        collapse = menu.addAction('展开' if self.data.get('collapsed') else '收起') if self.data['kind'] == 'group' else None
        move = menu.addAction('拖动时整组移动') if self.data['kind'] == 'group' else None
        if move:
            move.setCheckable(True); move.setChecked(self.data.get('move_members', False))
        remove = menu.addAction('删除注释（不删除节点）')
        action = menu.exec(event.screenPos()); scene = self.scene(); view = scene.views()[0] if scene.views() else None
        if action == rename:
            title, ok = QtWidgets.QInputDialog.getText(view, '注释名称', '名称', text=self.data['title'])
            if not ok or not title.strip() or len(title) > 120: return
            self.data['title'] = title.strip()
        elif action == color:
            chosen = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.data['color']), view, '注释颜色')
            if not chosen.isValid(): return
            self.data['color'] = chosen.name()
        elif collapse is not None and action == collapse:
            self.prepareGeometryChange(); self.data['collapsed'] = not self.data.get('collapsed'); scene.refresh_annotations()
        elif move is not None and action == move:
            self.data['move_members'] = move.isChecked()
        elif fit is not None and action == fit:
            members = [scene.nodes[key] for key in self.data.get('members', []) if key in scene.nodes]
            if not members: return
            rect = members[0].sceneBoundingRect()
            for member in members[1:]: rect = rect.united(member.sceneBoundingRect())
            rect = rect.adjusted(-30, -55, 30, 30)
            self.prepareGeometryChange(); self.setPos(rect.topLeft())
            self.data['position'] = [rect.x(), rect.y()]; self.data['size'] = [rect.width(), rect.height()]
        elif action == remove:
            scene.graph['annotations'].remove(self.data); scene.schedule_refresh(); return
        else:
            return
        self.update(); scene.graphChanged.emit()
