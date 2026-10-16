"""Render independently positioned, auto-sized text components."""
import math
from qt_compat import QtCore, QtGui
from .layout import _text_layout, TextDoesNotFit
from .shadow_render import shadow_extent, paint_blurred, layout_bounds


def text_padding(layer, scale):
    padding = max(2, layer.get("padding", 4)*scale,
        (layer.get("outline_width", 2)+3*layer.get('outline_blur',0))*scale+1)
    if layer.get("shadow_enabled"):
        padding = max(padding, (shadow_extent(layer)+1)*scale)
    return padding


def draw_text_groups(painter, groups, layer, x, y, scale):
    """One typography path for static boxes and animated text strips."""
    painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    outline = max(1, round(layer.get("outline_width", 2)*scale)) if layer.get("outline_width", 2) else 0
    if layer.get("shadow_enabled"):
        color = QtGui.QColor(layer["shadow_color"])
        color.setAlpha(round(layer["shadow_opacity"]*2.55))
        sx, sy = x+layer['shadow_x']*scale, y+layer['shadow_y']*scale
        def draw_shadow(target):
            target.setPen(color)
            for layout, offset in groups:
                layout.draw(target, QtCore.QPointF(sx, sy+offset))
        bounds = QtCore.QRectF()
        for layout, offset in groups:
            bounds = bounds.united(layout_bounds(layout).translated(sx, sy+offset))
        paint_blurred(painter, bounds, layer.get('shadow_blur', 0)*scale, draw_shadow)
    if outline:
        def draw_outline(target):
            color = QtGui.QColor(layer.get("outline_color", "#000000"))
            color.setAlpha(round(layer.get("outline_opacity", 84.31)*2.55))
            target.setPen(color)
            offsets = [(-outline, 0), (outline, 0), (0, -outline), (0, outline)]
            if outline > 2:
                offsets += [(-outline, -outline), (-outline, outline), (outline, -outline), (outline, outline)]
            for layout, offset in groups:
                for dx, dy in offsets:
                    layout.draw(target, QtCore.QPointF(x+dx, y+offset+dy))
        bounds = QtCore.QRectF()
        for layout,offset in groups:
            bounds=bounds.united(layout_bounds(layout).translated(x,y+offset))
        paint_blurred(painter,bounds.adjusted(-outline,-outline,outline,outline),
            layer.get('outline_blur',0)*scale,draw_outline)
    for layout, offset in groups:
        painter.setPen(QtGui.QColor(layer["color"]))
        layout.draw(painter, QtCore.QPointF(x, y+offset))


def render_text_box(layer, width, height, *, _local_surface=False):
    if layer.get("_watermark_corner") and layer.get("watermark_placement") == "corner":
        from .watermark_render import render_corner_watermark
        return render_corner_watermark(layer, width, height)
    if layer.get('_caption_style_design'):
        from .caption_style_render import render_design
        return render_design(layer,width,height)
    from .scrolling_text import is_scrolling, render_scroll_preview
    if is_scrolling(layer):
        return render_scroll_preview(layer, width, height)
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
    padding = text_padding(layer, scale)
    box.adjust(padding,padding,-padding,-padding)
    if box.width() <= 1 or box.height() <= 1:
        raise TextDoesNotFit(layer["name"]+"：文本框没有足够的可见空间。")
    def measure(fraction):
        size = (layer["font_min"]+fraction*(layer["font_max"]-layer["font_min"]))*scale
        if layer.get("writing_mode", "horizontal") != "horizontal":
            from .vertical_text import text_layout
            groups, total, used_width = text_layout(layer["text"], layer["font"], size,
                box.width(), box.height(), layer["align"], layer["line_spacing"], layer, scale)
            return groups, total, size, used_width
        groups, total = _text_layout(layer["text"],layer["font"],size,
                                     box.width(),layer["align"],layer["line_spacing"],layer,scale)
        return groups,total,size,box.width()
    def fits(candidate):
        return candidate[1] <= box.height()+1e-6 and candidate[3] <= box.width()+1e-6
    chosen = measure(0)
    if not fits(chosen):
        raise TextDoesNotFit(layer["name"]+"：最小字号仍放不下内容，请扩大文本框或降低最小字号；未裁掉文字。")
    maximum = measure(1)
    if fits(maximum):
        chosen = maximum
    else:
        low,high = 0.,1.
        for _ in range(12):
            middle = (low+high)/2
            candidate = measure(middle)
            if fits(candidate):
                low,chosen = middle,candidate
            else:
                high = middle
    groups,total,size,_ = chosen
    y = box.top()+(box.height()-total)*{"top":0,"center":.5,"bottom":1}[layer["vertical"]]
    if _local_surface:
        expansion = max(layer.get("background_extra_" + side, 0) for side in ("left", "right", "top", "bottom"))
        shadow = shadow_extent(layer)
        bleed = math.ceil((expansion + shadow + layer.get("outline_width", 0)+3*layer.get('outline_blur',0)) * scale + size * 2 + 4)
        surface_width = math.ceil(background_box.width()) + 2 * bleed
        surface_height = math.ceil(background_box.height()) + 2 * bleed
    else:
        surface_width, surface_height = width, height
    image = QtGui.QImage(surface_width,surface_height,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        if _local_surface:
            painter.translate(bleed - background_box.left(), bleed - background_box.top())
        from .text_background import draw_background
        draw_background(painter,layer,groups,box.left(),y,scale,background_box)
        draw_text_groups(painter, groups, layer, box.left(), y, scale)
    finally:
        painter.end()
    return image,size/scale
