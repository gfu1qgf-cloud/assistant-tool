"""Timestamp-driven viewport tracking shared by Qt preview and ASS export.

This is a caption layout mode, not another timing source. No inference, network,
subtitle rewrite or per-frame Python video generation takes place here.
"""
from bisect import bisect_right
import math

CHOICES = (('不跟随（原有排版）', 'none'), ('朗读跟随 · 当前词居中', 'word'),
           ('朗读跟随 · 当前行居中', 'line'))
DEFAULTS = {'caption_follow_mode': 'none', 'caption_focus_x': 50., 'caption_focus_y': 50.,
            'caption_follow_ms': 180, 'caption_context_opacity': 40.,
            'caption_active_opacity': 100., 'caption_read_opacity': 100.,
            'caption_clip_feather': 0.}


def enabled(layer):
    return layer.get('caption_follow_mode', 'none') != 'none'


def requires_word_times(layer):
    return layer.get('caption_follow_mode', 'none') == 'word'


def has_word_times(words):
    return bool(words) and all(b['start'] > a['start'] for a, b in zip(words, words[1:]))


def check_word_times(words, layer):
    if requires_word_times(layer) and not has_word_times(words):
        raise ValueError('逐词聚焦需要独立的逐词时间戳；当前有多个词共用起点。请使用动态逐词 SRT、ELE／本地逐词对齐，或改用当前行跟随；未猜测词时间。')


def normalize(layer):
    for key, value in DEFAULTS.items(): layer.setdefault(key, value)
    if layer['caption_follow_mode'] not in {value for _, value in CHOICES}:
        raise ValueError('朗读跟随方式无效。')
    for key in ('caption_focus_x', 'caption_focus_y', 'caption_context_opacity',
                'caption_active_opacity', 'caption_read_opacity'):
        value = layer[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('朗读跟随位置／透明度应为 0–100%。')
    if type(layer['caption_follow_ms']) is not int or not 0 <= layer['caption_follow_ms'] <= 2000:
        raise ValueError('朗读跟随平滑时间应为 0–2000 毫秒。')
    value = layer['caption_clip_feather']
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 50:
        raise ValueError('裁切边缘柔化应为0–50%。')


def track_for(events, layer, box, duration):
    """Reach each target at its authoritative start; hold through long pauses.

    A short continuous pan happens just BEFORE the next word. Its duration
    never exceeds half the preceding start interval. No spoken times change.
    """
    mode = layer.get('caption_follow_mode', 'none')
    check_word_times(events, layer)
    focus_x = box.left() + box.width()*layer.get('caption_focus_x', 50)/100
    focus_y = box.top() + box.height()*layer.get('caption_focus_y', 50)/100
    simultaneous = {}
    for event in events:
        center = event.get('_follow_line_center', event['y']+event['height']/2)
        low, high = simultaneous.get(event['start'], (center, center))
        simultaneous[event['start']] = min(low, center), max(high, center)
    anchors = []
    for event in events:
        center_y = event.get('_follow_line_center', event['y'] + event['height']/2)
        # Align line baselines, not different glyph ascenders / accents.
        dx = focus_x-event['x']-event['width']/2 if mode == 'word' else 0.
        target = (float(event['start']), dx, focus_y-center_y)
        if anchors and target[0] == anchors[-1][0]:
            # Sentence SRT has no intra-cue timing: centre its whole line block.
            old = anchors[-1]
            low,high = simultaneous[target[0]]
            anchors[-1] = (old[0], 0., focus_y-(low+high)/2)
        elif not anchors or target[1:] != anchors[-1][1:]:
            anchors.append(target)
    if not anchors: raise ValueError('没有可用于朗读跟随的文字。')
    points = [(0., *anchors[0][1:])]
    previous = anchors[0]
    for current in anchors[1:]:
        span = min(layer.get('caption_follow_ms', 180)/1000, (current[0]-previous[0])*.5)
        begin = current[0]-span
        if begin > points[-1][0]: points.append((begin, *previous[1:]))
        if span:
            # Let libass interpolate one native move. Sampling the trajectory
            # into many tiny events repeats every visible glyph's vector path
            # and can inflate an ordinary one-minute transcript beyond 64 MB.
            points.append(current)
        else:
            # A 0 ms setting is an explicit cut, not an unrequested long pan.
            points.append((current[0], *current[1:]))
        previous = current
    if points[-1][0] < duration: points.append((float(duration), *previous[1:]))
    return tuple(points)


def offset_at(track, time):
    index = max(0, bisect_right(track, (float(time), math.inf, math.inf))-1)
    a = track[index]
    if index == len(track)-1: return a[1:]
    b = track[index+1]
    fraction = max(0., min(1., (time-a[0])/max(1e-12, b[0]-a[0])))
    return a[1]+(b[1]-a[1])*fraction, a[2]+(b[2]-a[2])*fraction


def visible_segment(primitive, start, end):
    """Skip offscreen ASS events, rather than drawing the whole long transcript."""
    clip = primitive.get('screen_clip')
    if not clip: return True
    from .caption_style_render import motion_at
    a, b = motion_at(primitive, start), motion_at(primitive, end)
    extra = max(primitive['border'], primitive['blur']*3, 2)
    left = min(primitive['x']+a[1], primitive['x']+b[1])-extra
    top = min(primitive['y']+a[2], primitive['y']+b[2])-extra
    right = max(primitive['x']+a[1]+primitive['width']*max(a[0], 1),
                primitive['x']+b[1]+primitive['width']*max(b[0], 1))+extra
    bottom = max(primitive['y']+a[2]+primitive['height']*max(a[0], 1),
                 primitive['y']+b[2]+primitive['height']*max(b[0], 1))+extra
    return right > clip[0] and left < clip[2] and bottom > clip[1] and top < clip[3]


def new_follow_caption():
    from .captions import new_caption
    from .caption_styles import apply_style
    layer = apply_style(new_caption(), 'appear')
    layer.update(name='朗读跟随字幕', caption_follow_mode='word', caption_display_mode='group',
                 caption_per_line=4, height=44)
    layer['caption_effects'].update(karaoke_highlight=True, color_high='#5de6ff')
    return layer
