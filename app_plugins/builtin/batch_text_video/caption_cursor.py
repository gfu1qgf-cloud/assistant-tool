"""Cursor geometry shared by Qt previews and finite native FFmpeg overlays."""
from bisect import bisect_right
from functools import lru_cache
import json
import math
from pathlib import Path
from uuid import uuid4
from qt_compat import QtCore,QtGui

DEFAULTS={'caption_cursor_mode':'bar','caption_cursor_image':'','caption_cursor_scale':100.,
          'caption_cursor_offset_x':0.,'caption_cursor_offset_y':0.,
          'caption_cursor_width':2.,'caption_cursor_height':100.,'caption_cursor_opacity':100.}


def normalize(layer):
    for key,value in DEFAULTS.items():layer.setdefault(key,value)
    if layer['caption_cursor_mode'] not in {'bar','image'}:raise ValueError('光标类型无效。')
    path=layer['caption_cursor_image']
    if not isinstance(path,str) or len(path)>4096 or any(c in path for c in '\x00\r\n'):raise ValueError('光标图片路径无效。')
    for key,low,high in (('caption_cursor_scale',5,500),('caption_cursor_offset_x',-1000,1000),('caption_cursor_offset_y',-1000,1000),
            ('caption_cursor_width',.1,100),('caption_cursor_height',5,300),('caption_cursor_opacity',0,100)):
        value=layer[key]
        if type(value) not in (float,int) or not math.isfinite(value) or not low<=value<=high:raise ValueError('光标大小或偏移超出范围。')


def image_enabled(layer):
    from .caption_display import display_settings
    display=display_settings(layer)
    return (display['caption_cursor'] and display['caption_display_mode']=='char'
            and layer.get('caption_cursor_mode','bar')=='image' and layer.get('caption_cursor_opacity',100)>0)


@lru_cache(maxsize=4)
def _read(path,stamp,size,height):
    reader=QtGui.QImageReader(path);reader.setAutoTransform(True)
    dimensions=reader.size()
    if dimensions.isEmpty() or dimensions.width()*dimensions.height()>64*1024*1024:raise ValueError('光标图片无法读取或尺寸过大。')
    width=max(1,round(height*dimensions.width()/dimensions.height()))
    if width*height>2*1024*1024:raise ValueError('光标图片过大，请降低光标大小。')
    reader.setScaledSize(QtCore.QSize(width,height));image=reader.read()
    if image.isNull():raise ValueError('光标图片无法读取：'+reader.errorString())
    return image.scaled(width,height,QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation)


def read_image(path,height):
    if not path:raise ValueError('请为逐字光标选择一张图片，或切换为竖线光标。')
    file=Path(path)
    try:stat=file.stat()
    except OSError as error:raise ValueError('光标图片不存在或无法读取：'+file.name) from error
    return _read(str(file.resolve()),stat.st_mtime_ns,stat.st_size,max(1,round(height)))


def prepare_image(layer,scale):
    if not image_enabled(layer):return None
    return read_image(layer.get('caption_cursor_image',''),layer['font_max']*scale*layer.get('caption_cursor_scale',100)/100)


def add_cursor(add,event,layer,scale,options=None,*,image=None):
    from .caption_display import display_settings
    display=display_settings(layer)
    if not event.get('_character_piece') or not display['caption_cursor']:return
    start,stop=event['start'],min(event['end'],event['_cursor_until'])
    if stop-start<.005:return
    options=options or {}
    follow={key:options[key] for key in ('follow_track','screen_clip') if key in options}
    opacity=(layer.get('caption_active_opacity',100)/100 if follow else 1)*layer.get('caption_cursor_opacity',100)/100
    if opacity<=0:return
    x=event['x']+event['_cursor_edge']+layer.get('caption_cursor_offset_x',0)*scale
    y=event['y']+layer.get('caption_cursor_offset_y',0)*scale
    extra={}
    if layer.get('caption_cursor_mode','bar')=='image':
        if image is None:image=prepare_image(layer,scale)
        width,height=image.width(),image.height();y+=(event['height']-height)/2
        extra={'cursor_image':layer['caption_cursor_image']}
    else:
        width=max(.1,layer.get('caption_cursor_width',2)*scale)
        height=event['height']*layer.get('caption_cursor_height',100)/100
        y+=(event['height']-height)/2
    path=QtGui.QPainterPath();path.addRect(0,0,width,height)
    add(path,x,y,start,stop,display['caption_cursor_color'],opacity,z=2,**follow,**extra)


