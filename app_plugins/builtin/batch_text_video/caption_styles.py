"""Versioned preset adapter. Applying a style never changes timing or task data."""
import copy
import math
from qt_compat import QtGui
from .caption_style_catalog import PRESETS, STYLE_DEFAULTS as CATALOG_DEFAULTS
from .caption_custom_styles import PRESETS as CUSTOM_PRESETS, STYLE_DEFAULTS as CUSTOM_DEFAULTS

STYLE_DEFAULTS = {**CATALOG_DEFAULTS, **CUSTOM_DEFAULTS}

PREFIX = 'videokit:'
ANIMATIONS = ('none', 'fade', 'pop', 'slide_up', 'slide_down', 'slide_left', 'slide_right',
              'typewriter', 'char_bounce', 'letter_jump', 'word_pop_random',
              'word_pop_random_pulse', 'metronome', 'blur_sharp', 'holy_glow',
              'floating', 'bullet_reveal', 'flash_highlight', 'scatter_pop', 'word_random_position', 'lzx_word_pop')
LAYER_KEYS = ('font', 'font_min', 'font_max', 'font_weight', 'italic', 'capitalization',
              'letter_spacing', 'word_spacing', 'line_spacing', 'color', 'outline_width',
              'outline_color', 'outline_opacity', 'outline_blur', 'shadow_enabled', 'shadow_color',
              'shadow_opacity', 'shadow_x', 'shadow_y', 'shadow_blur', 'background_enabled',
              'background_color', 'background_opacity', 'background_radius', 'padding',
              'x', 'y', 'width', 'height', 'align', 'vertical')
COLOR_KEYS = {key for key in STYLE_DEFAULTS if 'color' in key and key not in
              {'box_brush_color_mode', 'fullpage_typewriter_first_line_color'}}


def is_catalog(layer):
    key = str(layer.get('caption_preset', ''))
    return key.startswith(PREFIX) or key in CUSTOM_PRESETS


def preset_choices():
    return [(value['label'],key) for key,value in CUSTOM_PRESETS.items()] + [('VideoKit · '+name, PREFIX+name) for name in PRESETS]


def preset_values(layer):
    key = str(layer.get('caption_preset',''))
    if key in {'pop','appear'}:
        return {'anim_in_type':'none','anim_out_type':'none','use_box':False,
                'color_high':'#5de6ff','color_high_bg':'#6654d9','opacity_high_bg':255}
    if key in CUSTOM_PRESETS: return CUSTOM_PRESETS[key]['style']
    name = key[len(PREFIX):] if key.startswith(PREFIX) else key
    if name not in PRESETS: raise ValueError('字幕预设不存在：'+name)
    return PRESETS[name]


def colors(value):
    result = value if isinstance(value, list) else str(value).split(',')
    if not 1 <= len(result) <= 8 or any(not QtGui.QColor(str(color).strip()).isValid() for color in result):
        raise ValueError('字幕效果颜色无效，请使用 #RRGGBB；渐变颜色用英文逗号分隔。')
    return [QtGui.QColor(str(color).strip()).name(QtGui.QColor.NameFormat.HexArgb)
            if QtGui.QColor(str(color).strip()).alpha() != 255 else QtGui.QColor(str(color).strip()).name()
            for color in result]


