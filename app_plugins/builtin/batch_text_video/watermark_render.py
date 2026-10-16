"""Anchor the actual visible watermark (including effects), not its empty box."""
import copy
import numpy as np
import sys
from qt_compat import QtCore, QtGui


def alpha_bounds(image):
    image = image.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        return QtCore.QRect()
    data = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8)
    rows = data.reshape(image.height(), image.bytesPerLine())
    pixels = rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4)
    alpha = pixels[:, :, 3 if sys.byteorder == "little" else 0]
    ys = np.flatnonzero(alpha.any(axis=1))
    xs = np.flatnonzero(alpha.any(axis=0))
    if not len(xs) or not len(ys):
        return QtCore.QRect()
    return QtCore.QRect(int(xs[0]), int(ys[0]), int(xs[-1] - xs[0] + 1), int(ys[-1] - ys[0] + 1))


def render_corner_watermark(layer, width, height):
    from .text_components import render_text_box
    from .layout import TextDoesNotFit
    local = copy.deepcopy(layer)
    corner = local.pop("_watermark_corner")
    # A safe local surface removes layout whitespace without losing strokes,
    # shadows or expanded backgrounds. Typography/fitting stays in one backend.
    local["x"] = local["y"] = 50
    surface, size = render_text_box(local, width, height, _local_surface=True)
    bounds = alpha_bounds(surface)
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    if bounds.isEmpty():
        return image, size
    unit = layer.get("watermark_inset_unit", "pixels")
    mx = round(layer.get("watermark_inset_x", 12) * (width / 100 if unit == "percent" else 1))
    my = round(layer.get("watermark_inset_y", 12) * (height / 100 if unit == "percent" else 1))
    if mx + bounds.width() > width or my + bounds.height() > height:
        raise TextDoesNotFit(layer["name"] + "：内缩距离加水印大小超过画布，请减小内缩、底色扩展或字号；未裁掉水印。")
    x = mx if corner.endswith("left") else width - mx - bounds.width()
    y = my if corner.startswith("top") else height - my - bounds.height()
    painter = QtGui.QPainter(image)
    try:
        painter.drawImage(QtCore.QPoint(x, y), surface.copy(bounds))
    finally:
        painter.end()
    return image, size
