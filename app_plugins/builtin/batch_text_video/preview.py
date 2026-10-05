"""Editable preview surface. Handles and box guides are never exported."""
import copy
from qt_compat import QtCore,QtGui,QtWidgets

GEOMETRY_KEYS = ("x","y","width","height")
HANDLES = ((-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1))


def event_position(event):
    return event.position() if hasattr(event,"position") else QtCore.QPointF(event.pos())


def dragged_geometry(original,dx,dy,handle=None):
    """Deltas use percent of the full canvas, not the containing widget."""
    values = {key:float(original[key]) for key in GEOMETRY_KEYS}
    if handle is None:
        values["x"] = max(values["width"]/2,min(100-values["width"]/2,values["x"]+dx))
        values["y"] = max(values["height"]/2,min(100-values["height"]/2,values["y"]+dy))
        return values
    left,right = max(0,values["x"]-values["width"]/2),min(100,values["x"]+values["width"]/2)
    top,bottom = max(0,values["y"]-values["height"]/2),min(100,values["y"]+values["height"]/2)
    # Existing off-canvas boxes are brought into view when resized.
    if right-left < 1:
        left = max(0,min(99,left))
        right = left+1
    if bottom-top < 1:
        top = max(0,min(99,top))
        bottom = top+1
    hx,hy = handle
    if hx < 0:
        left = max(0,min(right-1,left+dx))
    elif hx > 0:
        right = max(left+1,min(100,right+dx))
    if hy < 0:
        top = max(0,min(bottom-1,top+dy))
    elif hy > 0:
        bottom = max(top+1,min(100,bottom+dy))
    # Subtraction near the minimum can yield 0.99999999999999 and fail validation.
    return {"x":(left+right)/2,"y":(top+bottom)/2,
            "width":max(1,min(100,right-left)),"height":max(1,min(100,bottom-top))}


