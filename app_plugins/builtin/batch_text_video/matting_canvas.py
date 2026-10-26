"""Zoomable original/matte view with normalized keep/remove brush strokes."""
import copy
import math
from qt_compat import QtCore, QtGui, QtWidgets


class MattingCanvas(QtWidgets.QWidget):
    strokeAdded = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.original, self.cutout, self.mask = QtGui.QImage(), QtGui.QImage(), QtGui.QImage()
        self.view, self.mode, self.brush_size = 'composite', 'view', 24
        self.editable = True
        self.zoom, self.offset = 1., QtCore.QPointF()
        self.strokes, self.trace, self.pan = [], None, None
        self.background = QtGui.QColor('#657181')
        self._checker = QtGui.QPixmap(32, 32); self._checker.fill(QtGui.QColor('#e4e8ee'))
        tile = QtGui.QPainter(self._checker)
        tile.fillRect(16, 0, 16, 16, QtGui.QColor('#b5bdc8')); tile.fillRect(0, 16, 16, 16, QtGui.QColor('#b5bdc8'))
        tile.end()
        self.setMinimumSize(300, 240); self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_images(self, original, cutout=None):
        self.original = original
        self.cutout = cutout if cutout is not None else QtGui.QImage()
        self.mask = QtGui.QImage()
        if not self.cutout.isNull():
            from .matting_render import image_array
            alpha = image_array(self.cutout)[:, :, 3].copy()
            self.mask = QtGui.QImage(alpha.tobytes(), alpha.shape[1], alpha.shape[0], alpha.shape[1],
                                     QtGui.QImage.Format.Format_Grayscale8).copy()
        self.update()

    def image_rect(self):
        if self.original.isNull(): return QtCore.QRectF()
        scale = min(max(1, self.width()-16)/self.original.width(), max(1, self.height()-16)/self.original.height())*self.zoom
        w, h = self.original.width()*scale, self.original.height()*scale
        return QtCore.QRectF((self.width()-w)/2+self.offset.x(), (self.height()-h)/2+self.offset.y(), w, h)

    def fit(self):
        self.zoom, self.offset = 1., QtCore.QPointF(); self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#202934'))
        rect = self.image_rect()
        if rect.isEmpty():
            painter.setPen(QtGui.QColor('#d3dce8'))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, '选择素材，然后点击“生成AI遮罩”')
            return
        painter.save(); painter.setClipRect(rect)
        painter.drawTiledPixmap(self.rect(), self._checker)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        if self.view == 'original' or self.cutout.isNull():
            painter.drawImage(rect, self.original)
        elif self.view == 'mask': painter.drawImage(rect, self.mask)
        elif self.view == 'overlay':
            painter.drawImage(rect, self.original)
            painter.setOpacity(.55); painter.drawImage(rect, self.mask); painter.setOpacity(1)
        else: painter.drawImage(rect, self.cutout)
        if self.mode != 'view':
            for stroke in self.strokes + ([self.trace] if self.trace else []):
                points = [QtCore.QPointF(rect.left()+x*rect.width(), rect.top()+y*rect.height()) for x, y in stroke['points']]
                pen = QtGui.QPen(QtGui.QColor('#42d993' if stroke['mode'] == 'keep' else '#f16b67'),
                                 max(1., stroke['radius']*rect.width()*2))
                pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap); pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                if len(points) == 1: painter.drawPoint(points[0])
                else:
                    path = QtGui.QPainterPath(points[0])
                    for point in points[1:]: path.lineTo(point)
                    painter.drawPath(path)
        painter.restore()

    def coordinate(self, position):
        rect = self.image_rect()
        if rect.isEmpty(): return None
        return [min(1., max(0., (position.x()-rect.left())/rect.width())),
                min(1., max(0., (position.y()-rect.top())/rect.height()))]

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self.pan = event.position(); return
        if (self.editable and event.button() == QtCore.Qt.MouseButton.LeftButton and self.mode != 'view'
                and self.image_rect().contains(event.position()) and len(self.strokes) < 500):
            self.trace = dict(mode=self.mode, radius=min(1., self.brush_size/max(1, self.original.width())),
                              points=[self.coordinate(event.position())])
            self.update(); event.accept()

    def mouseMoveEvent(self, event):
        if self.pan is not None:
            self.offset += event.position()-self.pan; self.pan = event.position(); self.update(); return
        if self.trace:
            point = self.coordinate(event.position())
            if len(self.trace['points']) < 5000 and math.dist(point, self.trace['points'][-1]) > .0005:
                self.trace['points'].append(point)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton: self.pan = None
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.trace:
            stroke, self.trace = self.trace, None
            self.strokes.append(copy.deepcopy(stroke)); self.strokeAdded.emit(stroke); self.update()

    def wheelEvent(self, event):
        self.zoom = min(8., max(.2, self.zoom*(1.15 if event.angleDelta().y() > 0 else 1/1.15)))
        self.update(); event.accept()
