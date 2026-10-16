"""Predictable wire geometry; no fixed upward detour on long connections."""
from qt_compat import QtCore, QtGui

WIRE_STYLES = (("smooth", "平滑曲线"), ("straight", "直线"),
               ("elbow", "直角折线"), ("rounded", "圆角折线"))


def connection_path(start, end, style="smooth", orientation='horizontal'):
    if orientation == 'vertical':
        transposed = connection_path(QtCore.QPointF(start.y(), start.x()), QtCore.QPointF(end.y(), end.x()), style)
        return QtGui.QTransform(0, 1, 1, 0, 0, 0).map(transposed)
    path = QtGui.QPainterPath(start)
    dx, dy = end.x() - start.x(), end.y() - start.y()
    if start == end:
        return path
    if style == "straight":
        path.lineTo(end)
        return path
    if style == "smooth":
        if dx > 0:
            # Even a tiny forward gap is forward. Ordered control-point X values
            # keep the entire curve monotonic and prevent crossing/looping.
            reach = min(dx / 2, 200.0, max(24.0, dx * .45 + abs(dy) * .15))
            path.cubicTo(start + QtCore.QPointF(reach, 0), end - QtCore.QPointF(reach, 0), end)
            return path
        if dx == 0:
            path.lineTo(end)
            return path
        # Only genuinely backward wires use an outside lane. Round a simple
        # non-crossing polyline; the old two opposing cubic lobes crossed.
        return _rounded_path(_backward_points(start, end), 28.0)
    if style not in {"elbow", "rounded"}:
        raise ValueError("未知连线样式。")
    if dx == 0:
        path.lineTo(end)
        return path
    if dx > 0:
        middle = (start.x() + end.x()) / 2
        points = [start, QtCore.QPointF(middle, start.y()), QtCore.QPointF(middle, end.y()), end]
    else:
        points = _backward_points(start, end)
    return _rounded_path(points, 0.0 if style == "elbow" else 14.0)


def _backward_points(start, end):
    reach = 36.0
    dy = end.y() - start.y()
    lane = (start.y() + end.y()) / 2 if abs(dy) >= 80 else min(start.y(), end.y()) - 60
    return [start, start + QtCore.QPointF(reach, 0), QtCore.QPointF(start.x() + reach, lane),
            QtCore.QPointF(end.x() - reach, lane), end - QtCore.QPointF(reach, 0), end]


def _rounded_path(points, maximum_radius):
    path = QtGui.QPainterPath(points[0])
    # Remove coincident / collinear waypoints before rounding corners.
    clean = []
    for point in points:
        if clean and point == clean[-1]:
            continue
        if len(clean) >= 2:
            a, b = clean[-2], clean[-1]
            ab, bc = b - a, point - b
            if abs(ab.x() * bc.y() - ab.y() * bc.x()) < 1e-8 and ab.x() * bc.x() + ab.y() * bc.y() >= 0:
                clean.pop()
        clean.append(point)
    if not maximum_radius:
        for point in clean[1:]:
            path.lineTo(point)
        return path
    for previous, corner, following in zip(clean, clean[1:], clean[2:]):
        before, after = previous - corner, following - corner
        before_length, after_length = (before.x() ** 2 + before.y() ** 2) ** .5, (after.x() ** 2 + after.y() ** 2) ** .5
        radius = min(maximum_radius, before_length / 2, after_length / 2)
        entry, exit_ = corner + before * (radius / before_length), corner + after * (radius / after_length)
        path.lineTo(entry)
        path.quadTo(corner, exit_)
    path.lineTo(points[-1])
    return path


def closest_point(path, position):
    """Project clicks onto the path, including curves and orthogonal segments."""
    if path.isEmpty():
        return QtCore.QPointF(position)
    distance = lambda t: (path.pointAtPercent(t) - position).manhattanLength()
    steps = 128
    best = min(range(steps + 1), key=lambda i: distance(i / steps))
    low, high = max(0, (best - 1) / steps), min(1, (best + 1) / steps)
    for _ in range(18):
        a, b = (2 * low + high) / 3, (low + 2 * high) / 3
        if distance(a) < distance(b):
            high = b
        else:
            low = a
    return path.pointAtPercent((low + high) / 2)
