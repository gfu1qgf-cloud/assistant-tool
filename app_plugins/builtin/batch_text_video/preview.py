"""Editable preview surface. Handles and box guides are never exported."""
import copy
from qt_compat import QtCore,QtGui,QtWidgets

GEOMETRY_KEYS = ("x","y","width","height")
HANDLES = ((-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1))


def event_position(event):
    return event.position() if hasattr(event,"position") else QtCore.QPointF(event.pos())


def dragged_geometry(original,dx,dy,handle=None,*,max_size=100):
    """Deltas use percent of the full canvas, not the containing widget."""
    values = {key:float(original[key]) for key in GEOMETRY_KEYS}
    if handle is None:
        # Match the position editor: the BOX CENTER may be anywhere in 0..100.
        # A full-frame/edge-crossing picture is intentional, not an error to
        # repair. Moving X must never snap its unrelated Y back into the frame.
        values["x"] = max(0,min(100,values["x"]+dx))
        values["y"] = max(0,min(100,values["y"]+dy))
        return values
    if max_size > 100:
        left, right = values['x']-values['width']/2, values['x']+values['width']/2
        top, bottom = values['y']-values['height']/2, values['y']+values['height']/2
        hx, hy = handle
        if hx < 0:
            left = max(right-max_size, -right, min(right-1, 200-right, left+dx))
        elif hx > 0:
            right = min(left+max_size, 200-left, max(left+1, -left, right+dx))
        if hy < 0:
            top = max(bottom-max_size, -bottom, min(bottom-1, 200-bottom, top+dy))
        elif hy > 0:
            bottom = min(top+max_size, 200-top, max(top+1, -top, bottom+dy))
        return {'x':(left+right)/2, 'y':(top+bottom)/2,
                'width':max(1,min(max_size,right-left)), 'height':max(1,min(max_size,bottom-top))}
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
    guidesToggled = QtCore.pyqtSignal(bool)
    snappingToggled = QtCore.pyqtSignal(bool)
    orderRequested = QtCore.pyqtSignal(str,int)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setMinimumSize(220,240)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setToolTip("亮边框内是导出画布；外侧空白不属于视频。点击文本框选择；拖动框内移动，拖边角缩放。Esc取消当前拖动。辅助线不会导出。")
        self.background,self.overlay = QtGui.QImage(),QtGui.QImage()
        self.canvas_size = QtCore.QSize(1080,1920)
        self.cover,self.show_bounds = False,True
        self.layers,self.selected_id = [],""
        self._drag = None
        self.guides_visible, self.snapping_enabled = True, True
        self._alignment_guides = []
        self.setToolTip(self.toolTip()+' 右键可对齐、调整层级；拖动时智能吸附，按住Alt临时关闭。')

    def set_guides_visible(self, enabled):
        self.guides_visible = bool(enabled)
        self.update()

    def set_snapping_enabled(self, enabled):
        self.snapping_enabled = bool(enabled)
        self._alignment_guides = []
        self.update()

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
            if getattr(self, "composite_planes", None):
                for mode, image in self.composite_planes:
                    painter.setCompositionMode({"normal": QtGui.QPainter.CompositionMode.CompositionMode_SourceOver,
                        "add": QtGui.QPainter.CompositionMode.CompositionMode_Plus,
                        "screen": QtGui.QPainter.CompositionMode.CompositionMode_Screen}[mode])
                    painter.drawImage(canvas, image)
                painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            elif not self.overlay.isNull():
                painter.drawImage(canvas,self.overlay)
            elif self.background.isNull():
                painter.setPen(QtGui.QColor("#eeeeee"))
                painter.drawText(canvas,QtCore.Qt.AlignmentFlag.AlignCenter,"选择视频并填写标题／正文")
            if self.show_bounds:
                painter.setPen(QtGui.QPen(QtGui.QColor("#8299ac"), 1))
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(canvas.adjusted(.5, .5, -.5, -.5))
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
            if self.guides_visible and self._alignment_guides:
                painter.save()
                painter.setClipRect(canvas.adjusted(-1,-1,1,1))
                painter.setPen(QtGui.QPen(QtGui.QColor('#ed80d5'),1,QtCore.Qt.PenStyle.DashLine))
                for axis, value, _label in self._alignment_guides:
                    if axis == 'x':
                        coordinate = canvas.left()+canvas.width()*value/100
                        painter.drawLine(QtCore.QPointF(coordinate,canvas.top()),QtCore.QPointF(coordinate,canvas.bottom()))
                    else:
                        coordinate = canvas.top()+canvas.height()*value/100
                        painter.drawLine(QtCore.QPointF(canvas.left(),coordinate),QtCore.QPointF(canvas.right(),coordinate))
                painter.restore()
        finally:
            painter.end()

    def create_context_menu(self, position):
        layer, _handle = self.hit_test(position)
        if layer:
            self.setFocus()
            self.selected_id = layer['id']
            self.layerSelected.emit(layer['id'])
            layer = next((v for v in self.editable_layers() if v['id'] == self.selected_id),None)
        menu = QtWidgets.QMenu(self)
        if layer:
            identifier = layer['id']
            menu.addAction('编辑此控件属性',lambda:self.layerSelected.emit(identifier))
            align = menu.addMenu('对齐到画布')
            from .preview_guides import aligned_geometry
            for title,horizontal,vertical in (('水平居中','center',None),('垂直居中',None,'center'),
                    ('画布正中','center','center'),('贴左边','left',None),('贴右边','right',None),
                    ('贴上边',None,'top'),('贴下边',None,'bottom')):
                action = align.addAction(title,lambda _checked=False,h=horizontal,v=vertical:
                    self.align_selected(h,v))
                action.setEnabled(aligned_geometry(layer,horizontal,vertical) is not None)
            order = menu.addMenu('调整层级')
            editable_ids = [v['id'] for v in self.layers]
            index = editable_ids.index(identifier)
            front = order.addAction('上移一层',lambda:self.orderRequested.emit(identifier,-1))
            back = order.addAction('下移一层',lambda:self.orderRequested.emit(identifier,1))
            front.setEnabled(index < len(editable_ids)-1); back.setEnabled(index > 0)
            menu.addSeparator()
        guides = menu.addAction('显示对齐辅助线'); guides.setCheckable(True); guides.setChecked(self.guides_visible)
        guides.toggled.connect(self.set_guides_visible); guides.toggled.connect(self.guidesToggled)
        snapping = menu.addAction('启用智能吸附'); snapping.setCheckable(True); snapping.setChecked(self.snapping_enabled)
        snapping.toggled.connect(self.set_snapping_enabled); snapping.toggled.connect(self.snappingToggled)
        return menu

    def contextMenuEvent(self,event):
        menu = self.create_context_menu(QtCore.QPointF(event.pos()))
        try: menu.exec(event.globalPos())
        finally: menu.deleteLater()

    def align_selected(self,horizontal=None,vertical=None):
        from .preview_guides import aligned_geometry
        layer = next((v for v in self.editable_layers() if v['id'] == self.selected_id),None)
        if layer:
            values = aligned_geometry(layer,horizontal,vertical)
            if values is not None and any(layer[key] != values[key] for key in GEOMETRY_KEYS):
                layer.update(values)
                self.geometryEdited.emit(layer['id'],values)
                self.geometryCommitted.emit()
                self.update()

    def mousePressEvent(self,event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        position = event_position(event)
        layer,handle = self.hit_test(position)
        if not layer:
            return super().mousePressEvent(event)
        identifier = layer["id"]
        self.setFocus()
        self.selected_id = identifier
        self.layerSelected.emit(identifier)
        # Focus-out commits and selection callbacks may replace the preview's
        # copied layers. Never begin a drag using the earlier hit-test snapshot.
        layer = next((item for item in self.editable_layers() if item["id"] == identifier), None)
        self._drag = ({"id":identifier,"origin":position,"values":{key:layer[key] for key in GEOMETRY_KEYS},
                       "handle":handle,"changed":False} if layer else None)
        self.update()
        event.accept()

    def mouseMoveEvent(self,event):
        position = event_position(event)
        if self._drag:
            canvas = self.canvas_rect()
            delta = position-self._drag["origin"]
            # A click (or tiny hand jitter) only selects. In particular, do not
            # run the drag clamping code on a deliberately edge-crossing layer.
            if not self._drag["changed"] and delta.manhattanLength() < QtWidgets.QApplication.startDragDistance():
                event.accept()
                return
            layer = next((layer for layer in self.layers if layer["id"] == self._drag["id"]),None)
            from .layers import MAX_VISUAL_SIZE_PERCENT
            values = dragged_geometry(self._drag["values"],delta.x()*100/canvas.width(),delta.y()*100/canvas.height(),self._drag["handle"],
                max_size=MAX_VISUAL_SIZE_PERCENT if layer and layer['kind'] == 'image' else 100)
            self._alignment_guides = []
            if self.snapping_enabled and not event.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier:
                from .preview_guides import snapped_geometry
                values,self._alignment_guides = snapped_geometry(values,self.layers,self._drag['id'],
                    (canvas.width(),canvas.height()),handle=self._drag['handle'],
                    max_size=MAX_VISUAL_SIZE_PERCENT if layer and layer['kind'] == 'image' else 100)
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
            self._alignment_guides = []
            self.update()
            if changed:
                self.geometryCommitted.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def cancel_drag(self):
        self._alignment_guides = []
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
