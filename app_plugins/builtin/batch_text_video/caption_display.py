"""Caption reveal/layout parameters, independent of typography and animation."""
import copy
import math

DISPLAY_CHOICES = (('整组显示', 'group'), ('逐词出现', 'word'), ('逐字出现', 'char'), ('逐行出现', 'line'))
DEFAULTS = {
    'caption_display_mode': 'word', 'caption_accumulate': False,
    'caption_first_line_emphasis': False, 'caption_first_line_bold': True,
    'caption_first_line_scale': 1.2, 'caption_cursor': False,
    'caption_cursor_color': '#FFD700',
}
from .caption_follow import DEFAULTS as FOLLOW_DEFAULTS
DEFAULTS.update(FOLLOW_DEFAULTS)
WORD_ANIMATIONS = {'word_pop_random', 'word_pop_random_pulse', 'word_random_position',
                   'scatter_pop', 'char_bounce', 'letter_jump', 'metronome', 'lzx_word_pop'}


def preset_defaults(layer):
    """One adapter for old presets; renderers consume only the explicit fields."""
    from .caption_styles import raw_effect_parameters
    overrides=layer.get('caption_effects',{})
    if not isinstance(overrides,dict):
        raise ValueError('字幕效果参数格式无效。')
    raw = raw_effect_parameters(layer)
    full = raw['fullpage_typewriter']
    mode = ('char' if full else
            raw['typewriter_reveal_type'] if raw['anim_in_type'] == 'typewriter' else
            'line' if raw['anim_in_type'] == 'bullet_reveal' else
            'word' if layer.get('caption_preset', 'pop') in {'pop', 'appear'} or
                raw['anim_in_type'] in WORD_ANIMATIONS or raw['ambient_glow_enabled'] else 'group')
    if full:
        mode = raw['fullpage_typewriter_reveal_type']
    return {**DEFAULTS, 'caption_display_mode': mode, 'caption_accumulate': full,
            'caption_first_line_emphasis': bool(full),
            'caption_first_line_bold': raw['fullpage_typewriter_first_line_bold'],
            'caption_first_line_scale': raw['fullpage_typewriter_first_line_scale'],
            'caption_cursor': bool(full and raw['fullpage_typewriter_cursor']),
            'caption_cursor_color': raw['fullpage_typewriter_cursor_color']}


def normalize_display(layer):
    defaults = preset_defaults(layer) if any(key not in layer for key in DEFAULTS) else DEFAULTS
    for key, value in defaults.items():
        layer.setdefault(key, copy.deepcopy(value))
    if layer['caption_display_mode'] not in {value for _title, value in DISPLAY_CHOICES}:
        raise ValueError('字幕显示方式无效。')
    from .caption_follow import normalize as normalize_follow
    normalize_follow(layer)
    for key in ('caption_accumulate', 'caption_first_line_emphasis', 'caption_first_line_bold', 'caption_cursor'):
        if type(layer[key]) is not bool:
            raise ValueError('字幕显示开关无效：' + key)
    factor = layer['caption_first_line_scale']
    if type(factor) not in (int, float) or not math.isfinite(factor) or not .5 <= factor <= 3:
        raise ValueError('首行字号倍数应为0.5–3倍。')
    from qt_compat import QtGui
    if not isinstance(layer['caption_cursor_color'], str) or not QtGui.QColor(layer['caption_cursor_color']).isValid():
        raise ValueError('打字光标颜色无效。')


def display_settings(layer):
    """Also support pure preview callers before their first normalized save."""
    return {**(preset_defaults(layer) if any(key not in layer for key in DEFAULTS) else DEFAULTS),
            **{key: layer[key] for key in DEFAULTS if key in layer}}


