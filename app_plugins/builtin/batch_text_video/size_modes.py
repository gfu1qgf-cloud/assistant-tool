"""Shared canvas sizing; stored manual geometry is never overwritten."""
MODES=(('固定比例','fixed'),('铺满画面（裁切）','cover'),('铺满画面（拉伸）','stretch'))


def normalize(layer):
    value=layer.setdefault('size_mode','fixed')
    if not isinstance(value,str) or value not in {v for _,v in MODES}:
        raise ValueError('尺寸方式须为固定比例、铺满画面（裁切）或铺满画面（拉伸）。')


def is_full(layer):return layer.get('size_mode','fixed') in {'cover','stretch'}


def effective(layer):
    if not is_full(layer):return layer
    # Also bypass face/corner anchoring in the derived render view. Its original
    # settings remain in the project and return when fixed sizing is selected.
    return dict(layer,x=50.,y=50.,width=100.,height=100.,position_reference='canvas',
        position_anchor_x='center',position_anchor_y='center',watermark_placement='free',_face_anchor=(.5,.5))


def source_rect(image,width,height):
    """Centered source crop, matching native scale-increase then crop."""
    from qt_compat import QtCore
    sw,sh=image.width(),image.height()
    factor=max(width/sw,height/sh)
    cw,ch=width/factor,height/factor
    return QtCore.QRectF((sw-cw)/2,(sh-ch)/2,cw,ch)


def native_scale(layer,width,height):
    mode=layer.get('size_mode','fixed')
    if mode=='stretch':return [f'scale={width}:{height}']
    if mode=='cover':
        # Discard only far-offscreen source pixels first, bounding even a very
        # wide panorama to at most twice the target area before final cropping.
        return [f"crop=w='min(iw,ih*{width}/{height}*2)':h='min(ih,iw*{height}/{width}*2)':exact=1",
            f'scale={width}:{height}:force_original_aspect_ratio=increase',f'crop={width}:{height}:exact=1']
    return [f'scale={width}:{height}:force_original_aspect_ratio=decrease']
