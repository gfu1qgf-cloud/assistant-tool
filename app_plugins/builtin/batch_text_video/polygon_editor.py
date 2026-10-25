"""Local, cancellable polygon drawing; commits one ordinary project edit."""
import copy
import uuid
from qt_compat import QtCore, QtGui, QtWidgets
from .polygon_wrap import normalize_polygons, MAX_POLYGONS, MAX_VERTICES
from .undo import EditHistory


class PolygonCanvas(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()
    message = QtCore.pyqtSignal(str)

    def __init__(self, image, polygons=(), parent=None):
        super().__init__(parent)
        self.image, self.polygons = image, copy.deepcopy(list(polygons))
        normalize_polygons(self.polygons)
        self.history = EditHistory()
        self.active = 0 if self.polygons else None
        self.vertex, self.drag, self.hover = None, None, None
        self.drawing, self.draft, self.draft_redo = False, [], []
        self.setMinimumSize(260, 320)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def canvas_rect(self):
        size = QtCore.QSizeF(self.image.size() if not self.image.isNull() else QtCore.QSize(1080, 1920))
        size.scale(QtCore.QSizeF(max(1, self.width()-16), max(1, self.height()-16)), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        return QtCore.QRectF((self.width()-size.width())/2, (self.height()-size.height())/2, size.width(), size.height())

    def screen_point(self, point):
        box = self.canvas_rect()
        return QtCore.QPointF(box.left()+point[0]*box.width()/100, box.top()+point[1]*box.height()/100)

    def percent_point(self, position):
        box = self.canvas_rect()
        return [max(0., min(100., (position.x()-box.left())*100/box.width())),
                max(0., min(100., (position.y()-box.top())*100/box.height()))]

    def _path(self, points):
        path = QtGui.QPainterPath()
        if points:
            path.addPolygon(QtGui.QPolygonF([self.screen_point(point) for point in points])); path.closeSubpath()
        return path

    def _notify(self):
        self.update(); self.changed.emit()

    def begin(self):
        if len(self.polygons) >= MAX_POLYGONS:
            self.message.emit(f'最多{MAX_POLYGONS}个区域。'); return
        if self.drawing and self.draft:
            self.message.emit('请先完成或取消当前轮廓。'); return
        self.drawing, self.draft, self.draft_redo = True, [], []
        self.active, self.vertex, self.hover = None, None, None
        self.message.emit('逐点点击画轮廓；点击起点或Enter闭合，Backspace撤回一点，Esc取消本次绘制。')
        self.setFocus(); self._notify()

    def finish(self):
        if not self.drawing:
            return True
        item = dict(id=uuid.uuid4().hex, enabled=True, points=copy.deepcopy(self.draft))
        try:
            normalize_polygons([item])
        except ValueError as error:
            self.message.emit(str(error)); return False
        before = copy.deepcopy(self.polygons)
        self.polygons.append(item)
        self.active, self.vertex = len(self.polygons)-1, None
        self.drawing, self.draft, self.draft_redo, self.hover = False, [], [], None
        self.history.record(before, self.polygons)
        self.message.emit('轮廓已闭合。拖动顶点微调，拖区域内部整体移动；保存后文字重排。')
        self._notify(); return True

    def cancel_gesture(self):
        if self.drag:
            self.polygons = self.drag['before']; self.drag = None
        elif self.drawing:
            self.drawing, self.draft, self.draft_redo, self.hover = False, [], [], None
            self.active = 0 if self.polygons else None
        self.message.emit('已取消本次操作，已保存的轮廓保留。'); self._notify()

    def _restore(self, polygons):
        self.polygons = polygons
        self.active = min(self.active or 0, len(polygons)-1) if polygons else None
        self.vertex = None
        self._notify(); return True

    def undo(self):
        if self.drag:
            self.cancel_gesture()
        elif self.drawing:
            if self.draft:
                self.draft_redo.append(self.draft.pop()); self._notify()
        else:
            self.history.undo(self._restore)

    def redo(self):
        if self.drawing:
            if self.draft_redo:
                self.draft.append(self.draft_redo.pop()); self._notify()
        else:
            self.history.redo(self._restore)

    def delete_region(self):
        if self.active is None or self.drawing:
            return
        before = copy.deepcopy(self.polygons)
        del self.polygons[self.active]
        self._restore(copy.deepcopy(self.polygons)); self.history.record(before, self.polygons)

    def delete_vertex(self):
        if self.active is None or self.vertex is None:
            self.delete_region(); return
        points = self.polygons[self.active]['points']
        if len(points) <= 3:
            self.message.emit('至少保留3个顶点；删除整个区域请用“删除区域”。'); return
        before = copy.deepcopy(self.polygons)
        del points[self.vertex]
        try:
            normalize_polygons(self.polygons)
        except ValueError as error:
            self.polygons = before; self.message.emit(str(error)); return
        self.vertex = None; self.history.record(before, self.polygons); self._notify()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor('#20242a'))
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self.canvas_rect(), self.image)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            for index, item in enumerate(self.polygons):
                active = index == self.active
                color = QtGui.QColor('#62d8ee' if item.get('enabled', True) else '#9a9a9a')
                fill = QtGui.QColor(color); fill.setAlpha(48 if active else 22)
                painter.setPen(QtGui.QPen(color, 2 if active else 1)); painter.setBrush(fill)
                painter.drawPath(self._path(item['points']))
                if active:
                    for vertex, point in enumerate(item['points']):
                        painter.setBrush(QtGui.QColor('#fff277') if vertex == self.vertex else color)
                        painter.drawEllipse(self.screen_point(point), 5, 5)
            if self.drawing:
                painter.setPen(QtGui.QPen(QtGui.QColor('#fff277'), 2)); painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                points = [self.screen_point(point) for point in self.draft]
                if self.hover is not None:
                    points.append(self.hover)
                if len(points) >= 2:
                    painter.drawPolyline(QtGui.QPolygonF(points))
                painter.setBrush(QtGui.QColor('#fff277'))
                for index, point in enumerate(self.draft):
                    painter.drawEllipse(self.screen_point(point), 7 if index == 0 else 4, 7 if index == 0 else 4)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        position = event.position(); self.setFocus()
        if not self.canvas_rect().contains(position):
            return
        if self.drawing:
            if len(self.draft) >= 3 and (position-self.screen_point(self.draft[0])).manhattanLength() < 14:
                self.finish(); return
            if len(self.draft) >= MAX_VERTICES:
                self.message.emit(f'最多{MAX_VERTICES}个顶点，请闭合轮廓。'); return
            point = self.percent_point(position)
            if self.draft and (position-self.screen_point(self.draft[-1])).manhattanLength() < 3:
                return
            self.draft.append(point); self.draft_redo.clear(); self._notify(); return
        self.vertex = None
        if self.active is not None:
            for index, point in enumerate(self.polygons[self.active]['points']):
                if (position-self.screen_point(point)).manhattanLength() < 13:
                    self.vertex = index; break
        if self.vertex is None:
            self.active = next((i for i in reversed(range(len(self.polygons)))
                                if self._path(self.polygons[i]['points']).contains(position)), None)
        if self.active is not None:
            self.drag = dict(before=copy.deepcopy(self.polygons), origin=self.percent_point(position), vertex=self.vertex)
        self._notify()

    def mouseMoveEvent(self, event):
        position = event.position()
        if self.drawing:
            self.hover = position; self.update()
        elif self.drag:
            point = self.percent_point(position)
            item = self.polygons[self.active]
            original = self.drag['before'][self.active]['points']
            if self.drag['vertex'] is not None:
                item['points'][self.drag['vertex']] = point
            else:
                dx, dy = point[0]-self.drag['origin'][0], point[1]-self.drag['origin'][1]
                dx = min(100-max(p[0] for p in original), max(-min(p[0] for p in original), dx))
                dy = min(100-max(p[1] for p in original), max(-min(p[1] for p in original), dy))
                item['points'] = [[x+dx, y+dy] for x, y in original]
            self.update()
        else:
            super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mouseDoubleClickEvent(event)
        if self.drawing:
            self.finish(); return
        if self.active is None:
            return
        points = self.polygons[self.active]['points']
        position = event.position()
        if any((position-self.screen_point(point)).manhattanLength() < 13 for point in points):
            return
        if len(points) >= MAX_VERTICES:
            self.message.emit(f'最多{MAX_VERTICES}个顶点。'); return
        closest = None
        for index, point in enumerate(points):
            a, b = self.screen_point(point), self.screen_point(points[(index+1) % len(points)])
            delta = b-a; length = delta.x()**2+delta.y()**2
            factor = max(0., min(1., QtCore.QPointF.dotProduct(position-a, delta)/length)) if length else 0.
            projected = a+delta*factor
            distance = (position-projected).manhattanLength()
            if distance <= 10 and (closest is None or distance < closest[0]):
                closest = (distance, index, projected)
        if closest:
            before = copy.deepcopy(self.polygons)
            self.vertex = closest[1]+1
            points.insert(self.vertex, self.percent_point(closest[2]))
            self.drag = None
            self.history.record(before, self.polygons)
            self.message.emit('已加一个顶点，可直接拖动。'); self._notify()

    def mouseReleaseEvent(self, event):
        if self.drag and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            before, self.drag = self.drag['before'], None
            try:
                normalize_polygons(self.polygons)
            except ValueError as error:
                self.polygons = before; self.message.emit(str(error))
            else:
                self.history.record(before, self.polygons)
            self._notify(); return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and self.drawing:
            self.finish()
        elif key == QtCore.Qt.Key.Key_Escape and (self.drawing or self.drag):
            self.cancel_gesture()
        elif key == QtCore.Qt.Key.Key_Backspace and self.drawing:
            self.undo()
        elif key == QtCore.Qt.Key.Key_Delete and not self.drawing:
            self.delete_vertex()
        else:
            return super().keyPressEvent(event)
        event.accept()