class Preview(QtWidgets.QWidget):
    layerSelected = QtCore.pyqtSignal(str)
    geometryEdited = QtCore.pyqtSignal(str,object)
    geometryCommitted = QtCore.pyqtSignal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setMinimumSize(220,240)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setToolTip("点击文本框选择；拖动框内移动，拖边角缩放。Esc取消当前拖动。蓝色辅助线不会导出。")
        self.background,self.overlay = QtGui.QImage(),QtGui.QImage()
        self.canvas_size = QtCore.QSize(1080,1920)
        self.cover,self.show_bounds = False,True
        self.layers,self.selected_id = [],""
        self._drag = None

    def canvas_rect(self):
        target = QtCore.QSizeF(self.canvas_size)
        target.scale(QtCore.QSizeF(self.size()),QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        return QtCore.QRectF((self.width()-target.width())/2,(self.height()-target.height())/2,
                             target.width(),target.height())

    def layer_rect(self,layer):
        canvas = self.canvas_rect()
        return QtCore.QRectF(canvas.left()+canvas.width()*(layer["x"]-layer["width"]/2)/100,
            canvas.top()+canvas.height()*(layer["y"]-layer["height"]/2)/100,
            canvas.width()*layer["width"]/100,canvas.height()*layer["height"]/100)

    def editable_layers(self):
        return [layer for layer in self.layers if layer.get("enabled") and layer.get("opacity",100)>0
                and all(key in layer for key in GEOMETRY_KEYS)]

    def set_layers(self,layers,selected_id=""):
        self.layers = copy.deepcopy(layers)
        self.selected_id = selected_id
        self.update()

    def handle_rects(self,layer):
        box = self.layer_rect(layer)
        return [(handle,QtCore.QRectF(box.center().x()+handle[0]*box.width()/2-4,
            box.center().y()+handle[1]*box.height()/2-4,8,8)) for handle in HANDLES]

    def hit_test(self,position):
        active = self.editable_layers()
        selected = next((layer for layer in active if layer["id"] == self.selected_id),None)
        if selected:
            for handle,rect in self.handle_rects(selected):
                if rect.adjusted(-3,-3,3,3).contains(position):
                    return selected,handle
        if not self.canvas_rect().contains(position):
            return None,None
        for layer in reversed(active):
            if self.layer_rect(layer).contains(position):
                return layer,None
        return None,None

    def paintEvent(self,_event):
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(),QtGui.QColor("#20242a"))
            canvas = self.canvas_rect()
            painter.fillRect(canvas,QtCore.Qt.GlobalColor.black)
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
            if not self.background.isNull():
                size = QtCore.QSizeF(self.background.size())
                size.scale(canvas.size(),QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding if self.cover
                           else QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                rect = QtCore.QRectF(canvas.center().x()-size.width()/2,canvas.center().y()-size.height()/2,size.width(),size.height())
                painter.save()
                painter.setClipRect(canvas)
                painter.drawImage(rect,self.background)
                painter.restore()
            if not self.overlay.isNull():
                painter.drawImage(canvas,self.overlay)
            elif self.background.isNull():
                painter.setPen(QtGui.QColor("#eeeeee"))
                painter.drawText(canvas,QtCore.Qt.AlignmentFlag.AlignCenter,"选择视频并填写标题／正文")
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            for layer in self.editable_layers():
                selected = layer["id"] == self.selected_id
                if not self.show_bounds and not selected:
                    continue
                box = self.layer_rect(layer)
                painter.setPen(QtGui.QPen(QtGui.QColor("#58c4ff" if selected else "#9fb8ca"),2 if selected else 1,
                    QtCore.Qt.PenStyle.SolidLine if selected else QtCore.Qt.PenStyle.DashLine))
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(box)
                label = f"{layer['name']} · {layer['width']*self.canvas_size.width()/100:.0f} × {layer['height']*self.canvas_size.height()/100:.0f} px"
                font = QtGui.QFont(self.font())
                font.setPixelSize(12)
                painter.setFont(font)
                label_width = min(canvas.width(),painter.fontMetrics().horizontalAdvance(label)+12)
                label_rect = QtCore.QRectF(min(max(canvas.left(),box.left()),canvas.right()-label_width),
                    min(max(canvas.top(),box.top()),canvas.bottom()-20),label_width,20)
                painter.fillRect(label_rect,QtGui.QColor(20,38,52,220))
                painter.setPen(QtGui.QColor("#d8eeff"))
                painter.drawText(label_rect.adjusted(5,0,-5,0),QtCore.Qt.AlignmentFlag.AlignVCenter,label)
                if selected:
                    painter.setPen(QtGui.QPen(QtGui.QColor("#16394d"),1))
                    painter.setBrush(QtGui.QColor("#58c4ff"))
                    for _handle,rect in self.handle_rects(layer):
                        painter.drawRect(rect)
        finally:
            painter.end()

    def mousePressEvent(self,event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        position = event_position(event)
        layer,handle = self.hit_test(position)
        if not layer:
            return super().mousePressEvent(event)
        self.setFocus()
        self.selected_id = layer["id"]
        self.layerSelected.emit(layer["id"])
        self._drag = {"id":layer["id"],"origin":position,"values":{key:layer[key] for key in GEOMETRY_KEYS},"handle":handle,"changed":False}
        self.update()
        event.accept()

    def mouseMoveEvent(self,event):
        position = event_position(event)
        if self._drag:
            canvas = self.canvas_rect()
            delta = position-self._drag["origin"]
            values = dragged_geometry(self._drag["values"],delta.x()*100/canvas.width(),delta.y()*100/canvas.height(),self._drag["handle"])
            layer = next((layer for layer in self.layers if layer["id"] == self._drag["id"]),None)
            if layer and any(layer[key] != values[key] for key in GEOMETRY_KEYS):
                layer.update(values)
                self._drag["changed"] = True
                self.geometryEdited.emit(layer["id"],values)
                self.update()
            event.accept()
            return
        layer,handle = self.hit_test(position)
        cursor = QtCore.Qt.CursorShape.OpenHandCursor if layer else QtCore.Qt.CursorShape.ArrowCursor
        if handle:
            hx,hy = handle
            cursor = (QtCore.Qt.CursorShape.SizeVerCursor if not hx else QtCore.Qt.CursorShape.SizeHorCursor if not hy else
                QtCore.Qt.CursorShape.SizeFDiagCursor if hx == hy else QtCore.Qt.CursorShape.SizeBDiagCursor)
        self.setCursor(cursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if self._drag and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            changed = self._drag["changed"]
            self._drag = None
            if changed:
                self.geometryCommitted.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def cancel_drag(self):
        if self._drag:
            drag,self._drag = self._drag,None
            layer = next((layer for layer in self.layers if layer["id"] == drag["id"]),None)
            if layer and drag["changed"]:
                layer.update(drag["values"])
                self.geometryEdited.emit(layer["id"],drag["values"])
                self.geometryCommitted.emit()
            self.update()

    def keyPressEvent(self,event):
        if self._drag and event.key() == QtCore.Qt.Key.Key_Escape:
            self.cancel_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self,event):
        self.cancel_drag()
        super().hideEvent(event)
