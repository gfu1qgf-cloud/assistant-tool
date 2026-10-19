"""Classic word reveal uses the same appearance primitives and ASS writer as styles."""
from itertools import groupby
from qt_compat import QtCore, QtGui
from .caption_styles import effects, colors
from .caption_appearance import group_colors, active_segments, add_highlight_box, add_foreground
from .caption_display import display_settings, reveal_events, reveal_start


def make_primitives(words, layer, width, height, duration):
    from .caption_styles import uses_style_renderer
    if uses_style_renderer(layer):
        from .caption_style_render import make_primitives as make_style_primitives
        return make_style_primitives(words,layer,width,height,duration)
    from .captions import caption_events
    events=caption_events(words,layer,width,height,duration,native=True)
    style=effects(layer)
    scale=width/1080
    primitives=[]
    def add(path,x,y,start,end,color,opacity=1,**options):
        if end-start<.005 or opacity<=0: return
        bounds=path.boundingRect()
        primitives.append({'native':path,'x':x,'y':y,'width':bounds.width(),'height':bounds.height(),
            'start':start,'end':end,'color':colors(color),'opacity':opacity,'border':0,'border_color':layer['outline_color'],
            'blur':0,'rotation':0,'z':0,'motion':'none','motion_ms':0,'out':'none','out_ms':0,**options})
    palette=group_colors(layer,max(event['group'] for event in events)+1,words)
    if layer['background_enabled']:
        for _,items in groupby(events,key=lambda event:event['group']):
            group=list(items); bounds=QtCore.QRectF()
            for event in group:
                bounds=bounds.united(QtCore.QRectF(event['x'],event['y'],event['width'],event['height']))
            px,py=style['box_padding_x']*scale,style['box_padding_y']*scale
            bounds.adjust(-px,-py,px,py)
            if not style['box_adaptive_width']:
                bounds.setLeft(width*(layer['x']-layer['width']/2)/100)
                bounds.setRight(width*(layer['x']+layer['width']/2)/100)
            path=QtGui.QPainterPath(); path.addRoundedRect(QtCore.QRectF(0,0,bounds.width(),bounds.height()),
                layer['background_radius']*scale,layer['background_radius']*scale)
            color=style['bg_gradient_colors'] if style['bg_gradient_enabled'] else layer['background_color']
            add(path,bounds.x(),bounds.y(),group[0]['group_start'],group[0]['group_end'],color,
                layer['background_opacity']/100,z=-5,blur=style['box_blur']*scale,
                gradient_direction=style['bg_gradient_type'])
            if style['bg_gradient_highlight']:
                highlight=QtGui.QPainterPath(); highlight.addRoundedRect(
                    QtCore.QRectF(0,0,bounds.width(),bounds.height()/3),
                    layer['background_radius']*scale,layer['background_radius']*scale)
                add(highlight,bounds.x(),bounds.y(),group[0]['group_start'],group[0]['group_end'],
                    '#ffffff',.08,z=-5)
    for event in reveal_events(events, layer):
        start,stop=reveal_start(event,layer,events),event['end']; x,y=event['x'],event['y']; path=event['_native_path']
        pop=min(round(style['anim_in_duration']*1000),round((stop-start)*1000))
        options={'motion':style['anim_in_type'] if pop>=30 else 'none',
            'motion_ms':pop,'pop_start':style['pop_start'],'pop_peak':style['pop_peak']}
        if layer['shadow_enabled']:
            add(path,x+layer['shadow_x']*scale,y+layer['shadow_y']*scale,start,stop,layer['shadow_color'],
                layer['shadow_opacity']/100,z=-2,blur=layer.get('shadow_blur',0)*scale,**options)
        if not event.get('_character_piece') or event.get('_character_index') == 0:
            add_highlight_box(add,event,style,scale,options)
        base_color=(style['color_text'] if style['text_gradient_enabled'] and not layer.get('caption_group_colors')
                    else palette[event['group']])
        segments=active_segments(start,stop,event,base_color,style)
        for a,b,color in segments:
            phase={**options,'motion':options['motion'] if abs(a-start)<.005 else 'none'}
            add_foreground(add,path,x,y,a,b,color,1,layer,scale,
                {**phase,'gradient_direction':style['high_gradient_direction']
                 if color==style['color_high'] else style['text_gradient_direction']})
        display=display_settings(layer)
        if event.get('_character_piece') and display['caption_cursor']:
            cursor=QtGui.QPainterPath(); cursor.addRect(QtCore.QRectF(0,0,max(1,2*scale),event['height']))
            add(cursor,x+event['_cursor_edge'],y,event['start'],min(stop,event['_cursor_until']),
                display['caption_cursor_color'])
    return primitives,events
