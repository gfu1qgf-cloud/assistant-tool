"""Native Qt geometry + libass motion for the imported subtitle-style catalog.

No JS interpreter, browser renderer, per-frame Python video or new AI model.
Timings belong to the task; style changes never invoke/rewrite alignment.
"""
from model.AppDataStorage import data_file
import hashlib
import json
import math
from pathlib import Path
import random
from itertools import groupby
from qt_compat import QtCore, QtGui
from .caption_styles import colors, effects
from .caption_random import size_between, contrast_unit, size_contrast
from .shadow_render import shadow_extent, paint_blurred
from .word_spacing import glyph_rows, glyph_row_width, glyph_row_inset, natural_space
from .caption_appearance import group_colors, active_segments, add_highlight_box, add_foreground
from .caption_display import display_settings, layout_glyphs, reveal_events, reveal_start

REVEAL = {'word_pop_random', 'word_pop_random_pulse', 'word_random_position',
          'scatter_pop', 'typewriter', 'char_bounce', 'letter_jump', 'metronome', 'lzx_word_pop'}
MAX_ROWS = 80000


def rectangle(width, height, radius=0):
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0,0,width,height), radius, radius)
    return path


def groups_and_events(words, layer, width, height, duration):
    from .captions import validate_words, _glyph, glyph_line_metrics
    from .layout import TextDoesNotFit
    words = validate_words(words,duration)
    style = effects(layer)
    display = display_settings(layer)
    from .caption_follow import enabled as follow_enabled
    following = follow_enabled(layer)
    scale = width/1080
    box = QtCore.QRectF(width*(layer['x']-layer['width']/2)/100,
        height*(layer['y']-layer['height']/2)/100,width*layer['width']/100,height*layer['height']/100)
    box = box.intersected(QtCore.QRectF(0,0,width,height))
    halo = max(layer['outline_width']+3*layer.get('outline_blur',0)+2, layer['padding'], style['high_padding'] if style['dynamic_box'] else 0,
        shadow_extent(layer))*scale
    box.adjust(halo,halo,-halo,-halo)
    if box.width() <= 1 or box.height() <= 1:
        raise TextDoesNotFit(layer['name']+'：字幕框没有足够空间。')
    rng = random.Random(hashlib.sha256(json.dumps([layer['caption_seed'],style['scatter_seed'],words],
        ensure_ascii=False,sort_keys=True).encode()).digest())
    mode = style['anim_in_type']
    trusted = all('cue' in word for word in words)
    if following or display['caption_accumulate']:
        grouped = [words]
    elif trusted:
        grouped = [list(values) for _,values in groupby(words,key=lambda word:word['cue'])]
    elif mode == 'scatter_pop':
        grouped, index = [], 0
        while index < len(words):
            count = rng.randint(1,int(style['scatter_max_words'])) if rng.random() < style['scatter_accum_prob'] else 1
            grouped.append(words[index:index+count]); index += count
    else:
        count = layer['caption_words']
        grouped = [words[index:index+count] for index in range(0,len(words),count)]
    pop_peak=max(1,style['pop_start'],style['pop_peak'])
    peak = {'pop':pop_peak, 'scatter_pop':pop_peak, 'word_pop_random':pop_peak,
        'word_random_position':pop_peak, 'letter_jump':max(1,style['pop_start'],style['letter_jump_scale']),
        'word_pop_random_pulse':max(1,style['pop_start'],style['word_pop_random_pulse_max_scale']),
        'lzx_word_pop':style['lzx_pop_scale']}.get(mode,1)
    if style['dyn_box_anim'] and style['dynamic_box']:
        peak = max(peak,style['dyn_box_anim_overshoot'])
    # The Fusion-style pop scales an active word around its own centre. Do
    # not reserve 128% width for EVERY settled word: long words would create
    # very large gaps even after their pop had finished.
    layout_peak = 1 if mode == 'lzx_word_pop' else peak
    output, groups = [], []
    for gi, group in enumerate(grouped):
        start = min(word['start'] for word in group)
        next_start = grouped[gi+1][0]['start'] if gi+1 < len(grouped) else duration
        end = min(duration,max(word['end'] for word in group)) if trusted else min(
            duration,next_start,max(word['end'] for word in group)+layer['caption_hold_ms']/1000)
        contrast = size_contrast(layer)
        random_sizes = [size_between(rng,layer['font_min'],layer['font_max'],contrast) for word in group]
        rotations = [rng.uniform(style['scatter_min_rotate'],style['scatter_max_rotate'])
                     if mode in {'scatter_pop','word_random_position'} else style['rotation'] for word in group]
        if mode in {'word_pop_random','scatter_pop','word_random_position'} or style['block_typography_enabled']:
            lo,hi = (style['block_scale_min'],style['block_scale_max']) if style['block_typography_enabled'] else (
                (style['word_pop_random_min_scale'],style['word_pop_random_max_scale']) if mode == 'word_pop_random' else
                (style['scatter_min_scale'],style['scatter_max_scale']))
            random_sizes = [layer['font_max']*size_between(rng,lo,hi,contrast) for word in group]
        per_line = layer['caption_per_line']
        def measure(fraction):
            glyphs, lines, gap = layout_glyphs(group, layer, random_sizes, scale, fraction,
                available_width=box.width(), per_line=per_line, peak=layout_peak, edge_peak=peak, native=True,
                gap_for=lambda glyphs: (max(0,max(glyph['_space_advance'] for glyph in glyphs)+layer['word_spacing']*scale)
                    if mode == 'lzx_word_pop' else max(0,8+layer['word_spacing'])*scale))
            widths = [glyph_row_width(glyphs,line,gap,layout_peak,peak) for line in lines]
            metrics = [glyph_line_metrics([glyphs[i] for i in line]) for line in lines]
            heights = [line_height*peak*layer['line_spacing'] for line_height,_baseline in metrics]
            return glyphs,lines,widths,heights,gap,metrics
        room = box.height()/int(style['scrolling_visible_lines']) if style['scrolling_mode'] and not following else box.height()
        fits = lambda value: max(value[2]) <= box.width() and (max(value[3]) if following else sum(value[3])) <= room
        chosen = measure(1)
        if not fits(chosen):
            chosen = measure(0)
            if not fits(chosen):
                raise TextDoesNotFit(layer['name']+'：最小字号仍放不下'+('完整文案' if display['caption_accumulate'] else '本组字幕')+
                    '；请扩大字幕框、降低最小字号或减少每组词数。未裁掉文字。')
            low,high = 0.,1.
            for _ in range(10):
                middle = (low+high)/2
                value = measure(middle)
                if fits(value): low,chosen = middle,value
                else: high = middle
        glyphs,lines,widths,heights,gap,metrics = chosen
        y = box.top() if following else box.top()+(box.height()-sum(heights))*{'top':0,'center':.5,'bottom':1}[layer['vertical']]
        group_events = []
        occupied = []
        for li,line in enumerate(lines):
            x = (box.left()+(box.width()-widths[li])*{'left':0,'center':.5,'right':1}[layer['align']]
                 +glyph_row_inset(glyphs,line,layout_peak,peak))
            line_height,ascent = metrics[li]
            baseline = y+(heights[li]-line_height)/2+ascent
            for wi in line:
                word,glyph = group[wi],glyphs[wi]
                px,py = x+glyph['width']*(layout_peak-1)/2,baseline-glyph['_baseline']
                if style['only_show_active_word'] and not following:
                    px = box.center().x()-glyph['width']/2
                    py = box.center().y()-glyph['height']/2
                if mode in {'scatter_pop','word_random_position'} and not following:
                    # Reserved diagonal bounds keep rotated bubbles inside the component.
                    radius = math.hypot(glyph['width'],glyph['height'])*peak/2
                    if radius*2 > min(box.width(),box.height()):
                        radius = max(glyph['width'],glyph['height'])*peak/2
                    left,right = box.left()+radius,box.right()-radius
                    top,bottom = box.top()+radius,box.bottom()-radius
                    for attempt in range(20):
                        cx = rng.uniform(min(left,right),max(left,right))
                        cy = rng.uniform(min(top,bottom),max(top,bottom))
                        candidate = QtCore.QRectF(cx-radius,cy-radius,2*radius,2*radius)
                        if not any(candidate.intersects(other) for other in occupied): break
                    occupied.append(candidate)
                    px,py = cx-glyph['width']/2,cy-glyph['height']/2
                event = {**word,**glyph,'spoken_end':word['end'],'end':end,'x':px,'y':py,
                    'group':gi,'line':li,'index':wi,'group_start':start,'group_end':end,
                    'rotation':rotations[wi], 'context':False}
                if following:
                    event.update(end=duration, group_start=0., group_end=duration,
                                 _follow_line_center=y+heights[li]/2)
                group_events.append(event); output.append(event)
                x += glyph['width']*layout_peak+gap
            y += heights[li]
        bounds = QtCore.QRectF()
        for item in group_events:
            bounds = bounds.united(QtCore.QRectF(item['x'],item['y'],item['width'],item['height']))
        groups.append({'events':group_events,'bounds':box if following else bounds,
                       'start':0. if following else start,'end':duration if following else end})
    if style['scrolling_mode'] and not following:
        context = []
        visible = int(style['scrolling_visible_lines'])
        half = (visible-1)//2
        for gi,group in enumerate(groups):
            spacing = max(group['bounds'].height()+24*scale, room)
            for offset in range(-half,visible-half):
                if not offset or not 0 <= gi+offset < len(groups): continue
                other = groups[gi+offset]
                delta = group['bounds'].center().y()+offset*spacing-other['bounds'].center().y()
                for event in other['events']:
                    context.append({**event,'start':group['start'],'end':group['end'],
                        'group_start':group['start'],'group_end':group['end'],
                        'y':event['y']+delta,'context':True})
        output.extend(context)
    return output,groups,style


