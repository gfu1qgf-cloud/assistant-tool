"""Render independently positioned, auto-sized text components."""
from qt_compat import QtCore, QtGui
from .layout import _text_layout, TextDoesNotFit


def render_text_box(layer, width, height):
    if not layer["text"].strip():
        image = QtGui.QImage(width,height,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        return image,0
    scale = width/1080
    box = QtCore.QRectF(width*(layer["x"]-layer["width"]/2)/100,
                        height*(layer["y"]-layer["height"]/2)/100,
                        width*layer["width"]/100, height*layer["height"]/100)
    box = box.intersected(QtCore.QRectF(0,0,width,height))
    background_box = QtCore.QRectF(box)
    padding = max(2,layer.get("padding",4)*scale,layer.get("outline_width",2)*scale+1)
    if layer.get("shadow_enabled"):
        padding = max(padding,(max(abs(layer["shadow_x"]),abs(layer["shadow_y"]))+1)*scale)
    box.adjust(padding,padding,-padding,-padding)
    if box.width() <= 1 or box.height() <= 1:
        raise TextDoesNotFit(layer["name"]+"：文本框没有足够的可见空间。")
    def measure(fraction):
        size = (layer["font_min"]+fraction*(layer["font_max"]-layer["font_min"]))*scale
        groups, total = _text_layout(layer["text"],layer["font"],size,
                                     box.width(),layer["align"],layer["line_spacing"],layer,scale)
        return groups,total,size
    chosen = measure(0)
    if chosen[1] > box.height():
        raise TextDoesNotFit(layer["name"]+"：最小字号仍放不下内容，请扩大文本框或降低最小字号；未裁掉文字。")
    maximum = measure(1)
    if maximum[1] <= box.height():
        chosen = maximum
    else:
        low,high = 0.,1.
        for _ in range(12):
            middle = (low+high)/2
            candidate = measure(middle)
            if candidate[1] <= box.height():
                low,chosen = middle,candidate
            else:
                high = middle
    groups,total,size = chosen
    y = box.top()+(box.height()-total)*{"top":0,"center":.5,"bottom":1}[layer["vertical"]]
    image = QtGui.QImage(width,height,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    outline = max(1,round(layer.get("outline_width",2)*scale)) if layer.get("outline_width",2) else 0
    def color_with_opacity(key,default,opacity):
        color = QtGui.QColor(layer.get(key,default))
        color.setAlpha(round(layer.get(opacity,100)*2.55))
        return color
    try:
        if layer.get("background_enabled"):
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(color_with_opacity("background_color","#000000","background_opacity"))
            radius = layer["background_radius"]*scale
            painter.drawRoundedRect(background_box,radius,radius)
        if layer.get("shadow_enabled"):
            painter.setPen(color_with_opacity("shadow_color","#000000","shadow_opacity"))
            for layout,offset in groups:
                layout.draw(painter,QtCore.QPointF(box.left()+layer["shadow_x"]*scale,y+offset+layer["shadow_y"]*scale))
        for layout,offset in groups:
            if outline:
                color = QtGui.QColor(layer.get("outline_color","#000000"))
                color.setAlpha(round(layer.get("outline_opacity",84.31)*2.55))
                painter.setPen(color)
                offsets = [(-outline,0),(outline,0),(0,-outline),(0,outline)]
                if outline > 2:
                    offsets += [(-outline,-outline),(-outline,outline),(outline,-outline),(outline,outline)]
                for dx,dy in offsets:
                    layout.draw(painter,QtCore.QPointF(box.left()+dx,y+offset+dy))
            painter.setPen(QtGui.QColor(layer["color"]))
            layout.draw(painter,QtCore.QPointF(box.left(),y+offset))
    finally:
        painter.end()
    return image,size/scale
