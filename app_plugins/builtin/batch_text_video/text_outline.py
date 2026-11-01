"""Outside-only glyph outlines, shared by static Qt text and native captions."""
from qt_compat import QtCore, QtGui


def outside_ring(path, width):
    if width <= 0 or path.isEmpty():
        return QtGui.QPainterPath()
    stroker = QtGui.QPainterPathStroker()
    stroker.setWidth(width * 2)
    stroker.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    stroker.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    # Remove the filled glyph, including overlapping font contours. Counters
    # remain outside the ink, just as with an ordinary exterior text outline.
    return stroker.createStroke(path).subtracted(path)


def ass_commands(path):
    """ASS p4 uses eighth-pixel integer coordinates, avoiding decimal bloat."""
    import re
    from .captions import _path_commands
    return re.sub(r'-?\d+\.\d+', lambda match: str(round(float(match[0])*8)), _path_commands(path))


def layout_path(layout):
    """Use already shaped runs: preserve fallback fonts, bidi and vertical cells."""
    result = QtGui.QPainterPath()
    result.setFillRule(QtCore.Qt.FillRule.WindingFill)
    if hasattr(layout, 'placements'):
        for native, x, y in layout.placements:
            result.addPath(layout_path(native).translated(x, y))
        return result.simplified()
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
    return result.simplified()


def draw_outside(painter, groups, layer, x, y, scale):
    from .shadow_render import paint_blurred
    path = QtGui.QPainterPath()
    path.setFillRule(QtCore.Qt.FillRule.WindingFill)
    for layout, offset in groups:
        path.addPath(layout_path(layout).translated(x, y + offset))
    ring = outside_ring(path.simplified(), layer['outline_width'] * scale)
    color = QtGui.QColor(layer['outline_color'])
    color.setAlpha(round(layer['outline_opacity'] * 2.55))
    def draw(target):
        target.fillPath(ring, color)
    paint_blurred(painter, ring.boundingRect(), layer.get('outline_blur', 0) * scale, draw)