def layout_glyphs(words, layer, sizes, scale, fraction, *, available_width, per_line,
                  gap_for, peak=1, edge_peak=1, native=False):
    """Measure the actual first visual row with its own emphasis.

    Lock that row before laying out normal-sized following rows; shrinking a
    wrapped word must not pull it back into the emphasized row and oscillate.
    """
    from .captions import _glyph
    from .word_spacing import glyph_rows
    options = display_settings(layer)
    emphasize = options['caption_first_line_emphasis']

    def glyph(index, first=False):
        actual = layer
        size = (layer['font_min'] + fraction*(sizes[index]-layer['font_min']))*scale
        if first:
            size *= options['caption_first_line_scale']
            if options['caption_first_line_bold']:
                actual = {**layer, 'font_weight': 900}
        value = _glyph(words[index]['text'], actual, size, native=native)
        if '_space_advance' not in value:
            from .word_spacing import natural_space
            value['_space_advance'] = natural_space(actual,size)
        return {**value, '_first_line_emphasis': first,
                '_font_pixel_size': max(1, round(size)), '_font_weight': actual['font_weight']}

    glyphs = [glyph(i, emphasize and i < per_line) for i in range(len(words))]
    gap = gap_for(glyphs)
    lines = glyph_rows(glyphs, per_line, available_width, gap, peak, edge_peak)
    if emphasize:
        count = len(lines[0])
        for i in range(count, min(per_line, len(words))):
            glyphs[i] = glyph(i)
        gap = gap_for(glyphs)
        rest = glyph_rows(glyphs[count:], per_line, available_width, gap, peak, edge_peak)
        lines = [list(range(count))] + [[i+count for i in row] for row in rest]
    return glyphs, lines, gap


def reveal_events(events, layer):
    """Split grapheme paths only, never the stored words or audio timestamps."""
    from qt_compat import QtCore, QtGui
    mode = display_settings(layer)['caption_display_mode']
    line_starts={}
    for event in events:
        key=(event['group'],event['line'])
        line_starts[key]=min(line_starts.get(key,event['start']),event['start'])
    windows = {}
    if mode == 'char':
        # Sentence SRT assigns its whole interval to every word. Those are
        # shared cue bounds, not simultaneous word alignments: type one prefix
        # through the cue instead of restarting a typewriter at every word.
        # This is a visual schedule only. Keep the source events unchanged.
        shared = {}
        for index, event in enumerate(events):
            if event.get('context'): continue
            key = (event['group'], event.get('cue'), event['start'], event['spoken_end'])
            shared.setdefault(key, []).append(index)
        for indices in shared.values():
            if len(indices) < 2: continue
            counts = [len(events[index]['_cluster_edges'])-1 for index in indices]
            total = sum(counts) + len(indices)-1  # include the spaces between words
            cursor = 0
            for index, count in zip(indices, counts):
                event = events[index]
                span = event['spoken_end']-event['start']
                windows[index] = (event['start']+span*cursor/total,
                    event['start']+span*(cursor+count)/total)
                cursor += count+1
    values = []
    for event_index, event in enumerate(events):
        event={**event,'_line_start':line_starts[event['group'],event['line']]}
        if mode != 'char' or event.get('context'):
            values.append(event)
            continue
        edges = event['_cluster_edges']
        count = len(edges)-1
        begin, end = windows.get(event_index, (event['start'],event['spoken_end']))
        for index, (left, right) in enumerate(zip(edges, edges[1:])):
            start = begin+(end-begin)*index/count
            until = begin+(end-begin)*(index+1)/count
            clip = QtGui.QPainterPath()
            clip.addRect(QtCore.QRectF(left, 0, right-left+.5, event['height']+2))
            values.append({**event, 'start': start, 'spoken_end': end,
                           '_native_path': event['_native_path'].intersected(clip),
                           '_character_piece': True, '_character_index':index,
                           '_cursor_edge': right, '_cursor_until': until})
    return values


def reveal_start(event, layer, events):
    if event.get('context'):
        return event['group_start']
    mode = display_settings(layer)['caption_display_mode']
    if mode in {'word', 'char'}:
        return event['start']
    if mode == 'line':
        if '_line_start' in event:
            return event['_line_start']
        return min(item['start'] for item in events
                   if item['group'] == event['group'] and item['line'] == event['line'])
    return event['group_start']
