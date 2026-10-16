"""Text-following backgrounds share measured typography, not guessed bounds."""
import math
from qt_compat import QtCore, QtGui

DEFAULTS = {"background_mode":"box", "background_join_lines":True,
            "background_extra_left":0., "background_extra_right":0.,
            "background_extra_top":0., "background_extra_bottom":0.}


def normalize_background(layer):
    for key, value in DEFAULTS.items(): layer.setdefault(key,value)
    if not isinstance(layer["background_mode"],str) or layer["background_mode"] not in {"box","text"}:
        raise ValueError("文本底色模式无效。")
    if not isinstance(layer["background_join_lines"],bool):
        raise ValueError("连接相邻行底色必须为true或false。")
    for key in ("background_extra_left","background_extra_right","background_extra_top","background_extra_bottom"):
        value = layer[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 300:
            raise ValueError("底色四方向扩展须为0–300像素，按1080宽画布计算。")


def text_rectangles(groups, x, y):
    """Natural QTextLine rectangles include alignment, fallback and UTF-16 runs.

    Vertical text exposes its already measured columns. Empty/whitespace-only
    lines have no background, but spaces between words stay inside the band.
    """
    rectangles = []
    for layout, offset in groups:
        if hasattr(layout,"background_rects"):
            rectangles.extend(rect.translated(x,y+offset) for rect in layout.background_rects)
            continue
        encoded = layout.text().encode("utf-16-le","surrogatepass")
        for index in range(layout.lineCount()):
            line = layout.lineAt(index)
            start, end = line.textStart(), line.textStart()+line.textLength()
            if not encoded[start*2:end*2].decode("utf-16-le","surrogatepass").strip(): continue
            rect = line.naturalTextRect().translated(x,y+offset)
            if rect.width() > 0 and rect.height() > 0: rectangles.append(rect)
    return rectangles


def background_path(layer, groups, x, y, scale, box):
    mode = layer.get("background_mode","box")
    natural = text_rectangles(groups,x,y) if mode == "text" else [QtCore.QRectF(box)]
    left, right, top, bottom = (layer.get("background_extra_"+side,0)*scale for side in ("left","right","top","bottom"))
    rectangles = [rect.adjusted(-left,-top,right,bottom) for rect in natural]
    path = QtGui.QPainterPath()
    # One fill means translucent overlapping bands do not become darker.
    path.setFillRule(QtCore.Qt.FillRule.WindingFill)
    radius = layer.get("background_radius",12)*scale
    for rect in rectangles:
        path.addRoundedRect(rect,min(radius,rect.width()/2),min(radius,rect.height()/2))
    if mode == "text" and layer.get("background_join_lines",True):
        vertical = layer.get("writing_mode","horizontal") != "horizontal"
        ordered = sorted(zip(natural,rectangles),key=lambda pair:pair[0].left() if vertical else pair[0].top())
        for (original_a,a),(original_b,b) in zip(ordered,ordered[1:]):
            if vertical:
                gap, base_size = original_b.left()-original_a.right(), min(original_a.width(),original_b.width())
                distance, size = b.left()-a.right(), min(a.width(),b.width())
                start, end = max(a.top(),b.top()), min(a.bottom(),b.bottom())
            else:
                gap, base_size = original_b.top()-original_a.bottom(), min(original_a.height(),original_b.height())
                distance, size = b.top()-a.bottom(), min(a.height(),b.height())
                start, end = max(a.left(),b.left()), min(a.right(),b.right())
            # Decide adjacency before expansion; small pads must not suddenly
            # bridge an explicit blank paragraph between title and body.
            if gap > base_size*.65 or end <= start: continue
            inset = min(radius,(end-start)/4,size/4)
            if vertical:
                bridge = QtCore.QRectF(a.right()-inset,start+inset,max(0,distance+2*inset),end-start-2*inset)
            else:
                bridge = QtCore.QRectF(start+inset,a.bottom()-inset,end-start-2*inset,max(0,distance+2*inset))
            if bridge.width() > 0 and bridge.height() > 0: path.addRect(bridge)
    return path


def draw_background(painter, layer, groups, x, y, scale, box):
    if not layer.get("background_enabled"): return
    color = QtGui.QColor(layer.get("background_color","#000000"))
    color.setAlpha(round(layer.get("background_opacity",35)*2.55))
    painter.save()
    try:
        # Preserve the original full-box rasterization when no new feature is
        # enabled; existing projects must not subtly change edge pixels.
        legacy = layer.get("background_mode","box") == "box" and not any(
            layer.get("background_extra_"+side,0) for side in ("left","right","top","bottom"))
        if not legacy: painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen); painter.setBrush(color)
        if legacy:
            radius = layer.get("background_radius",12)*scale
            painter.drawRoundedRect(box,radius,radius)
        else:
            painter.drawPath(background_path(layer,groups,x,y,scale,box))
    finally: painter.restore()
