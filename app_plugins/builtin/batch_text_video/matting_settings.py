"""Material-owned matte refinements, independent of the AI backend and UI."""
import copy
import math

DEFAULTS = dict(model='isnet', quality='better', device='auto', black_clip=0., white_clip=100.,
                clean_black=0., clean_white=0., denoise=0, grow=0, smooth=0., blur=0.,
                in_out=0., opacity=100., invert=False, cleanup=False, cleanup_size=2,
                despill='none', despill_amount=100., despill_width=4, despill_luma=50., strokes=[],
                fill_holes=False, hole_threshold=50., screen_cleanup=True, despill_softness=0.)


def normalize(values=None):
    if values is not None and not isinstance(values, dict): raise ValueError('抠像参数必须是对象。')
    values = copy.deepcopy(values or {})
    unknown = set(values)-set(DEFAULTS)
    if unknown: raise ValueError('未知抠像参数：'+', '.join(sorted(unknown)))
    result = {**copy.deepcopy(DEFAULTS), **values}
    for name, choices in (('model', {'isnet', 'rvm'}), ('quality', {'fast', 'better'}),
                          ('device', {'auto', 'cpu', 'cuda'}), ('despill', {'none', 'green', 'blue'})):
        if not isinstance(result[name], str) or result[name] not in choices: raise ValueError('抠像选项无效：'+name)
    for name in ('invert', 'cleanup', 'fill_holes', 'screen_cleanup'):
        if type(result[name]) is not bool: raise ValueError('抠像开关无效：'+name)
    ranges = dict(black_clip=(0, 99.9), white_clip=(.1, 100), clean_black=(0, 100), clean_white=(0, 100),
                  denoise=(0, 5), grow=(-20, 20), smooth=(0, 100), blur=(0, 20), in_out=(-100, 100),
                  opacity=(0, 100), cleanup_size=(1, 12), despill_amount=(0, 100),
                  despill_width=(1, 12), despill_luma=(0, 100), hole_threshold=(1, 99), despill_softness=(0, 100))
    for name, (low, high) in ranges.items():
        value = result[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('抠像参数超出范围：'+name)
        if name in {'denoise', 'cleanup_size', 'despill_width'} and type(value) is not int:
            raise ValueError('抠像参数须为整数：'+name)
    if result['white_clip']-result['black_clip'] < .1: raise ValueError('白色截断必须大于黑色截断，至少相差0.1%。')
    strokes = result['strokes']
    if not isinstance(strokes, list) or len(strokes) > 500: raise ValueError('最多保留500笔遮罩修正。')
    total = 0
    for stroke in strokes:
        if not isinstance(stroke, dict) or set(stroke) != {'mode', 'radius', 'points'}:
            raise ValueError('画笔记录无效。')
        if stroke['mode'] not in {'keep', 'remove'} or type(stroke['radius']) not in (int, float) or not 0 < stroke['radius'] <= 1:
            raise ValueError('画笔方式／尺寸无效。')
        points = stroke['points']
        if not isinstance(points, list) or not points or len(points) > 5000: raise ValueError('画笔轨迹过长或为空。')
        total += len(points)
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2 or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
                raise ValueError('画笔坐标须位于素材内。')
    if total > 50000: raise ValueError('画笔轨迹总长度过大。')
    return result


def brush_mask(shape, strokes):
    import cv2
    import numpy as np
    if not strokes: return None
    h, w = shape
    mask = np.full((h, w), 128, np.uint8)
    for stroke in strokes:
        value = 255 if stroke['mode'] == 'keep' else 0
        radius = max(1, round(stroke['radius']*w))
        points = [(min(w-1, round(x*(w-1))), min(h-1, round(y*(h-1)))) for x, y in stroke['points']]
        cv2.circle(mask, points[0], radius, value, -1)
        for before, after in zip(points, points[1:]): cv2.line(mask, before, after, value, radius*2+1)
        cv2.circle(mask, points[-1], radius, value, -1)
    return mask


def refine(rgb, alpha, source_alpha, values, *, source_rgb=None):
    """RGB8 + predicted alpha -> RGBA8; native transparent pixels never revive."""
    import cv2
    import numpy as np
    p = normalize(values)
    a = np.clip(alpha.astype(np.float32), 0, 1)
    if p['invert']: a = 1-a
    a = np.clip((a-p['black_clip']/100)/((p['white_clip']-p['black_clip'])/100), 0, 1)
    a *= 1-p['clean_black']/100*(1-a)
    a += p['clean_white']/100*a*(1-a)
    from .matting_screen import screen_removal
    screen = screen_removal(source_rgb if source_rgb is not None else rgb, a, p)
    if p['fill_holes']:
        # Fill enclosed low-confidence regions. The source alpha and explicit
        # remove strokes are applied later, so genuine user cuts stay holes.
        outside = np.pad((a < p['hole_threshold']/100).astype(np.uint8), 1, constant_values=1)
        cv2.floodFill(outside, None, (0, 0), 2, flags=4)
        holes = outside[1:-1, 1:-1] == 1
        a = np.maximum(a, holes.astype(np.float32))
        if source_rgb is not None:
            # A foreground estimator may return black where its alpha was zero.
            # Restoring opacity must also restore the real subject's RGB there.
            rgb = np.where(holes[:, :, None], source_rgb, rgb)
    if p['denoise']:
        a = cv2.medianBlur(np.rint(a*255).astype(np.uint8), p['denoise']*2+1).astype(np.float32)/255
    if p['grow']:
        from .matte_geometry import shift
        a = shift(a, p['grow'])
    if p['smooth']:
        guide = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)/255
        radius = 1+round(p['smooth']*.11)
        box = (radius*2+1, radius*2+1)
        mean_i, mean_a = cv2.boxFilter(guide, -1, box), cv2.boxFilter(a, -1, box)
        variance = cv2.boxFilter(guide*guide, -1, box)-mean_i*mean_i
        covariance = cv2.boxFilter(guide*a, -1, box)-mean_i*mean_a
        weight = covariance/(np.maximum(0, variance)+.0004)
        a = cv2.boxFilter(weight, -1, box)*guide+cv2.boxFilter(mean_a-weight*mean_i, -1, box)
        a = np.clip(a, 0, 1)
    if p['blur']:
        blurred = cv2.GaussianBlur(a, (0, 0), max(.05, p['blur']), borderType=cv2.BORDER_REPLICATE)
        ratio = p['in_out']/100
        side = np.maximum(a, blurred) if ratio >= 0 else np.minimum(a, blurred)
        a = blurred*(1-abs(ratio))+side*abs(ratio)
    # Apply after hole filling/grow/feather, so these cannot close genuine screen
    # gaps again. Original alpha and intentional brush edits still win below.
    if screen is not None: a *= 1-screen
    painted = brush_mask(a.shape, p['strokes'])
    if painted is not None:
        a = np.where(painted == 255, 1, np.where(painted == 0, 0, a))
    a8 = np.rint(np.clip(a, 0, 1)*source_alpha).astype(np.uint8)
    if p['cleanup']:
        from .chroma_cleanup import clean_alpha
        a8 = clean_alpha(a8, dict(key_cleanup=True, key_cleanup_size=p['cleanup_size']),
                         (painted == 255).astype(np.uint8) if painted is not None else None)
    data = np.dstack([rgb, a8]).astype(np.uint8)
    if p['despill'] != 'none' and p['despill_amount']:
        from .chroma_despill import correct_rgb
        layer = dict(key_mode=p['despill'], key_despill_amount=p['despill_amount'],
                     key_despill_width=p['despill_width'], key_despill_luma=p['despill_luma'],
                     key_despill_softness=p['despill_softness'])
        protection = (painted == 255).astype(np.float32) if painted is not None else np.zeros(a.shape, np.float32)
        correct_rgb(data, layer, protection)
    data[:, :, 3] = np.rint(data[:, :, 3].astype(np.float32)*(float(p['opacity'])/100)).astype(np.uint8)
    return data
