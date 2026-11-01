"""Original native presets informed by common caption design patterns.

Reference review: CapCut caption categories, Adobe caption styling, and the
DaVinci Resolve 20 Animated Subtitles guide. No commercial template assets.
"""
from copy import deepcopy

PREFIX = 'modern:'


def preset(label, category, *, style=None, layer=None):
    return {'label': label, 'category': category, 'description': label.split(' · ', 1)[-1],
        'style': {'font_family':'Poppins','fontsize':76,'font_weight':700,'bold':True,
            'color_text':'#ffffff','color_outline':'#141820','use_stroke':True,'border_width':3,
            'opacity_outline':255,'color_shadow':'#000000','opacity_shadow':130,
            'shadow_blur':2,'shadow_offset_x':0,'shadow_offset_y':2,
            'use_box':False,'karaoke_highlight':False,'dynamic_box':False,
            'anim_in_type':'none','anim_in_duration':.16,'anim_out_type':'none','anim_out_duration':0,
            'line_spacing':1.15,'pos_y':.78,'wrap_width_percent':90,**(style or {})},
        'layer': {'caption_words':8,'caption_per_line':4,'height':26,'width':90,'x':50,'y':78,
            'caption_display_mode':'group','word_spacing':0,'word_spacing_version':2,
            'caption_random_size':False,'caption_hold_ms':180,'outline_position':'outside',**(layer or {})}}