def make_primitives(words, layer, width, height, duration):
    events,groups,style = groups_and_events(words,layer,width,height,duration)
    scale = width/1080
    from .caption_follow import enabled as follow_enabled, track_for
    following = follow_enabled(layer)
    viewport = QtCore.QRectF(width*(layer['x']-layer['width']/2)/100,
        height*(layer['y']-layer['height']/2)/100,width*layer['width']/100,height*layer['height']/100).intersected(
            QtCore.QRectF(0,0,width,height))
    track = track_for(events,layer,viewport,duration) if following else None
    clip = (viewport.left(),viewport.top(),viewport.right(),viewport.bottom())
    primitives = []
    group_palette=group_colors(layer,math.ceil(len(events)/layer['caption_words']) if following else len(groups),words)
    def add(path,x,y,start,end,color,opacity=1,**options):
        if end-start < .005 or opacity <= 0: return
        bounds = path.boundingRect()
        primitives.append({'native':path,'x':x,'y':y,'width':bounds.width(),'height':bounds.height(),
            'start':start,'end':end,'color':colors(color),'opacity':max(0,min(1,opacity)),
            'border':0,'border_color':layer['outline_color'],'blur':0,'rotation':0,'z':0,
            'motion':'none','motion_ms':0,'out':'none','out_ms':0,**options})
    def background(path,x,y,start,end,color,opacity,blur=0,**options):
        add(path,x,y,start,end,color,opacity,**{'z':-5,'blur':blur,**options})
    begin,end = min(event['start'] for event in events),max(event['end'] for event in events)
    if style['global_mask_enabled']:
        background(rectangle(width,height),0,0,begin,end,
            style['global_mask_color'],style['global_mask_opacity'],z=-20)
    if style['ambient_lighting_enabled']:
        background(rectangle(width,height),0,0,begin,end,
            style['ambient_dark_color'],style['ambient_dark_opacity'],z=-19)
    if style['ambient_glow_enabled']:
        radius = min(max(width,height),style['ambient_glow_radius']*scale)
        for band in range(48,0,-1):
            r = radius*band/48
            path = QtGui.QPainterPath(); path.addEllipse(QtCore.QRectF(0,0,r*2,r))
            background(path,width*layer['x']/100-r,height*layer['y']/100-r/2,begin,end,
                style['ambient_glow_color'],style['ambient_glow_opacity']*.02,blur=26*scale,z=-19)
    mode = style['anim_in_type']
    def animation(event,entry=True):
        seconds = (style['word_pop_random_duration'] if mode in {'word_pop_random','word_random_position'} else
            style['word_pop_random_pulse_duration'] if mode=='word_pop_random_pulse' else
            style['letter_jump_duration'] if mode=='letter_jump' else style['anim_in_duration'])
        return {'motion':mode if entry else 'none',
            'motion_ms':min(seconds*1000,(event['end']-event['start'])*400),
            'out':style['anim_out_type'],
            'out_ms':min(style['anim_out_duration']*1000,(event['end']-event['start'])*300),
            'pulse':style['word_pop_random_pulse_min_scale']+(style['word_pop_random_pulse_max_scale']-
                style['word_pop_random_pulse_min_scale'])*contrast_unit(
                    (event['index']*.61803398875+layer['caption_seed']*.13)%1, size_contrast(layer)),
            'jump':style['letter_jump_scale'], 'lzx_peak':style['lzx_pop_scale'], 'floating_amplitude':style['floating_amplitude']*scale,
            'pop_start':style['pop_start'], 'pop_peak':style['pop_peak'],
            'floating_period':style['floating_period'],'bounce':style['char_bounce_height']*scale,
            'max_blur':style['blur_sharp_max']*scale,'rotation':event['rotation']}
    if style['use_box'] and mode not in {'scatter_pop','word_random_position'}:
        for group in groups:
            bounds = QtCore.QRectF(group['bounds'])
            bounds.adjust(-style['box_padding_x']*scale,-style['box_padding_y']*scale,
                          style['box_padding_x']*scale,style['box_padding_y']*scale)
            if not style['box_adaptive_width']:
                bounds.setLeft(width*(layer['x']-layer['width']/2)/100)
                bounds.setRight(width*(layer['x']+layer['width']/2)/100)
            color = style['bg_gradient_colors'] if style['bg_gradient_enabled'] else layer['background_color']
            background(rectangle(bounds.width(),bounds.height(),style['box_radius']*scale),
                bounds.x(),bounds.y(),group['start'],group['end'],color,style['opacity_bg']/255,
                blur=style['box_blur']*scale,gradient_direction=style['bg_gradient_type'],
                **(animation(group['events'][0],entry=mode not in REVEAL) if style['box_follow_motion'] else {}))
            if style['bg_gradient_highlight']:
                background(rectangle(bounds.width(),bounds.height()/3,style['box_radius']*scale),
                    bounds.x(),bounds.y(),group['start'],group['end'],'#ffffff',.08)
    draw_events = reveal_events(events, layer)
    for event in draw_events:
        context = event['context']
        start = reveal_start(event, layer, events)
        stop = event['end']
        if context:
            start,stop = event['group_start'],event['group_end']
        if style['only_show_active_word'] and not context:
            start=max(start,event['start'])
            stop = min(stop,event['spoken_end'])
        # BPM can affect visual pulse, never shift authoritative spoken timestamps.
        options = animation(event)
        if following:
            options.update(follow_track=track, screen_clip=clip,
                           follow_spoken=(event['start'], event['spoken_end']),
                           follow_opacities=(layer.get('caption_context_opacity',40)/100,
                                             layer.get('caption_active_opacity',100)/100,
                                             layer.get('caption_read_opacity',100)/100))
        if mode == 'metronome':
            options.update(motion='word_pop_random_pulse',motion_ms=min(60/style['metronome_bpm']*1000,250))
        if context: options.update(motion='none',out='none')
        path = event['_native_path']
        base_opacity = style['scrolling_opacity_context'] if context else 1
        x,y = event['x'],event['y']
        if style['use_box'] and mode in {'scatter_pop','word_random_position'} and not context:
            px,py = style['box_padding_x']*scale,style['box_padding_y']*scale
            background(rectangle(event['width']+2*px,event['height']+2*py,style['box_radius']*scale),
                x-px,y-py,start,stop,layer['background_color'],style['opacity_bg']/255,
                **(options if style['box_follow_motion'] else {}))
        if layer['shadow_enabled']:
            add(path,x+layer['shadow_x']*scale,y+layer['shadow_y']*scale,start,stop,
                layer['shadow_color'],layer['shadow_opacity']/100*base_opacity,
                z=-2,blur=layer.get('shadow_blur',style['shadow_blur'])*scale,**options)
        if style['stroke_expand_enabled']:
            stroke_palette = colors(style['stroke_expand_colors'])
            for n in reversed(range(min(6,int(style['stroke_expand_layers'])))):
                add(path,x,y,start,stop,stroke_palette[n%len(stroke_palette)],.55*base_opacity,z=-2,
                    border=(layer['outline_width']+(n+1)*style['stroke_expand_step'])*scale,
                    border_color=stroke_palette[n%len(stroke_palette)],blur=style['stroke_expand_feather']*scale,**options)
        if mode == 'holy_glow' and not context:
            add(path,x,y,start,stop,style['holy_glow_color'],.5,z=-2,
                border=style['holy_glow_radius']*scale,blur=style['holy_glow_radius']*scale,**options)
        if style['speed_trail_enabled'] and not context:
            count = min(8,int(style['speed_trail_layers']))
            for n in range(count,0,-1):
                add(path,x+n*style['speed_trail_step']*scale,y,start,stop,style['speed_trail_color'],
                    style['speed_trail_opacity']/255*(1-n/(count+1))*.5,z=-2,**options)
        bg_options={**options}
        if style['dyn_box_anim']:
            bg_options.update(motion='word_pop_random_pulse',pulse=style['dyn_box_anim_overshoot'],
                motion_ms=style['dyn_box_anim_duration']*1000)
        if not event.get('_character_piece') or event.get('_character_index') == 0:
            add_highlight_box(background,event,style,scale,bg_options)
        base_color=(style['color_text'] if style['text_gradient_enabled'] and not layer.get('caption_group_colors')
                    else group_palette[event['index']//layer['caption_words'] if following else event['group']])
        segments=active_segments(start,stop,event,base_color,style,context=context)
        if following:
            # Constant-alpha ASS rows and Qt preview use exactly the same
            # spoken boundaries, including when colour highlighting is off.
            split = []
            for a,b,color in segments:
                points=sorted({a,b}|{t for t in (event['start'],event['spoken_end']) if a<t<b})
                split.extend((c,d,color) for c,d in zip(points,points[1:]))
            segments=split
        for a,b,color in segments:
            phase = {**options,'motion':options['motion'] if abs(a-start)<.005 else 'none',
                     'out':options['out'] if abs(b-stop)<.005 else 'none'}
            add_foreground(add,path,x,y,a,b,color,base_opacity,layer,scale,
                {**phase,'gradient_direction':style['high_gradient_direction']
                 if color==style['color_high'] else style['text_gradient_direction']})
        display = display_settings(layer)
        if event.get('_character_piece') and display['caption_cursor']:
            add(rectangle(max(1,2*scale),event['height']),x+event['_cursor_edge'],y,
                event['start'],min(stop,event['_cursor_until']),display['caption_cursor_color'])
        if style['use_underline'] and not context:
            add(rectangle(event['width'],max(1,3*scale)),x,y+event['height']+2*scale,
                start,stop,style['color_underline'],**options)
        if mode == 'flash_highlight' and not context:
            add(path,x,y,event['start'],min(stop,event['start']+style['flash_duration']),
                style['flash_color'],.85,motion='fade',out='fade',out_ms=style['flash_duration']*1000)
    return primitives,events


def gradient_pieces(primitive):
    palette = primitive['color']
    if len(palette)==1: return [(primitive['native'],palette[0])]
    path = primitive['native']; bounds = path.boundingRect()
    vertical = primitive.get('gradient_direction') in {'vertical','linear_v'}
    result = []
    count = 96
    for index in range(count):
        part = QtCore.QRectF(bounds)
        if vertical:
            part.setTop(bounds.top()+bounds.height()*index/count)
            part.setBottom(bounds.top()+bounds.height()*(index+1)/count)
        else:
            part.setLeft(bounds.left()+bounds.width()*index/count)
            part.setRight(bounds.left()+bounds.width()*(index+1)/count)
        position = index/(count-1)*(len(palette)-1)
        stop = min(len(palette)-2,int(position)); blend = position-stop
        a,b = QtGui.QColor(palette[stop]),QtGui.QColor(palette[stop+1])
        color = QtGui.QColor(*[round(getattr(a,k)()*(1-blend)+getattr(b,k)()*blend) for k in ('red','green','blue')])
        clipped = path.intersected(rectangle(part.width(),part.height()).translated(part.x(),part.y()))
        if not clipped.isEmpty(): result.append((clipped,color.name()))
    return result


def ass_gradient_pieces(primitive):
    """Binary ASS clipping avoids antialias seams between translucent strips."""
    palette=primitive['color']
    if len(palette)==1:
        return [(palette[0],None)]
    bounds=primitive['native'].boundingRect()
    vertical=primitive.get('gradient_direction') in {'vertical','linear_v'}
    count=96 if primitive['native'].elementCount()<60 else 32
    result=[]
    for i in range(count):
        position=(i+.5)/count*(len(palette)-1)
        stop=min(len(palette)-2,int(position));blend=position-stop
        a,b=QtGui.QColor(palette[stop]),QtGui.QColor(palette[stop+1])
        color=QtGui.QColor(*[round(getattr(a,k)()*(1-blend)+getattr(b,k)()*blend) for k in ('red','green','blue')])
        if vertical:
            clip=(-100000,bounds.top()+bounds.height()*i/count,
                  100000,bounds.top()+bounds.height()*(i+1)/count)
        else:
            clip=(bounds.left()+bounds.width()*i/count,-100000,
                  bounds.left()+bounds.width()*(i+1)/count,100000)
        clip=list(clip)
        if i==0:clip[1 if vertical else 0]=-100000
        if i==count-1:clip[3 if vertical else 2]=100000
        result.append((color.name(),clip))
    return result


def motion_at(primitive, time):
    elapsed = max(0,time-primitive['start'])
    seconds = max(.001,primitive.get('motion_ms',0)/1000)
    p = min(1,elapsed/seconds)
    mode = primitive['motion']
    size,dx,dy,alpha,blur = 1.,0.,0.,1.,primitive['blur']
    if mode in {'pop','scatter_pop','word_random_position','word_pop_random','word_pop_random_pulse','letter_jump'}:
        peak = primitive.get('pulse',1.15) if mode=='word_pop_random_pulse' else primitive.get('jump',1.5) if mode=='letter_jump' else primitive.get('pop_peak',1.15)
        initial=primitive.get('pop_start',.68)
        size = (initial+(peak-initial)*p/.55) if p < .55 else peak+(1-peak)*(p-.55)/.45
    if mode == 'lzx_word_pop':
        from .caption_custom_styles import pop_curve
        size,alpha = pop_curve(p,primitive.get('lzx_peak',1.28))
    if mode=='fade': alpha = p
    if mode.startswith('slide_'):
        delta = (1-p)**2*80
        dx = delta if mode=='slide_left' else -delta if mode=='slide_right' else 0
        dy = delta if mode=='slide_up' else -delta if mode=='slide_down' else 0
    if mode=='char_bounce': dy = -primitive.get('bounce',20)*math.sin(math.pi*p)
    if mode=='floating': dy = primitive.get('floating_amplitude',8)*math.sin(2*math.pi*elapsed/primitive.get('floating_period',2))
    if mode=='blur_sharp': blur += primitive.get('max_blur',20)*(1-p)
    remaining = max(0,primitive['end']-time)
    out_ms = primitive.get('out_ms',0)
    if out_ms and remaining < out_ms/1000:
        progress = 1-remaining/(out_ms/1000)
        if primitive['out']=='fade': alpha *= 1-progress
        elif primitive['out']=='pop': size *= 1-progress*.5
        elif primitive['out'].startswith('slide_'):
            dx += 80*progress if primitive['out']=='slide_right' else -80*progress if primitive['out']=='slide_left' else 0
            dy += 80*progress if primitive['out']=='slide_down' else -80*progress if primitive['out']=='slide_up' else 0
    if primitive.get('follow_track'):
        from .caption_follow import offset_at
        fx,fy=offset_at(primitive['follow_track'],time)
        dx+=fx;dy+=fy
        spoken=primitive.get('follow_spoken')
        if spoken:
            states = primitive.get('follow_opacities')
            if states is not None:
                state = 0 if time < spoken[0] else 1 if time < spoken[1] else 2
                alpha *= states[state]
            elif not spoken[0] <= time < spoken[1]:
                alpha *= primitive.get('context_opacity',1)
    return size,dx,dy,alpha,blur


def write_style_ass(path, words, layer, width, height, duration):
    primitives,events = make_primitives(words,layer,width,height,duration)
    return write_primitives_ass(path,primitives,events,layer,width,height)


def write_primitives_ass(path, primitives, events, layer, width, height):
    from .captions import _ass_time, _ass_color, _path_commands
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    rows,bytes_used = [],0
    for primitive in primitives:
        motion = primitive['motion']
        # Only positional trajectories need segments; libass interpolates each
        # segment. Static, fading, scale, gradient and blur paths are not frames.
        enter = primitive['motion_ms']/1000
        leave = primitive['out_ms']/1000
        keyframes = {primitive['start'],primitive['end']}
        if enter and motion!='none':
            if motion in {'pop','scatter_pop','word_random_position','word_pop_random','word_pop_random_pulse','letter_jump'}:
                keyframes.add(primitive['start']+enter*.55)
            if motion == 'lzx_word_pop':
                keyframes.update(primitive['start']+enter*fraction for fraction in (.18,.55,.78))
            keyframes.add(primitive['start']+enter)
        if leave and primitive['out']!='none':
            keyframes.add(primitive['end']-leave)
        keyframes=sorted(point for point in keyframes if primitive['start']<=point<=primitive['end'])
        if motion=='floating':
            count = max(1,math.ceil((primitive['end']-primitive['start'])*8))
            times = [primitive['start']+(primitive['end']-primitive['start'])*i/count for i in range(count+1)]
        else:
            points = {primitive['start'],primitive['end']}
            if enter and motion!='none':
                points.update(primitive['start']+enter*i/4 for i in range(1,5))
            if leave and primitive['out']!='none':
                points.add(primitive['end']-leave)
            times = sorted(point for point in points if primitive['start']<=point<=primitive['end'])
        positional = motion=='floating' or motion=='char_bounce' or motion.startswith('slide_') or primitive['out'].startswith('slide_')
        if primitive.get('follow_track'):
            positional=True
            times=sorted(set(times)|{t for t,_x,_y in primitive['follow_track']
                                      if primitive['start']<t<primitive['end']})
            times=sorted(set(times)|{t for t in primitive.get('follow_spoken',())
                                      if primitive['start']<t<primitive['end']})
        if not positional:
            times=[primitive['start'],primitive['end']]
        commands=_path_commands(primitive['native'])
        pieces=ass_gradient_pieces(primitive)
        for start,end in zip(times,times[1:]):
            if round(start*100) >= round(end*100): continue
            if primitive.get('follow_track'):
                from .caption_follow import visible_segment
                if not visible_segment(primitive,start,end): continue
            sa,xa,ya,aa,ba = motion_at(primitive,start)
            _sb,xb,yb,_ab,_bb = motion_at(primitive,max(start,end-1e-7) if primitive.get('follow_track') else end)
            ms = max(1,round((end-start)*1000))
            opacity = primitive['opacity']*layer['opacity']/100
            alpha = lambda value:f'&H{round(255*(1-opacity*value)):02X}&'
            x,y = primitive['x'],primitive['y']
            position = f'\\move({x+xa:.3f},{y+ya:.3f},{x+xb:.3f},{y+yb:.3f},0,{ms})'
            base = (f'\\an7{position}\\org({x+primitive.get("origin_x",primitive["width"]/2):.3f},{y+primitive.get("origin_y",primitive["height"]/2):.3f})'
                f'\\frz{primitive["rotation"]:.3f}\\bord{primitive["border"]:.3f}'
                f'\\3c{_ass_color(primitive["border_color"])}\\3a{alpha(aa*layer["outline_opacity"]/100)}\\shad0'
                f'\\fscx{sa*100:.3f}\\fscy{sa*100:.3f}\\blur{ba:.3f}\\1a{alpha(aa)}')
            checkpoints=sorted({start,end}|{time for time in keyframes if start<time<end})
            for color,clip in pieces:
                def clip_at(time):
                    fixed=primitive.get('screen_clip')
                    if clip is None:
                        return '\\clip('+','.join(str(round(v)) for v in fixed)+')' if fixed else ''
                    factor,dx,dy,_alpha,_blur=motion_at(primitive,time)
                    cx,cy=primitive['width']/2,primitive['height']/2
                    values=(x+dx+cx+(clip[0]-cx)*factor,y+dy+cy+(clip[1]-cy)*factor,
                            x+dx+cx+(clip[2]-cx)*factor,y+dy+cy+(clip[3]-cy)*factor)
                    if fixed:
                        values=(max(values[0],fixed[0]),max(values[1],fixed[1]),
                                min(values[2],fixed[2]),min(values[3],fixed[3]))
                        values=(values[0],values[1],max(values[0],values[2]),max(values[1],values[3]))
                    rounded=[round(v) for v in values]
                    return '\\clip('+','.join(map(str,rounded))+')'
                color_alpha = QtGui.QColor(color).alphaF()
                tags=base+f'\\1a{alpha(aa*color_alpha)}'+clip_at(start)
                previous=start
                for checkpoint in checkpoints[1:]:
                    size,_dx,_dy,fade,blur=motion_at(primitive,checkpoint)
                    if primitive.get('follow_spoken') and checkpoint==end:
                        # Do not interpolate the next word's opacity backwards.
                        fade=motion_at(primitive,max(start,end-1e-7))[3]
                    old_size,old_dx,old_dy,old_fade,old_blur=motion_at(primitive,previous)
                    if (size,_dx,_dy,fade,blur)==(old_size,old_dx,old_dy,old_fade,old_blur):
                        previous=checkpoint
                        continue
                    begin_ms=max(0,round((previous-start)*1000));end_ms=max(1,round((checkpoint-start)*1000))
                    tags+=(f'\\t({begin_ms},{end_ms},\\fscx{size*100:.3f}\\fscy{size*100:.3f}'
                        f'\\blur{blur:.3f}\\1a{alpha(fade*color_alpha)}\\3a{alpha(fade*layer["outline_opacity"]/100)}{clip_at(checkpoint)})')
                    previous=checkpoint
                tags+=f'\\1c{_ass_color(color)}\\p1'
                row = f'Dialogue: {primitive["z"]},{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{{{tags}}}{commands}{{\\p0}}'
                rows.append(row); bytes_used += len(row.encode('utf8'))
                if len(rows)>MAX_ROWS or bytes_used>64*1024*1024:
                    raise ValueError('字幕效果过于复杂（动画/渐变事件过多）；请缩短视频或减少发光、渐变、漂浮效果。')
    data_file(Path(path)).write_text(header+'\n'.join(rows)+'\n',encoding='utf8')
    return events


def render_design(layer, width, height, *, words=None, duration=None, time=None):
    """Same geometry/effects as export, sampled at an active-word moment."""
    from .captions import validate_words
    text = str(layer.get('text','')).split()
    if words is None:
        duration = duration or .6+len(text)*.35
        # Layout-only illustration, not an audio alignment. A fixed 1 ms floor
        # made long transcripts overflow short preview durations and produced
        # zero-length words. Keep every illustrative word inside the clock.
        span = duration * .98
        words = validate_words([{'text':word,'start':span*i/max(1,len(text)),
            'end':span*(i+1)/max(1,len(text))} for i,word in enumerate(text)],duration)
    else:
        words=validate_words(words,duration)
    from .caption_styles import uses_style_renderer
    if uses_style_renderer(layer):
        primitives,_events = make_primitives(words,layer,width,height,duration)
    else:
        from .caption_basic_render import make_primitives as make_basic
        primitives,_events = make_basic(words,layer,width,height,duration)
    image = QtGui.QImage(width,height,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    time = duration*.5 if time is None else min(max(0.,time),max(0.,duration-1e-7))
    try:
        for primitive in sorted(primitives,key=lambda item:item['z']):
            if not primitive['start']<=time<primitive['end']: continue
            if primitive.get('follow_track'):
                from .caption_follow import visible_segment
                if not visible_segment(primitive,time,time): continue
            size,dx,dy,alpha,blur = motion_at(primitive,time)
            painter.save()
            if primitive.get('screen_clip'):
                a,b,c,d=primitive['screen_clip']
                painter.setClipRect(QtCore.QRectF(a,b,c-a,d-b))
            painter.setOpacity(alpha*primitive['opacity'])  # Layer opacity applied by compose_layers.
            painter.translate(primitive['x']+dx,primitive['y']+dy)
            ox,oy=primitive.get('origin_x',primitive['width']/2),primitive.get('origin_y',primitive['height']/2)
            painter.translate(ox,oy)
            painter.rotate(-primitive['rotation']); painter.scale(size,size)
            painter.translate(-ox,-oy)
            palette=primitive['color'];bounds=primitive['native'].boundingRect()
            brush=QtGui.QBrush(QtGui.QColor(palette[0]))
            if len(palette)>1:
                vertical=primitive.get('gradient_direction') in {'vertical','linear_v'}
                gradient=QtGui.QLinearGradient(bounds.topLeft(),bounds.bottomLeft() if vertical else bounds.topRight())
                for index,color in enumerate(palette):gradient.setColorAt(index/(len(palette)-1),QtGui.QColor(color))
                brush=QtGui.QBrush(gradient)
            pen=QtGui.QPen(QtGui.QColor(primitive['border_color']),primitive['border'])
            def draw(target):
                target.setPen(pen if primitive['border'] else QtCore.Qt.PenStyle.NoPen)
                target.setBrush(brush);target.drawPath(primitive['native'])
            border = primitive['border']
            paint_blurred(painter, bounds.adjusted(-border,-border,border,border), blur, draw)
            painter.restore()
    finally: painter.end()
    from .caption_clip import apply_preview_mask
    apply_preview_mask(image,layer,width,height)
    return image,layer['font_max']