def effects(layer, *, include_common=True):
    overrides = layer.get('caption_effects', {})
    if not isinstance(overrides, dict) or set(overrides)-set(STYLE_DEFAULTS):
        raise ValueError('字幕效果参数格式无效。')
    style = {**copy.deepcopy(STYLE_DEFAULTS), **copy.deepcopy(preset_values(layer)), **copy.deepcopy(overrides)}
    if (not ('opacity_high_bg' in overrides and layer.get('caption_appearance_version') == 2)
            and isinstance(style['opacity_high_bg'],(int,float)) and not isinstance(style['opacity_high_bg'],bool)
            and 0 <= style['opacity_high_bg'] <= 1):
        style['opacity_high_bg']*=255
    if include_common:
        from .caption_appearance import apply_common
        apply_common(layer,style)
    for key, value in style.items():
        default = STYLE_DEFAULTS[key]
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError('字幕效果开关无效：'+key)
        elif isinstance(default, (int, float)):
            if not isinstance(value,(int,float)) or isinstance(value, bool) or not math.isfinite(value) or abs(value) > 10000:
                raise ValueError('字幕效果数值无效：'+key)
        elif isinstance(value, list):
            if len(value) > 8 or any(not isinstance(item, (str, int, float)) for item in value):
                raise ValueError('字幕效果列表过长或无效：'+key)
        elif not isinstance(value, str) or len(value) > 500:
            raise ValueError('字幕效果内容无效：'+key)
        if key in COLOR_KEYS:
            colors(value)
    if style['anim_in_type'] not in ANIMATIONS or style['anim_out_type'] not in ANIMATIONS:
        raise ValueError('字幕动画类型无效。')
    if not 1 <= style['lzx_pop_scale'] <= 2:
        raise ValueError('柔弹峰值应为1–2倍。')
    for key in ('anim_in_duration', 'anim_out_duration', 'word_pop_random_duration',
                'word_pop_random_pulse_duration', 'letter_jump_duration', 'dyn_box_anim_duration','flash_duration'):
        if not 0 <= style[key] <= 5:
            raise ValueError('字幕动画时长应为0–5秒：'+key)
    for key in ('word_pop_random_min_scale', 'word_pop_random_max_scale',
                'word_pop_random_pulse_min_scale', 'word_pop_random_pulse_max_scale',
                'scatter_min_scale', 'scatter_max_scale', 'letter_jump_scale', 'dyn_box_anim_overshoot'):
        if not .1 <= style[key] <= 3:
            raise ValueError('字幕缩放应为0.1–3倍：'+key)
    for key in ('floating_period', 'holy_glow_period'):
        if not .1 <= style[key] <= 30:
            raise ValueError('字幕周期应为0.1–30秒：'+key)
    if not 1 <= style['scatter_max_words'] <= 12 or not 1 <= style['scrolling_visible_lines'] <= 5:
        raise ValueError('随机词数或歌词上下文行数无效。')
    for key,low,high in (('metronome_bpm',1,300),('speed_trail_layers',1,8),('stroke_expand_layers',1,6),
                         ('scatter_max_words',1,12),('scrolling_visible_lines',1,5)):
        if not low <= style[key] <= high or key!='metronome_bpm' and style[key]!=int(style[key]):
            raise ValueError('字幕节拍／层数无效：'+key)
    for key in ('opacity_bg','opacity_outline','opacity_shadow','opacity_high_bg','speed_trail_opacity'):
        if not 0 <= style[key] <= 255:
            raise ValueError('字幕透明度应为0–255：'+key)
    for key in ('scrolling_opacity_context','scatter_accum_prob','ambient_glow_opacity',
                'ambient_dark_opacity','global_mask_opacity'):
        if not 0 <= style[key] <= 1:
            raise ValueError('字幕比例应为0–1：'+key)
    for lo,hi in (('scatter_min_scale','scatter_max_scale'),('word_pop_random_min_scale','word_pop_random_max_scale'),
                  ('word_pop_random_pulse_min_scale','word_pop_random_pulse_max_scale')):
        if style[lo]>style[hi]:
            raise ValueError('字幕最小缩放不能超过最大缩放。')
    for key in ('box_padding_x', 'box_padding_y', 'high_padding', 'box_radius', 'dynamic_radius',
                'box_blur', 'shadow_blur', 'floating_amplitude', 'char_bounce_height', 'holy_glow_radius'):
        if not 0 <= style[key] <= 150:
            raise ValueError('字幕效果大小应为0–150：'+key)
    if not -150 <= style['high_offset_y'] <= 150 or not 0 <= style['dynamic_box_stroke_width'] <= 30:
        raise ValueError('当前词高亮框的偏移或边线宽度无效。')
    return style


