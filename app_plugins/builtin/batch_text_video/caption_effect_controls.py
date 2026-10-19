"""Complete control ownership for implemented effects, not upstream-only data."""

# id, title, tab, enable switch, fields, relevant entrance modes
GROUPS = (
    ('motion','入场、退场与旋转',0,None,('anim_in_type','anim_in_duration','anim_out_type','anim_out_duration','rotation'),()),
    ('pop','回弹缩放',0,None,('pop_start','pop_peak'),('pop','scatter_pop','word_random_position','word_pop_random','word_pop_random_pulse','letter_jump')),
    ('word_pop','逐词弹入',0,None,('word_pop_random_duration',),('word_pop_random','word_random_position')),
    ('pulse','逐词回弹',0,None,('word_pop_random_pulse_min_scale','word_pop_random_pulse_max_scale','word_pop_random_pulse_duration'),('word_pop_random_pulse',)),
    ('jump','逐词放大',0,None,('letter_jump_scale','letter_jump_duration'),('letter_jump',)),
    ('bounce','文字跳动',0,None,('char_bounce_height',),('char_bounce',)),
    ('floating','漂浮',0,None,('floating_amplitude','floating_period'),('floating',)),
    ('blur','模糊入场',0,None,('blur_sharp_max',),('blur_sharp',)),
    ('lzx','达芬奇柔弹',0,None,('lzx_pop_scale',),('lzx_word_pop',)),
    ('metronome','视觉节拍',0,None,('metronome_bpm',),('metronome',)),
    ('flash','闪光',1,None,('flash_color','flash_duration'),('flash_highlight',)),
    ('holy','光晕',1,None,('holy_glow_radius','holy_glow_color'),('holy_glow',)),
    ('box_bounce','当前词高亮框回弹',0,'dyn_box_anim',('dyn_box_anim_overshoot','dyn_box_anim_duration'),()),
    ('underline','彩色下划线',1,'use_underline',('color_underline',),()),
    ('stroke','多层描边',1,'stroke_expand_enabled',('stroke_expand_layers','stroke_expand_step','stroke_expand_feather','stroke_expand_colors'),()),
    ('trail','速度幻影',1,'speed_trail_enabled',('speed_trail_layers','speed_trail_step','speed_trail_color','speed_trail_opacity'),()),
    ('mask','全画面压暗',1,'global_mask_enabled',('global_mask_color','global_mask_opacity'),()),
    ('glow','环境柔光',1,'ambient_glow_enabled',('ambient_glow_color','ambient_glow_radius','ambient_glow_opacity'),()),
    ('ambient','环境暗部',1,'ambient_lighting_enabled',('ambient_dark_color','ambient_dark_opacity'),()),
    ('active','只显示正在读的词',2,'only_show_active_word',(),()),
    ('layout','保留特效排版',2,'styled_layout',(),()),
    ('scrolling','歌词上下文',2,'scrolling_mode',('scrolling_visible_lines','scrolling_opacity_context'),()),
    ('scatter','随机定位与旋转',2,None,('scatter_seed','scatter_min_rotate','scatter_max_rotate'),('scatter_pop','word_random_position')),
    ('scatter_group','随机词组累积',2,None,('scatter_max_words','scatter_accum_prob'),('scatter_pop',)),
)

FONT_EFFECT_KEYS = {'block_typography_enabled','block_scale_min','block_scale_max',
                    'word_pop_random_min_scale','word_pop_random_max_scale','scatter_min_scale','scatter_max_scale'}
ADVANCED_KEYS = {key for _id,_title,_tab,switch,fields,_modes in GROUPS for key in ((switch,) if switch else ()) + fields}

RANGES = {
    'rotation':(-180,180,1), 'scatter_min_rotate':(-180,180,1), 'scatter_max_rotate':(-180,180,1),
    'scatter_seed':(0,10000,0), 'scatter_max_words':(1,12,0),
    'scatter_accum_prob':(0,1,2), 'scrolling_visible_lines':(1,5,0),
    'scrolling_opacity_context':(0,1,2), 'metronome_bpm':(1,300,1),
    'stroke_expand_layers':(1,6,0), 'stroke_expand_step':(0,30,1), 'stroke_expand_feather':(0,150,1),
    'speed_trail_layers':(1,8,0), 'speed_trail_step':(-150,150,1), 'speed_trail_opacity':(0,255,0),
    'ambient_glow_radius':(0,10000,1), 'ambient_glow_opacity':(0,1,2),
    'ambient_dark_opacity':(0,1,2), 'global_mask_opacity':(0,1,2),
    'pop_start':(.1,3,2), 'pop_peak':(.1,3,2), 'lzx_pop_scale':(1,2,2),
    'letter_jump_scale':(.1,3,2), 'dyn_box_anim_overshoot':(.1,3,2),
    'word_pop_random_pulse_min_scale':(.1,3,2), 'word_pop_random_pulse_max_scale':(.1,3,2),
    'floating_period':(.1,30,2),
}


def number_range(key):
    if key in RANGES:
        return RANGES[key]
    if key.endswith(('_duration', '_period')):
        return (0,5,3)
    return (0,150,1)
