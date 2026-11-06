"""Outside-only glyph outlines, shared by static Qt text and native captions."""
from qt_compat import QtCore, QtGui
from collections import OrderedDict
import hashlib
import threading

_RINGS = OrderedDict()
_LOCK = threading.RLock()
_ELEMENTS = 0
ELEMENT_LIMIT = 200_000


def outside_ring(path, width):
    global _ELEMENTS
    if width <= 0 or path.isEmpty():
        return QtGui.QPainterPath()
    data = QtCore.QByteArray()
    stream = QtCore.QDataStream(data, QtCore.QIODevice.OpenModeFlag.WriteOnly)
    stream << path
    key = (hashlib.sha256(bytes(data)).digest(), float(width))
    with _LOCK:
        hit = _RINGS.get(key)
        if hit is not None:
            _RINGS.move_to_end(key)
            return QtGui.QPainterPath(hit)
    stroker = QtGui.QPainterPathStroker()
    stroker.setWidth(width * 2)
    stroker.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    stroker.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    # Remove the filled glyph, including overlapping font contours. Counters
    # remain outside the ink, just as with an ordinary exterior text outline.
    result = stroker.createStroke(path).subtracted(path)
    size = result.elementCount()
    if size <= ELEMENT_LIMIT:
        with _LOCK:
            if key not in _RINGS:
                _RINGS[key] = QtGui.QPainterPath(result); _ELEMENTS += size
                while _ELEMENTS > ELEMENT_LIMIT or len(_RINGS) > 128:
                    _, old = _RINGS.popitem(last=False); _ELEMENTS -= old.elementCount()
    return result


def ass_commands(path):
    """ASS p4 uses eighth-pixel integer coordinates, avoiding decimal bloat."""
    import re
    from .captions import _path_commands
    return re.sub(r'-?\d+\.\d+', lambda match: str(round(float(match[0])*8)), _path_commands(path))


def layout_path(layout, *, simplify=True):
    """Use already shaped runs: preserve fallback fonts, bidi and vertical cells."""
    result = QtGui.QPainterPath()
    result.setFillRule(QtCore.Qt.FillRule.WindingFill)
    if hasattr(layout, 'placements'):
        for native, x, y in layout.placements:
            result.addPath(layout_path(native, simplify=False).translated(x, y))
        return result.simplified() if simplify else result
    for run in layout.glyphRuns():
        raw = run.rawFont()
        for glyph, position in zip(run.glyphIndexes(), run.positions()):
            result.addPath(raw.pathForGlyph(glyph).translated(position))
    font = layout.font()
    if font.underline() or font.strikeOut() or font.overline():
        metrics = QtGui.QFontMetricsF(font)
        for i in range(layout.lineCount()):
            line = layout.lineAt(i)
            rect = line.naturalTextRect()
            baseline = line.y() + line.ascent()
            ys = []
            if font.underline(): ys.append(baseline + metrics.underlinePos())
            if font.strikeOut(): ys.append(baseline - metrics.strikeOutPos())
            if font.overline(): ys.append(baseline - metrics.ascent())
            for y in ys:
                result.addRect(rect.left(), y, rect.width(), max(1., metrics.lineWidth()))
    return result.simplified() if simplify else result


def draw_outside(painter, groups, layer, x, y, scale):
    import numpy as np
    import cv2
    from .shadow_render import paint_blurred, layout_bounds
    bounds = QtCore.QRectF()
    for layout, offset in groups:
        bounds = bounds.united(layout_bounds(layout).translated(x,y+offset))
    width = layer['outline_width'] * scale
    bounds = bounds.adjusted(-width-3, -width-3, width+3, width+3).toAlignedRect()
    if bounds.isEmpty(): return
    if bounds.width()*bounds.height() > 64*1024*1024:
        raise ValueError('向外描边画面过大，请减小字号或拆分文字。')
    # Shape with QTextLayout once. A distance field expands its alpha in time
    # proportional to surface area, independent of paragraph contour count.
    surface = QtGui.QImage(bounds.size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    if surface.isNull(): raise ValueError('向外描边所需内存不足，请减小字号。')
    surface.fill(QtCore.Qt.GlobalColor.transparent)
    local = QtGui.QPainter(surface)
    local.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    local.translate(-bounds.left(), -bounds.top())
    color = QtGui.QColor(layer['outline_color'])
    color.setAlpha(round(layer['outline_opacity'] * 2.55))
    try:
        local.setPen(QtGui.QColor('white'))
        for layout, offset in groups:
            layout.draw(local, QtCore.QPointF(x,y+offset))
    finally: local.end()
    alpha = surface.convertToFormat(QtGui.QImage.Format.Format_Alpha8)
    ink = np.frombuffer(alpha.constBits().asstring(alpha.sizeInBytes()), np.uint8).reshape(
        alpha.height(),alpha.bytesPerLine())[:,:alpha.width()]
    if not np.any(ink): return
    distance = cv2.distanceTransform(np.where(ink >= 128,0,255).astype(np.uint8),cv2.DIST_L2,5)
    expanded = np.rint(np.clip(width+.5-distance,0,1)*255).astype(np.uint8)
    ring = cv2.subtract(np.maximum(expanded,ink),ink)
    rgba = np.empty((alpha.height(),alpha.width(),4),np.uint8)
    rgba[:,:,:3] = [color.red(),color.green(),color.blue()]
    rgba[:,:,3] = np.rint(ring.astype(np.float32)*color.alpha()/255).astype(np.uint8)
    pixels = rgba.tobytes()
    surface = QtGui.QImage(pixels,alpha.width(),alpha.height(),alpha.width()*4,QtGui.QImage.Format.Format_RGBA8888)
    def draw(target):
        target.drawImage(bounds.topLeft(), surface)
    paint_blurred(painter, bounds, layer.get('outline_blur', 0) * scale, draw)