def apply_style(layer, preset):
    result = copy.deepcopy(layer)
    result['caption_preset'] = preset
    result['caption_effects'] = {}  # Reset previous style's effects, not timing/source.
    result['caption_random_size'] = False
    if not is_catalog(result):
        return result
    style = effects(result,include_common=False)
    requested = style['font_family']
    families = {name.casefold(): name for name in QtGui.QFontDatabase.families()}
    fallback = {'arimo': 'Arial', 'anton': 'Impact'}.get(requested.casefold(), 'Poppins')
    font = families.get(requested.casefold(), families.get(fallback.casefold(), 'Segoe UI'))
    size = min(200, max(8, style['fontsize']))
    result.update(font=font, font_min=max(8, size*.65), font_max=size,
        font_weight=int(style['font_weight'] if style['bold'] else 400), italic=style['italic'],
        capitalization={'none':'normal'}.get(style['text_transform'],style['text_transform']),
        letter_spacing=style['letter_spacing'], word_spacing=style['word_spacing'] or 20,
        line_spacing=min(3,max(.8,style['line_spacing'])), color=colors(style['color_text'])[0],
        outline_width=style['border_width'] if style['use_stroke'] else 0,
        outline_color=colors(style['color_outline'])[0], outline_opacity=style['opacity_outline']/255*100,
        shadow_enabled=bool(style['shadow_blur'] or style['shadow_offset_x'] or style['shadow_offset_y']),
        shadow_color=colors(style['color_shadow'])[0], shadow_opacity=style['opacity_shadow']/255*100,
        shadow_x=style['shadow_offset_x'], shadow_y=style['shadow_offset_y'], shadow_blur=style['shadow_blur'],
        background_enabled=style['use_box'], background_color=colors(style['color_bg'])[0],
        background_opacity=style['opacity_bg']/255*100, background_radius=style['box_radius'],
        padding=max(4,style['box_padding_y']), x=50,y=style['pos_y']*100,
        width=min(100,max(1,style['wrap_width_percent'])),height=32,
        align='center',vertical='center',caption_words=8,caption_per_line=4)
    if style['scrolling_mode']:
        result.update(height=65)
    if style['fullpage_typewriter']:
        result.update(y=50,height=84,caption_words=32,caption_per_line=6,font_min=12)
    if style['only_show_active_word'] or style['anim_in_type'] == 'word_random_position':
        result.update(caption_words=1,caption_per_line=1)
    if style['block_typography_enabled']:
        result.update(height=60,caption_words=6,caption_per_line=1,line_spacing=1.2)
    if preset in CUSTOM_PRESETS:
        result.update(copy.deepcopy(CUSTOM_PRESETS[preset]['layer']))
    result['caption_appearance_version']=2
    return result


def description(layer):
    if not is_catalog(layer):
        return '原有逐词字幕；时间戳接口与设置保持不变。'
    style = effects(layer)
    custom = CUSTOM_PRESETS.get(layer['caption_preset'])
    pieces = [custom['label'] if custom else str(layer['caption_preset'])[len(PREFIX):]]
    if custom:
        pieces.append('按原词时间逐词柔弹；五段缩放曲线，峰值可在效果参数调整；不压缩人声时间')
    if style['karaoke_highlight']:
        pieces.append('读到的词高亮；本组未读文字可见')
    if style['dynamic_box']:
        pieces.append('跟随当前词的高亮框')
    if style['scrolling_mode']:
        pieces.append('滚动歌词，前后文淡显')
    if style['fullpage_typewriter']:
        pieces.append('整页逐字＋光标；长文放不下会提示，不会删字')
    if layer.get('font') != style['font_family']:
        pieces.append('本机未装 '+style['font_family']+'，使用 '+str(layer.get('font'))+'（可在样式中更换）')
    pieces.append('位置、字号和描边可在“样式／位置”调整；播放预览显示真实动画')
    return '；'.join(pieces)+'。'
