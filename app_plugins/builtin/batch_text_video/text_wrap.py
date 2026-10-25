"""Word-style rectangular exclusions, shaped once for preview and export."""
import copy
import math
import unicodedata
from qt_compat import QtCore, QtGui

DEFAULTS = {'wrap_enabled': False, 'wrap_targets': [], 'wrap_gap': 12., 'wrap_side': 'both', 'wrap_polygons': []}
SIDES = (('两侧环绕', 'both'), ('只走较宽一侧', 'largest'), ('只走左侧', 'left'), ('只走右侧', 'right'))


def supported(layer):
    return (layer.get('kind') in {'title', 'body', 'text'} and 'font_min' in layer
            and layer.get('writing_mode', 'horizontal') == 'horizontal'
            and layer.get('text_animation', 'none') == 'none')


def normalize_wrap(layer):
    for key, value in DEFAULTS.items():
        layer.setdefault(key, copy.deepcopy(value))
    if type(layer['wrap_enabled']) is not bool:
        raise ValueError('文字环绕开关必须为true或false。')
    targets = layer['wrap_targets']
    if (not isinstance(targets, list) or len(targets) > 79
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in targets)
            or len(set(targets)) != len(targets) or layer.get('id') in targets):
        raise ValueError('文字避让对象编号无效、重复或包含自身。')
    gap = layer['wrap_gap']
    if type(gap) not in (int, float) or not math.isfinite(gap) or not 0 <= gap <= 300:
        raise ValueError('文字避让间距须为0–300像素，按1080宽画布计算。')
    if layer['wrap_side'] not in {value for _, value in SIDES}:
        raise ValueError('文字环绕方向无效。')
    from .polygon_wrap import normalize_polygons
    normalize_polygons(layer['wrap_polygons'])
    if layer['wrap_enabled'] and not supported(layer):
        raise ValueError('文字环绕仅支持普通横排文字，请先关闭环绕再启用竖排或滚动。')


def targetable(layer):
    return (layer.get('kind') in {'image', 'title', 'body', 'text', 'caption'}
            and all(key in layer for key in ('x', 'y', 'width', 'height')))


def bind_exclusions(layers, context=None):
    """Attach geometry only, including targets in other native composition runs.

    No source decode, pool advancement, file probing, or persistent model writes.
    Deleted targets remain referenced so undo/re-adding the object restores them.
    """
    by_id = {item['id']: item for item in (context if context is not None else layers)}
    for layer in layers:
        layer.pop('_wrap_rects', None)
        if not layer.get('wrap_enabled'):
            continue
        normalize_wrap(layer)
        rectangles = []
        for identifier in layer['wrap_targets']:
            item = by_id.get(identifier)
            if not item or not targetable(item) or not item.get('enabled', True) or not item.get('opacity', 100):
                continue
            rectangles.append((item['x']-item['width']/2, item['y']-item['height']/2,
                               item['width'], item['height']))
        layer['_wrap_rects'] = rectangles
    return layers


def exclusions(layer, width, height, extra=0.):
    gap = layer.get('wrap_gap', 12)*width/1080 + extra
    rectangles = [QtCore.QRectF(x*width/100, y*height/100, w*width/100, h*height/100)
            .adjusted(-gap, -gap, gap, gap) for x, y, w, h in layer.get('_wrap_rects', ())]
    from .polygon_wrap import polygon_exclusions
    return rectangles + polygon_exclusions(layer.get('wrap_polygons', []), width, height, gap)


def intervals(width, y, height, rectangles, side='both'):
    from .polygon_wrap import PolygonExclusion
    blocks = []
    for rect in rectangles:
        if rect.top() < y+height and rect.bottom() > y and rect.right() > 0 and rect.left() < width:
            spans = rect.band_intervals(y, height) if isinstance(rect, PolygonExclusion) else [(rect.left(), rect.right())]
            blocks.extend((max(0., left), min(width, right)) for left, right in spans if right > 0 and left < width)
    blocks.sort()
    result, left = [], 0.
    for start, end in blocks:
        if start > left:
            result.append((left, start-left))
        left = max(left, end)
    if left < width:
        result.append((left, width-left))
    if side == 'largest':
        return [max(result, key=lambda span: span[1])] if result else []
    if side == 'left':
        return result[:1] if result and result[0][0] == 0 else []
    if side == 'right':
        return result[-1:] if result and abs(sum(result[-1])-width) < 1e-6 else []
    return result


