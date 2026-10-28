"""One static feather mask, shared by Qt preview and native FFmpeg composition."""
from functools import lru_cache
from pathlib import Path
from qt_compat import QtCore, QtGui

MAX_CACHED_MASK_BYTES = 8*1024*1024


def geometry(layer, width, height):
    from .caption_follow import enabled
    amount = layer.get('caption_clip_feather',0)
    if not enabled(layer) or amount <= 0:
        return None
    box=QtCore.QRectF(width*(layer['x']-layer['width']/2)/100,
        height*(layer['y']-layer['height']/2)/100,width*layer['width']/100,height*layer['height']/100).intersected(
            QtCore.QRectF(0,0,width,height))
    if box.isEmpty():
        return None
    return ((box.left(),box.top(),box.right(),box.bottom()), min(box.width(),box.height())*amount/100)


def _make_mask(width, height, clip, feather, alpha):
    import numpy as np
    left,top,right,bottom=clip
    x=np.arange(width,dtype=np.float32)+.5
    y=np.arange(height,dtype=np.float32)+.5
    x=np.minimum((x-left)/feather,(right-x)/feather)
    y=np.minimum((y-top)/feather,(bottom-y)/feather)
    values=np.minimum(y[:,None],x[None,:])
    np.clip(values,0,1,out=values)
    values=np.rint(values*255).astype(np.uint8)
    if alpha:
        # Premultiplied white: all four channels equal the mask alpha.
        pixels=np.repeat(values[:,:,None],4,axis=2)
        return QtGui.QImage(pixels.data,width,height,pixels.strides[0],
                           QtGui.QImage.Format.Format_ARGB32_Premultiplied).copy()
    return QtGui.QImage(values.data,width,height,values.strides[0],QtGui.QImage.Format.Format_Grayscale8).copy()


@lru_cache(maxsize=4)
def _cached_mask(width, height, clip, feather, alpha):
    return _make_mask(width,height,clip,feather,alpha)


def mask_image(layer, width, height, *, alpha=False):
    value=geometry(layer,width,height)
    if value is None:
        return None
    clip,feather=value
    build=_cached_mask if width*height*(4 if alpha else 1)<=MAX_CACHED_MASK_BYTES else _make_mask
    return build(width,height,clip,feather,alpha)


def apply_preview_mask(image, layer, width, height):
    mask=mask_image(layer,width,height,alpha=True)
    if mask is None:
        return
    painter=QtGui.QPainter(image)
    try:
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0,0,mask)
    finally:
        painter.end()


def prepare_plane(filters, name, ass_path, layer, width, height, duration, fps, mask_input):
    """Return an optional single-frame PNG input; every video clock is finite."""
    from .captions import filter_path
    rate=f'{float(fps):.9f}'
    mask=mask_image(layer,width,height)
    if mask is None:
        filters.append(f'color=c=black@0:s={width}x{height}:r={rate}:d={duration:.9f},'
                       f'format=rgba,ass=filename={filter_path(ass_path)}:alpha=1[{name}]')
        return None
    from model.AppDataStorage import data_file
    # RGB rendering must not apply libass's default TV-range YCbCr mapping.
    rgb_ass=Path(ass_path).with_suffix('.clip.ass')
    script=data_file(ass_path).read_text('utf8')
    script=script.replace('WrapStyle: 2\n','WrapStyle: 2\nYCbCr Matrix: None\n',1)
    data_file(rgb_ass).write_text(script,encoding='utf8')
    # Derive true coverage from black/white RGB renders. The RGBA ass filter
    # squares translucent fill alpha; using that alpha would darken the text.
    for color in ('black','white'):
        filters.append(f'color=c={color}:s={width}x{height}:r={rate}:d={duration:.9f},'
                       f'format=rgb24,setparams=range=full,ass=filename={filter_path(rgb_ass)}[{name}_{color}]')
    path=Path(ass_path).with_suffix('.clip.png')
    if not mask.save(str(path),'PNG'):
        raise RuntimeError('字幕裁切柔化遮罩保存失败。')
    # Decode the static mask once, then reuse one frame in FFmpeg RAM.
    filters.extend((
        f'[{mask_input}]format=gray,loop=loop=-1:size=1:start=0,setpts=N/({rate}*TB),'
        f'trim=duration={duration:.9f},setsar=1[{name}_mask]',
        f'[{name}_black]split[{name}_rgb][{name}_black_alpha]',
        f'[{name}_black_alpha]format=gbrp,extractplanes=r[{name}_black_r]',
        f'[{name}_white]format=gbrp,extractplanes=r[{name}_white_r]',
        f"[{name}_white_r][{name}_black_r]blend=all_expr='255-A+B':shortest=1[{name}_coverage]",
        f'[{name}_coverage]split[{name}_alpha][{name}_mask_alpha]',
        f'[{name}_rgb][{name}_alpha]alphamerge=shortest=1,setparams=alpha_mode=premultiplied,format=rgba:alpha_modes=straight[{name}_straight]',
        f'[{name}_mask_alpha][{name}_mask]blend=all_mode=multiply:shortest=1[{name}_soft_alpha]',
        f'[{name}_straight][{name}_soft_alpha]alphamerge=shortest=1[{name}]'))
    return path


def append_caption(args, filters, source, output, ass_path, layer, width, height, duration, fps, next_index):
    from .captions import filter_path
    if geometry(layer,width,height) is None:
        filters.append(f'[{source}]ass=filename={filter_path(ass_path)}[{output}]')
        return 0
    name=output+'_caption'
    mask=prepare_plane(filters,name,ass_path,layer,width,height,duration,fps,f'{next_index}:v')
    args.extend(('-i',str(mask)))
    filters.append(f'[{source}][{name}]overlay=0:0:format=auto:eof_action=pass[{output}]')
    return 1
