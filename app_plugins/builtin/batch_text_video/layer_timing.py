"""One signed visibility interval for every visual control, on the final job clock."""
import copy
import math

DEFAULTS = {'show_start': 0., 'show_end': 0.}


def normalize(layer):
    legacy = layer.get('visual_timing', {}) if layer.get('visual_timing_enabled') else {}
    for key, old in (('show_start', 'start'), ('show_end', 'end')):
        layer.setdefault(key, legacy.get(old, 0.))
        value = layer[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not -86400 <= value <= 86400:
            raise ValueError('起始／结束时间须为 -86400 到 86400 秒之间的有限数值。')
        layer[key] = float(value)
    start, end = layer['show_start'], layer['show_end']
    if end and ((start >= 0 and end > 0) or (start < 0 and end < 0)) and end <= start:
        raise ValueError('结束时间必须晚于起始时间；0 表示持续到片尾，负数从片尾倒算。')
    return layer


def active(layer):
    return any(layer.get(key, 0.) for key in DEFAULTS) or bool(layer.get('_show_parent'))


def bounds(layer, duration):
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError('显示时段需要有效的最终视频时长。')
    start, end = layer.get('show_start', 0.), layer.get('show_end', 0.)
    start = duration+start if start < 0 else start
    end = duration+end if end < 0 else end or duration
    start, end = min(duration, max(0., start)), min(duration, max(0., end))
    if layer.get('_show_parent'):
        parent_start, parent_end = bounds(layer['_show_parent'], duration)
        start, end = max(start, parent_start), min(end, parent_end)
    return start, max(start, end)


def visible(layer, timing):
    if timing is None or not active(layer):
        return True  # Existing untimed design callers still see a representative poster.
    start, end = bounds(layer, timing['duration'])
    return start <= timing['time'] < end


def expression(layer, duration, variable='t'):
    start, end = bounds(layer, duration)
    return f'gte({variable},{start:.9f})*lt({variable},{end:.9f})'


def window(layer, duration, *, transitions=None, clock='global'):
    from .overlay_timing import DEFAULTS as MOTION, resolve_window, ShowWindow
    start, end = bounds(layer, duration)
    if end <= start:
        return ShowWindow(start, start, 'cut', 'cut', 0., 0., 0., clock)
    values = {**copy.deepcopy(MOTION), **(transitions or {}), 'start': start, 'end': end}
    return resolve_window(values, duration, automatic_clock=clock)


def begin_gate(filters, background, label, layer, duration):
    """Keep an unmodified copy for caption/node outputs that already include the base."""
    if not active(layer):
        return background, label, False
    filters.append(f'[{background}]split=2[{label}_before][{label}_input]')
    return label+'_input', label+'_visible', True


def end_gate(filters, label, layer, duration):
    filters.append(f"[{label}_before][{label}_visible]overlay=0:0:format=auto:eof_action=pass:"
                   f"repeatlast=0:enable='{expression(layer, duration)}'[{label}]")