def _rtl(text):
    for char in text:
        direction = unicodedata.bidirectional(char)
        if direction in {'R', 'AL', 'L'}:
            return direction in {'R', 'AL'}
    return False


def _flow(text, font, box, rectangles, layer, scale, start, line_height):
    """QTextLine owns UTF-16 consumption, shaping and word breaking."""
    groups, y = [], start
    # Qt positions are quantized to 1/64 px and italic/fallback glyphs can
    # overhang the advance width. Leave a real ink guard on all four edges.
    guard = max(1., scale)
    band = line_height+2*guard
    stride = max(line_height*layer['line_spacing'], band)
    for index, paragraph in enumerate(str(text).replace('\r\n', '\n').split('\n')):
        if index:
            y += layer.get('paragraph_spacing', 0)*scale
        if not paragraph:
            y += line_height*layer['line_spacing']*.65
            continue
        native = QtGui.QTextLayout(paragraph, font)
        option = QtGui.QTextOption()
        option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        option.setAlignment({'left': QtCore.Qt.AlignmentFlag.AlignLeft,
                             'center': QtCore.Qt.AlignmentFlag.AlignHCenter,
                             'right': QtCore.Qt.AlignmentFlag.AlignRight}[layer['align']])
        native.setTextOption(option)
        rtl = _rtl(paragraph)
        groups.append((native, 0.))
        native.beginLayout()
        try:
            line = native.createLine()
            while line.isValid():
                if y+band > box.height()+1e-6:
                    return groups, math.inf
                spans = intervals(box.width(), y, band, rectangles, layer['wrap_side'])
                if rtl:
                    spans.reverse()
                for x, width in spans:
                    # Never overflow a narrow sliver, including a single wide glyph.
                    if width <= 2*guard:
                        continue
                    line.setLineWidth(width-2*guard)
                    line.setPosition(QtCore.QPointF(x+guard, y+guard))
                    ink = line.naturalTextRect()
                    if (line.naturalTextWidth() > width-2*guard+1e-6
                            or ink.left() < x or ink.right() > x+width
                            or ink.top() < y or ink.bottom() > y+band):
                        continue
                    line = native.createLine()
                    if not line.isValid():
                        break
                if not spans:
                    bottoms = [rect.bottom() for rect in rectangles
                               if rect.top() < y+band and rect.bottom() > y]
                    # A sloping/concave contour may open again on the next row.
                    from .polygon_wrap import PolygonExclusion
                    curved = any(isinstance(rect, PolygonExclusion) and rect.top() < y+band and rect.bottom() > y
                                 for rect in rectangles)
                    y = max(y+stride, min(bottoms)) if bottoms and not curved else y+stride
                else:
                    y += stride
        finally:
            native.endLayout()
    return groups, y-start


def flow_layout(layer, size, box, rectangles, scale):
    from .layout import _text_font, _text_layout
    font = _text_font(layer['font'], size, layer, scale)
    # Inspect fallback-font metrics for the entire content before assigning bands.
    measured, _ = _text_layout(layer['text'], layer['font'], size, box.width(),
                               layer['align'], layer['line_spacing'], layer, scale)
    line_height = max([QtGui.QFontMetricsF(font).height()] + [native.lineAt(i).height()
        for native, _ in measured for i in range(native.lineCount())])
    local = [rect.translated(-box.left(), -box.top()) for rect in rectangles]
    anchor = {'top': 0., 'center': .5, 'bottom': 1.}[layer['vertical']]
    # Reflow at the actual aligned Y, never translate an already-shaped paragraph
    # through an obstacle. Bounded iteration handles center/bottom discontinuities.
    start, candidates, seen = 0., [], set()
    for _ in range(8 if anchor else 1):
        key = round(start, 4)
        if key in seen:
            break
        seen.add(key)
        groups, total = _flow(layer['text'], font, box, local, layer, scale, start, line_height)
        if math.isfinite(total) and start+total <= box.height()+1e-6:
            desired = max(0., (box.height()-total)*anchor)
            candidates.append((abs(start-desired), groups, total, start))
            start = desired
        else:
            break
    if not candidates:
        return [], math.inf, 0.
    _, groups, total, start = min(candidates, key=lambda item: item[0])
    return groups, total, start
