"""Validated simple polygons and exact per-line-band exclusion projections."""
import math
from functools import lru_cache
from qt_compat import QtCore, QtGui

MAX_POLYGONS, MAX_VERTICES = 16, 128


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _on_segment(a, b, p):
    return (abs(_cross(a, b, p)) < 1e-8 and min(a[0], b[0])-1e-8 <= p[0] <= max(a[0], b[0])+1e-8
            and min(a[1], b[1])-1e-8 <= p[1] <= max(a[1], b[1])+1e-8)


def _intersects(a, b, c, d):
    ab, ba, cd, dc = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    return (ab*ba < 0 and cd*dc < 0 or _on_segment(a, b, c) or _on_segment(a, b, d)
            or _on_segment(c, d, a) or _on_segment(c, d, b))


def normalize_polygons(polygons):
    if not isinstance(polygons, list) or len(polygons) > MAX_POLYGONS:
        raise ValueError(f'最多允许{MAX_POLYGONS}个多边形避让区域。')
    ids = set()
    for item in polygons:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not item['id'] or len(item['id']) > 128 or item['id'] in ids):
            raise ValueError('多边形区域编号无效或重复。')
        ids.add(item['id'])
        item.setdefault('enabled', True)
        if type(item['enabled']) is not bool:
            raise ValueError('多边形区域开关无效。')
        points = item.get('points')
        if not isinstance(points, list) or not 3 <= len(points) <= MAX_VERTICES:
            raise ValueError(f'每个多边形需要3–{MAX_VERTICES}个顶点。')
        if any(not isinstance(p, (list, tuple)) or len(p) != 2 or any(type(v) not in (int, float)
                or not math.isfinite(v) or not 0 <= v <= 100 for v in p) for p in points):
            raise ValueError('多边形顶点须位于画布内，坐标为0–100%。')
        if len({tuple(p) for p in points}) != len(points):
            raise ValueError('多边形有重复顶点，请移开重叠的点。')
        _validate_geometry(tuple(tuple(point) for point in points))
    return polygons


@lru_cache(maxsize=128)
def _validate_geometry(points):
    """Reuse only a complete immutable geometry key; batch jobs share contours."""
    n = len(points)
    for i, a in enumerate(points):
        b, c = points[(i+1) % n], points[(i+2) % n]
        if abs(_cross(a, b, c)) < 1e-8 and _on_segment(a, b, c):
            raise ValueError('多边形边线折返重叠，请调整顶点。')
        for j in range(i+2, n):
            if i == 0 and j == n-1:
                continue
            if _intersects(a, b, points[j], points[(j+1) % n]):
                raise ValueError('多边形边线不能交叉，请调整顶点。')
    area = abs(sum(points[i][0]*points[(i+1) % n][1]-points[(i+1) % n][0]*points[i][1]
                   for i in range(n)))/2
    if area < .0001:
        raise ValueError('多边形面积太小或顶点共线。')


class PolygonExclusion:
    """QPainterPath supplies concave clipping, round padding and disconnected bands."""
    def __init__(self, path):
        self.path = path
        self.bounds = path.boundingRect()
        self.cache = {}
        self.edges = []
        for polygon in path.toSubpathPolygons():
            points = list(polygon)
            for a, b in zip(points, points[1:]+points[:1]):
                if abs(a.y()-b.y()) > 1e-8:
                    self.edges.append((a.x(), a.y(), b.x(), b.y()))

    def left(self): return self.bounds.left()
    def right(self): return self.bounds.right()
    def top(self): return self.bounds.top()
    def bottom(self): return self.bounds.bottom()
    def intersects(self, rect): return self.path.intersects(rect)

    def translated(self, dx, dy):
        return PolygonExclusion(self.path.translated(dx, dy))

    def band_intervals(self, y, height):
        key = (y, height)
        if key not in self.cache:
            # Boolean clipping can connect disjoint subpaths with rewind edges.
            # Project each stable scanline strip instead, preserving concave gaps.
            edges = [edge for edge in self.edges if min(edge[1], edge[3]) < y+height and max(edge[1], edge[3]) > y]
            events = sorted({y, y+height, *(v for edge in edges for v in (edge[1], edge[3]) if y < v < y+height)})
            spans = []
            def at(edge, ordinate):
                ax, ay, bx, by = edge
                return ax+(ordinate-ay)*(bx-ax)/(by-ay)
            for top, bottom in zip(events, events[1:]):
                if bottom-top < 1e-8:
                    continue
                middle = (top+bottom)/2
                crossing = sorted((at(edge, middle), index) for index, edge in enumerate(edges)
                                  if min(edge[1], edge[3]) < middle < max(edge[1], edge[3]))
                for left, right in zip(crossing[::2], crossing[1::2]):
                    values = [at(edges[index], ordinate) for index in (left[1], right[1]) for ordinate in (top, bottom)]
                    spans.append((min(values), max(values)))
            if len(self.cache) >= 512:
                self.cache.clear()
            self.cache[key] = spans
        return self.cache[key]


def polygon_exclusions(polygons, width, height, gap):
    result = []
    for item in polygons:
        if not item.get('enabled', True):
            continue
        path = QtGui.QPainterPath()
        path.addPolygon(QtGui.QPolygonF([QtCore.QPointF(x*width/100, y*height/100) for x, y in item['points']]))
        path.closeSubpath()
        if gap > 0:
            stroke = QtGui.QPainterPathStroker()
            stroke.setWidth(2*gap)
            stroke.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            path = path.united(stroke.createStroke(path))
        result.append(PolygonExclusion(path))
    return result


def exclusion_path(shape):
    if isinstance(shape, PolygonExclusion):
        return shape.path
    path = QtGui.QPainterPath(); path.addRect(shape)
    return path
