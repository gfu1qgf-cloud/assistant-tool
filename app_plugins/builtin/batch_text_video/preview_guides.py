"""Canvas-only alignment math; never modifies render settings or source objects."""
GEOMETRY_KEYS = ('x','y','width','height')


def targets(layers, selected_id, axis):
    extent = 'width' if axis == 'x' else 'height'
    result = [(50.0,'画布中心'),(0.0,'画布边缘'),(100.0,'画布边缘')]
    for layer in layers:
        if layer.get('id') == selected_id or not layer.get('enabled') or layer.get('opacity',100) <= 0: continue
        if not all(key in layer for key in GEOMETRY_KEYS): continue
        result.extend((layer[axis] + factor*layer[extent], layer.get('name','控件')) for factor in (-.5,0,.5))
    return result


def snapped_geometry(values, layers, selected_id, canvas_pixels, *, handle=None, tolerance=6, max_size=100):
    result, guides = dict(values), []
    for axis, extent, pixels, direction in (
            ('x','width',canvas_pixels[0],handle[0] if handle else None),
            ('y','height',canvas_pixels[1],handle[1] if handle else None)):
        if pixels <= 0 or direction == 0: continue
        candidates = [(result[axis]+factor*result[extent], factor) for factor in (-.5,0,.5)] if handle is None else [
            (result[axis]+direction*result[extent]/2,direction/2)]
        matches = [(abs(target-current), order, target-current, target, label) for order,(target,label) in
            enumerate(targets(layers,selected_id,axis)) for current,_factor in candidates if 0 <= target <= 100]
        if not matches: continue
        distance, _order, delta, target, label = min(matches,key=lambda m:(m[0],m[1]))
        if distance * pixels / 100 > tolerance: continue
        position = result[axis] + (delta if handle is None else delta/2)
        size = result[extent] if handle is None else result[extent] + direction*delta
        if not 0 <= position <= 100 or not 1 <= size <= max_size: continue
        result[axis], result[extent] = position, size
        guides.append((axis,target,label))
    return result,guides


def aligned_geometry(layer, horizontal=None, vertical=None):
    result = {key:layer[key] for key in GEOMETRY_KEYS}
    for axis, extent, side in (('x','width',horizontal),('y','height',vertical)):
        if side is None: continue
        result[axis] = 50 if side == 'center' else result[extent]/2 if side in {'left','top'} else 100-result[extent]/2
        if not 0 <= result[axis] <= 100: return None
    return result
