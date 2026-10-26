"""Decoded dimensions, including MOV clean aperture, before raw frame reads."""
import math


def decoded_size(stream):
    width, height = int(stream['width']), int(stream['height'])
    crop = next((v for v in stream.get('side_data_list', [])
                 if v.get('side_data_type') == 'Frame Cropping'), {})
    sides = [int(crop.get('crop_'+side, 0)) for side in ('left', 'right', 'top', 'bottom')]
    if any(v < 0 for v in sides): raise ValueError('视频裁切信息无效。')
    width = int(stream.get('display_width', width-sides[0]-sides[1]))
    height = int(stream.get('display_height', height-sides[2]-sides[3]))
    if width <= 0 or height <= 0: raise ValueError('视频裁切后的画幅无效。')
    rotation = stream.get('tags', {}).get('rotate', 0)
    rotation = next((v['rotation'] for v in stream.get('side_data_list', []) if 'rotation' in v), rotation)
    rotation = float(rotation)
    if not math.isfinite(rotation): raise ValueError('视频旋转信息无效。')
    if abs(rotation) % 180 == 90: width, height = height, width
    return width, height
