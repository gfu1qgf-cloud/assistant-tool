"""Bounded Gaussian surfaces shared by typography and subtitle design previews."""
import math

from qt_compat import QtCore, QtGui


def shadow_extent(layer):
    if not layer.get('shadow_enabled'):
        return 0
    return max(abs(layer.get('shadow_x', 0)), abs(layer.get('shadow_y', 0))) + 3*layer.get('shadow_blur', 0)


def paint_blurred(painter, bounds, radius, draw):
    """Blur premultiplied colour+alpha, retaining the caller's clip/transform.

    Pillow is already an application dependency. No QGraphicsScene/GUI-owned
    effects are constructed in the preview/render worker threads.
    """
    if radius <= 0:
        draw(painter)
        return
    from PIL import Image, ImageFilter
    extent = math.ceil(radius*3)+2
    bounds = QtCore.QRectF(bounds).adjusted(-extent, -extent, extent, extent).toAlignedRect()
    if bounds.isEmpty():
        return
    if bounds.width()*bounds.height() > 64*1024*1024:
        raise ValueError('阴影模糊图层过大，请减小模糊半径或拆分文字。')
    surface = QtGui.QImage(bounds.size(), QtGui.QImage.Format.Format_RGBA8888_Premultiplied)
    if surface.isNull():
        raise ValueError('阴影模糊所需内存不足，请减小模糊半径。')
    surface.fill(QtCore.Qt.GlobalColor.transparent)
    local = QtGui.QPainter(surface)
    try:
        local.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        local.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        local.translate(-bounds.left(), -bounds.top())
        draw(local)
    finally:
        local.end()
    pixels = Image.frombytes('RGBA', (surface.width(), surface.height()),
        surface.constBits().asstring(surface.sizeInBytes()), 'raw', 'RGBA', surface.bytesPerLine())
    pixels = pixels.filter(ImageFilter.GaussianBlur(radius))
    data = pixels.tobytes()
    blurred = QtGui.QImage(data, surface.width(), surface.height(), surface.width()*4,
        QtGui.QImage.Format.Format_RGBA8888_Premultiplied)
    painter.drawImage(bounds.topLeft(), blurred)


def layout_bounds(layout):
    if hasattr(layout, 'placements'):  # Upright vertical text uses native QTextLayouts too.
        result = QtCore.QRectF()
        for native, x, y in layout.placements:
            result = result.united(layout_bounds(native).translated(x, y))
        return result
    # QTextLayout's allocation can be one million pixels wide for an upright
    # glyph. Only its shaped lines are ink; never allocate the full wrap width.
    result = QtCore.QRectF()
    for index in range(layout.lineCount()):
        result = result.united(layout.lineAt(index).naturalTextRect())
    return result
