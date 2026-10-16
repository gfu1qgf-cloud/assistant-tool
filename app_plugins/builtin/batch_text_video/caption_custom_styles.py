"""Application-owned presets, separate from the upstream VideoKit catalog.

LZX Word Pop adapts the user's Fusion template's five scale/opacity points to
the existing Qt/libass renderer. No Fusion callbacks or frame/time remapping
are executed: the supplied audio/word timestamps remain authoritative.
"""

LZX_WORD_POP = 'davinci:lzx_word_pop'
STYLE_DEFAULTS = {'lzx_pop_scale': 1.28}
PRESETS = {
    LZX_WORD_POP: {
        'label': '自制达芬奇字幕-逐词柔弹',
        'style': {
            'font_family':'Microsoft YaHei UI', 'fontsize':108, 'font_weight':700,
            'bold':True, 'color_text':'#ffffff', 'color_outline':'#0d1f4d',
            'border_width':4.86, 'use_stroke':True, 'opacity_outline':255,
            'color_shadow':'#000000', 'opacity_shadow':255,
            'shadow_blur':5, 'shadow_offset_x':5.4, 'shadow_offset_y':5.4,
            'use_box':False, 'karaoke_highlight':False, 'dynamic_box':False,
            'anim_in_type':'lzx_word_pop', 'anim_in_duration':.333,
            'anim_out_type':'none', 'anim_out_duration':0,
            'wrap_width_percent':85, 'line_spacing':1.1, 'pos_y':.84,
            'lzx_pop_scale':1.28,
        },
        'layer': {'caption_words':6, 'caption_per_line':4, 'height':28,
                  'word_spacing':0, 'word_spacing_version':2,
                  'caption_random_size':False, 'caption_hold_ms':1000},
    },
}


def pop_curve(progress, peak=1.28):
    points = ((0,.55,0),(.18,.86,1),(.55,peak,1),(.78,.94,1),(1,1,1))
    progress = max(0,min(1,progress))
    for (a,scale_a,alpha_a),(b,scale_b,alpha_b) in zip(points,points[1:]):
        if progress <= b:
            blend = (progress-a)/(b-a)
            return scale_a+(scale_b-scale_a)*blend, alpha_a+(alpha_b-alpha_a)*blend
    return 1.,1.