class PolygonDialog(QtWidgets.QDialog):
    def __init__(self, image, polygons=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle('文字环绕 · 绘制多边形避让区域')
        self.resize(1050, 820)
        root = QtWidgets.QVBoxLayout(self)
        tools = QtWidgets.QHBoxLayout()
        self.canvas = PolygonCanvas(image, polygons)
        for name, callback in (('新增区域', self.canvas.begin), ('完成绘制', self.canvas.finish),
                ('删除区域', self.canvas.delete_region), ('撤销', self.canvas.undo), ('重做', self.canvas.redo)):
            button = QtWidgets.QPushButton(name); button.setAutoDefault(False)
            button.clicked.connect(callback); tools.addWidget(button)
        root.addLayout(tools)
        row = QtWidgets.QHBoxLayout(); row.addWidget(self.canvas, 1)
        self.regions = QtWidgets.QListWidget(); self.regions.setFixedWidth(145)
        row.addWidget(self.regions); root.addLayout(row, 1)
        self.status = QtWidgets.QLabel('点击区域选择；拖动顶点改形状，双击边线加点，拖区域内部整体移动。Delete删除选中顶点。')
        self.status.setWordWrap(True); root.addWidget(self.status)
        note = QtWidgets.QLabel('绘制：逐点点击 → 点击起点或Enter闭合；Backspace撤回一点，Esc取消本次绘制。'
            '\nCtrl+Z撤销，Ctrl+Shift+Z／Ctrl+Y重做。可以画多个凹多边形，边线不能交叉。保存后文字重排；取消不修改项目。')
        note.setWordWrap(True); root.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText('保存并重排')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); root.addWidget(buttons)
        for keys, callback in ((['Ctrl+Z'], self.canvas.undo), (['Ctrl+Shift+Z', 'Ctrl+Y'], self.canvas.redo)):
            action = QtGui.QAction(self); action.setShortcuts(keys); action.triggered.connect(callback); self.addAction(action)
        self.canvas.message.connect(self.status.setText)
        self.canvas.changed.connect(self._sync)
        self.regions.currentRowChanged.connect(self._select)
        self.regions.itemChanged.connect(self._toggle)
        self._sync()
        if not polygons:
            self.canvas.begin()

    def _sync(self):
        blocker = QtCore.QSignalBlocker(self.regions)
        self.regions.clear()
        for index, item in enumerate(self.canvas.polygons):
            row = QtWidgets.QListWidgetItem(f'区域{index+1} · {len(item["points"])}点', self.regions)
            row.setFlags(row.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(QtCore.Qt.CheckState.Checked if item.get('enabled', True) else QtCore.Qt.CheckState.Unchecked)
        self.regions.setCurrentRow(self.canvas.active if self.canvas.active is not None else -1)
        self.regions.setEnabled(not self.canvas.drawing and self.canvas.drag is None)
        del blocker

    def _select(self, index):
        if index >= 0:
            self.canvas.active, self.canvas.vertex = index, None; self.canvas.update()

    def _toggle(self, item):
        index = self.regions.row(item)
        before = copy.deepcopy(self.canvas.polygons)
        self.canvas.polygons[index]['enabled'] = item.checkState() == QtCore.Qt.CheckState.Checked
        self.canvas.history.record(before, self.canvas.polygons); self.canvas._notify()

    def accept(self):
        if self.canvas.drag:
            self.canvas.cancel_gesture()
        if self.canvas.drawing:
            if self.canvas.draft and not self.canvas.finish():
                return
            if not self.canvas.draft:
                self.canvas.drawing = False
        normalize_polygons(self.canvas.polygons)
        super().accept()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape and (self.canvas.drawing or self.canvas.drag):
            self.canvas.cancel_gesture(); event.accept(); return
        super().keyPressEvent(event)


def edit_polygons(owner, identifier):
    if owner.is_batch_busy():
        return
    owner.return_to_editor()
    owner.flush_editor_history()
    layer = owner.layer_panel.current()
    if not layer or layer['id'] != identifier:
        return
    dialog = PolygonDialog(owner.preview.composition_image(), layer.get('wrap_polygons', []), owner)
    try:
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            if owner.is_batch_busy() or not owner.layer_panel.current() or owner.layer_panel.current()['id'] != identifier:
                owner._log('当前任务状态已改变，多边形修改未应用；请空闲时重新编辑。')
                return
            owner.layer_panel._inspector_edited(identifier, {'wrap_polygons': copy.deepcopy(dialog.canvas.polygons)})
            owner.persist(); owner.update_preview()
    finally:
        dialog.deleteLater()