def write_native_assets(ass_path,primitives,layer,width,height):
    """One small PNG and a seekable command track, never a rendered video."""
    cursors=[p for p in primitives if p.get('cursor_image')]
    if len(cursors)>80000:raise ValueError('图片光标事件过多，请分段导出。')
    path=Path(ass_path)
    if not cursors:
        path.with_suffix('.cursor.json').unlink(missing_ok=True);return
    target='cursor_'+uuid4().hex
    image=read_image(cursors[0]['cursor_image'],cursors[0]['height'])
    image_path=path.parent/(target+'.png');command_path=path.parent/(target+'.cmd')
    if not image.save(str(image_path),'PNG'):raise RuntimeError('光标图片准备失败。')
    from .caption_follow import offset_at
    rows=[];tracks={}
    for p in cursors:
        track=p.get('follow_track');points=[p['start'],p['end']]
        if track:
            if id(track) not in tracks:tracks[id(track)]=[v[0] for v in track]
            times=tracks[id(track)]
            points[1:1]=times[bisect_right(times,p['start']):bisect_right(times,p['end']-1e-9)]
        for start,end in zip(points,points[1:]):
            if end-start<1e-8:continue
            a=offset_at(track,start) if track else (0,0)
            b=offset_at(track,max(start,end-1e-8)) if track else (0,0)
            expressions=[f'{origin+first:.9f}+({last-first:.9f})*(t-{start:.9f})/{end-start:.9f}'
                         for origin,first,last in zip((p['x'],p['y']),a,b)]
            rows.append((start,1,f"{start:.9f} overlay@{target} x '{expressions[0]}', overlay@{target} y '{expressions[1]}';"))
        rows.append((p['end'],0,f"{p['end']:.9f} overlay@{target} x -100000, overlay@{target} y -100000;"))
    if len(rows)>160000:raise ValueError('图片光标事件过多，请分段导出。')
    commands='\n'.join(row[2] for row in sorted(rows))
    if len(commands.encode('utf8'))>32*1024*1024:raise ValueError('图片光标轨迹过大，请分段导出。')
    command_path.write_text(commands,encoding='utf8')
    metadata=dict(image=str(image_path),commands=str(command_path),target=target,
                  opacity=cursors[0]['opacity']*layer['opacity']/100)
    path.with_suffix('.cursor.json').write_text(json.dumps(metadata),encoding='utf8')


def append_native(args,filters,source,output,ass_path,layer,width,height,duration,fps,index):
    from .captions import filter_path
    metadata=json.loads(Path(ass_path).with_suffix('.cursor.json').read_text('utf8'))
    prefix=output+'_cursor';rate=f'{float(fps):.9f}'
    args.extend(('-i',metadata['image']))
    filters.append(f'[{index}:v]format=rgba,loop=loop=-1:size=1:start=0,setpts=N/({rate}*TB),trim=duration={duration:.9f},colorchannelmixer=aa={metadata["opacity"]:.9f}[{prefix}_sprite]')
    filters.append(f'color=c=black@0:s={width}x{height}:r={rate}:d={duration:.9f},format=rgba,sendcmd=filename={filter_path(metadata["commands"])}[{prefix}_canvas]')
    filters.append(f'[{prefix}_canvas][{prefix}_sprite]overlay@{metadata["target"]}=x=-100000:y=-100000:eval=frame:format=auto:eof_action=pass[{prefix}_full]')
    used=1;plane=prefix+'_full'
    from .caption_follow import enabled
    if enabled(layer):
        from .caption_clip import mask_image
        mask=mask_image(layer,width,height)
        if mask is None:
            mask=QtGui.QImage(width,height,QtGui.QImage.Format.Format_Grayscale8);mask.fill(0)
            painter=QtGui.QPainter(mask)
            painter.fillRect(QtCore.QRectF(width*(layer['x']-layer['width']/2)/100,height*(layer['y']-layer['height']/2)/100,
                                         width*layer['width']/100,height*layer['height']/100),QtGui.QColor('white'));painter.end()
        mask_path=Path(metadata['image']).with_suffix('.mask.png')
        if not mask.save(str(mask_path),'PNG'):raise RuntimeError('光标裁切遮罩保存失败。')
        args.extend(('-i',str(mask_path)));used+=1
        filters.extend((f'[{index+1}:v]format=gray,loop=loop=-1:size=1:start=0,setpts=N/({rate}*TB),trim=duration={duration:.9f}[{prefix}_mask]',
            f'[{plane}]split[{prefix}_rgb][{prefix}_alpha]',
            f'[{prefix}_alpha]extractplanes=a[{prefix}_a]',
            f'[{prefix}_a][{prefix}_mask]blend=all_mode=multiply:shortest=1[{prefix}_masked]',
            f'[{prefix}_rgb][{prefix}_masked]alphamerge=shortest=1[{prefix}_clipped]'))
        plane=prefix+'_clipped'
    filters.append(f'[{source}][{plane}]overlay=0:0:format=auto:eof_action=pass[{output}]')
    return used
