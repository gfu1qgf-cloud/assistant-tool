"""Universal caption appearance; independent switches and deterministic group colours."""
import copy
import hashlib
import json

from qt_compat import QtCore, QtGui

PALETTE = ['#5de6ff','#ff83b5','#b6a5ff','#79efbf','#ffb6aa','#f6f8ff']
DEFAULTS = {'caption_group_colors':False, 'caption_group_palette':PALETTE, 'caption_color_seed':0}
COMMON_EFFECT_KEYS = {
    'karaoke_highlight','color_high','high_gradient_direction','dynamic_box','color_high_bg','opacity_high_bg',
    'dynamic_radius','dynamic_box_stroke','dynamic_box_stroke_width','high_padding','high_offset_y',
    'use_box','opacity_bg','box_padding_x','box_padding_y','box_radius','box_blur','box_adaptive_width','box_follow_motion',
    'bg_gradient_enabled','bg_gradient_colors','bg_gradient_type','shadow_blur',
    'bg_gradient_highlight','text_gradient_enabled','color_text','text_gradient_direction',
}


def normalize_appearance(layer):
    from .caption_styles import effects, is_catalog, colors
    layer.setdefault('caption_effects',{})
    if layer.get('caption_appearance_version') != 2:
        style = effects(layer, include_common=False)
        if is_catalog(layer):
            # In the old renderer use_box/opacity_bg ignored the visible font
            # controls. Transfer the actual rendered values once, then give
            # the ordinary controls sole ownership.
            layer.update(background_enabled=style['use_box'], background_opacity=style['opacity_bg']/255*100,
                         background_radius=style['box_radius'])
        if 'opacity_high_bg' in layer['caption_effects']:
            layer['caption_effects']={**layer['caption_effects'],'opacity_high_bg':style['opacity_high_bg']}
        layer['caption_appearance_version']=2
    for key,value in DEFAULTS.items(): layer.setdefault(key,copy.deepcopy(value))
    if type(layer['caption_group_colors']) is not bool:
        raise ValueError('按字幕组换色的开关无效。')
    palette=colors(layer['caption_group_palette'])
    if layer['caption_group_colors'] and len(set(palette)) < 2:
        raise ValueError('按组换色请至少选择两种不同颜色。')
    layer['caption_group_palette']=palette
    if type(layer['caption_color_seed']) is not int or not 0 <= layer['caption_color_seed'] <= 2147483647:
        raise ValueError('字幕配色种子须为0–2147483647的整数。')


def apply_common(layer, style):
    if layer.get('caption_appearance_version') == 2:
        style.update(use_box=bool(layer.get('background_enabled',False)),
            opacity_bg=layer.get('background_opacity',35)/100*255,
            box_radius=layer.get('background_radius',12))
    return style


def group_colors(layer, count, words):
    if not layer.get('caption_group_colors'):
        return [layer['color']]*count
    from .caption_styles import colors
    palette=list(dict.fromkeys(colors(layer['caption_group_palette'])))
    # Design preview deliberately contains just the first group. Do not hash
    # the full word list here, or its first colour would differ from export.
    seed=json.dumps([layer.get('caption_color_seed',0),layer.get('caption_seed',1)],ensure_ascii=False).encode('utf8')
    offset=int.from_bytes(hashlib.sha256(seed).digest()[:4],'big')%len(palette)
    return [palette[(offset+group)%len(palette)] for group in range(count)]


def active_segments(start, stop, event, base_color, style, *, context=False):
    if not style['karaoke_highlight'] or context:
        return [(start,stop,base_color)]
    a,b=max(start,event['start']),min(stop,event['spoken_end'])
    return [(start,a,base_color),(a,b,style['color_high']),(b,stop,base_color)]


def add_highlight_box(add, event, style, scale, options):
    if not style['dynamic_box'] or event.get('context'):
        return
    pad=style['high_padding']*scale
    path=QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0,0,event['width']+pad*2,event['height']+pad*2),
        style['dynamic_radius']*scale,style['dynamic_radius']*scale)
    if style['dynamic_box_stroke']:
        stroker=QtGui.QPainterPathStroker(); stroker.setWidth(style['dynamic_box_stroke_width']*scale)
        path=stroker.createStroke(path)
    # A current-word box lives only for the spoken word, not the whole held
    # group. Inheriting the text's fade could keep it translucent until it
    # disappeared. Only its explicit advanced bounce is inherited.
    options={**options,'motion':'none','motion_ms':0,'out':'none','out_ms':0}
    if style['dyn_box_anim']:
        options.update(motion='word_pop_random_pulse',pulse=style['dyn_box_anim_overshoot'],
            motion_ms=min(style['dyn_box_anim_duration']*1000,(event['spoken_end']-event['start'])*1000))
    add(path,event['x']-pad,event['y']-pad+style['high_offset_y']*scale,
        event['start'],min(event['spoken_end'],event['end']),style['color_high_bg'],
        style['opacity_high_bg']/255,z=-3,**options)


def add_foreground(add, path, x, y, start, stop, color, opacity, layer, scale, options):
    border=layer['outline_width']*scale
    blur=layer.get('outline_blur',0)*scale
    if border and blur:
        stroker=QtGui.QPainterPathStroker(); stroker.setWidth(border*2)
        ring=stroker.createStroke(path)
        add(ring,x,y,start,stop,layer['outline_color'],opacity*layer['outline_opacity']/100,
            z=-1,blur=blur,origin_x=path.boundingRect().width()/2,origin_y=path.boundingRect().height()/2,**options)
        border=0
    add(path,x,y,start,stop,color,opacity,border=border,border_color=layer['outline_color'],**options)