PRESETS = {
    PREFIX+'talk_highlight': preset('口播 · 白字黄词','口播',style={
        'karaoke_highlight':True,'color_high':'#FFD84D'},layer={'caption_words':6,'caption_per_line':3}),
    PREFIX+'talk_yellow': preset('口播 · 黄字粗描边','口播',style={
        'color_text':'#FFE34D','border_width':5,'fontsize':88,'anim_in_type':'pop',
        'pop_start':.88,'pop_peak':1.04},layer={'caption_display_mode':'word','caption_words':5,'caption_per_line':3}),
    PREFIX+'talk_pill': preset('口播 · 当前词胶囊','口播',style={
        'dynamic_box':True,'color_high_bg':'#FFE34D','opacity_high_bg':255,'dynamic_radius':12,
        'high_padding':8,'karaoke_highlight':True,'color_high':'#161B28',
        'dyn_box_anim':True,'dyn_box_anim_duration':.16,'dyn_box_anim_overshoot':1.05},
        layer={'caption_words':6,'caption_per_line':3}),
    PREFIX+'talk_pop': preset('口播 · 单词柔弹','口播',style={
        'anim_in_type':'lzx_word_pop','anim_in_duration':.26,'lzx_pop_scale':1.12,
        'fontsize':100,'border_width':4},layer={'caption_display_mode':'word','caption_words':4,'caption_per_line':2}),
    PREFIX+'talk_keywords': preset('口播 · 关键词放大','口播',style={
        'keyword_emphasis_enabled':True,'keyword_terms':'nádej, láska, pokoj, hope, peace, 希望, 信心, 重点',
        'keyword_color':'#FFE34D','keyword_scale':1.22},layer={'caption_words':6,'caption_per_line':3}),
    PREFIX+'film_white': preset('影视 · 双行细白','影视',style={
        'fontsize':52,'font_weight':500,'border_width':1,'shadow_blur':2,'shadow_offset_y':1,
        'anim_in_type':'fade','anim_in_duration':.12,'anim_out_type':'fade','anim_out_duration':.1},
        layer={'caption_words':14,'caption_per_line':7,'height':18,'y':86}),
    PREFIX+'film_narration': preset('影视 · 黑底叙述','影视',style={
        'fontsize':58,'border_width':0,'use_stroke':False,'use_box':True,'opacity_bg':185,
        'box_radius':5,'box_adaptive_width':True,'box_padding_x':18,'box_padding_y':10},
        layer={'caption_words':12,'caption_per_line':6,'height':22,'y':82}),
    PREFIX+'vlog_italic': preset('Vlog · 斜体淡入','Vlog',style={
        'italic':True,'font_weight':500,'fontsize':62,'color_text':'#FFF5E9',
        'border_width':0,'use_stroke':False,'anim_in_type':'fade','anim_in_duration':.25,
        'anim_out_type':'fade','anim_out_duration':.18,'shadow_blur':3},
        layer={'caption_words':8,'caption_per_line':4,'y':80}),
    PREFIX+'news_bar': preset('新闻 · 稳定字幕条','新闻',style={
        'fontsize':58,'use_stroke':False,'border_width':0,'use_box':True,'opacity_bg':220,
        'color_bg':'#132A42','box_radius':0,'box_adaptive_width':False,
        'box_padding_y':12,'box_padding_x':20},layer={'caption_words':12,'caption_per_line':6,'y':84,'height':20}),
    PREFIX+'course_typewriter': preset('教程 · 打字光标','教程',style={
        'fontsize':66,'border_width':2,'use_box':True,'opacity_bg':150,'box_radius':8,
        'box_adaptive_width':True},layer={'caption_display_mode':'char','caption_cursor':True,
        'caption_cursor_color':'#58DBD0','caption_words':10,'caption_per_line':5}),
    PREFIX+'course_lines': preset('课程 · 逐行讲义','教程',style={
        'fontsize':66,'color_text':'#E9FFF3','border_width':1,'color_outline':'#092A21'},
        layer={'caption_display_mode':'line','caption_words':12,'caption_per_line':4,
        'caption_accumulate':True,'align':'left','vertical':'top','y':52,'height':76}),
    PREFIX+'lyric_follow': preset('歌词 · 竖向跟读','歌词',style={
        'fontsize':70,'karaoke_highlight':True,'color_high':'#6DE4FF','border_width':2},
        layer={'caption_follow_mode':'line','caption_clip_feather':8,'caption_focus_y':50,
        'caption_context_opacity':32,'caption_read_opacity':100,'caption_active_opacity':100,
        'caption_per_line':4,'height':58,'y':52}),
    PREFIX+'lyric_line': preset('歌词 · 整句追词','歌词',style={
        'fontsize':76,'karaoke_highlight':True,'color_high':'#84F7C5','dynamic_box':False,
        'border_width':3},layer={'caption_words':10,'caption_per_line':5,'y':76}),
    PREFIX+'kinetic_slide': preset('动感 · 快速上滑','动感',style={
        'fontsize':88,'anim_in_type':'slide_up','anim_in_duration':.16,
        'anim_out_type':'fade','anim_out_duration':.08},
        layer={'caption_display_mode':'word','caption_words':4,'caption_per_line':2}),
    PREFIX+'kinetic_blur': preset('动感 · 由虚转实','动感',style={
        'fontsize':86,'anim_in_type':'blur_sharp','anim_in_duration':.22,'blur_sharp_max':10},
        layer={'caption_display_mode':'word','caption_words':4,'caption_per_line':2}),
    PREFIX+'interview_word': preset('访谈 · 单词下划线','访谈',style={
        'fontsize':100,'only_show_active_word':True,'use_underline':True,'color_underline':'#FFCE5B',
        'anim_in_type':'fade','anim_in_duration':.08},layer={'caption_words':1,'caption_per_line':1}),
    PREFIX+'neon_outline': preset('特效 · 霓虹双描边','特效',style={
        'fontsize':86,'stroke_expand_enabled':True,'stroke_expand_layers':2,'stroke_expand_step':3,
        'stroke_expand_feather':2,'stroke_expand_colors':['#3ECFFF','#CF76FF'],
        'border_width':1,'color_outline':'#23354E'},layer={'caption_words':6,'caption_per_line':3}),
    PREFIX+'poster_headline': preset('海报 · 首行大字','海报',style={
        'fontsize':70,'font_weight':800,'use_box':False,'border_width':3},
        layer={'caption_words':12,'caption_per_line':4,'height':60,'y':50,
        'caption_display_mode':'line','caption_first_line_emphasis':True,
        'caption_first_line_scale':1.35,'caption_first_line_bold':True}),
    PREFIX+'brand_minimal': preset('品牌 · 极简白字','品牌',style={
        'fontsize':56,'font_weight':500,'use_stroke':False,'border_width':0,'letter_spacing':1.5,
        'shadow_blur':2,'shadow_offset_y':1,'anim_in_type':'fade','anim_in_duration':.2},
        layer={'caption_words':10,'caption_per_line':5,'height':20,'y':82}),
    PREFIX+'bubble_cards': preset('卡片 · 撞色气泡','卡片',style={
        'fontsize':78,'anim_in_type':'scatter_pop','anim_in_duration':.22,'pop_start':.8,'pop_peak':1.08,
        'scatter_min_scale':.9,'scatter_max_scale':1.05,'scatter_min_rotate':-3,'scatter_max_rotate':3,
        'scatter_max_words':3,'scatter_accum_prob':.8,'use_box':True,'opacity_bg':235,
        'color_bg':'#473866','box_radius':14,'box_padding_x':16,'box_padding_y':10,
        'box_follow_motion':True,'border_width':0,'use_stroke':False},
        layer={'caption_words':3,'caption_per_line':3,'height':55,'y':55,'caption_display_mode':'word'}),
}
