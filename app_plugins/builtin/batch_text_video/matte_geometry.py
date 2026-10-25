"""Fractional source-pixel matte shifts shared by Qt, AI and native video."""
import math

DEFAULTS = {'key_matte_grow': 0.0}


def normalize(layer):
    layer.setdefault('key_matte_grow', 0.0)
    value = layer['key_matte_grow']
    if type(value) not in (int, float) or not math.isfinite(value) or not -20 <= value <= 20:
        raise ValueError('遮罩收缩／扩展须为-20至20个源素材像素，可使用小数。')


def shift(alpha, amount):
    if not amount: return alpha
    import cv2
    import numpy as np
    size = abs(float(amount)); whole = math.floor(size); fraction = size-whole
    method = cv2.erode if amount < 0 else cv2.dilate
    kernel = np.ones((3, 3), np.uint8)
    value = method(alpha.astype(np.float32), kernel, iterations=whole,
                   borderType=cv2.BORDER_REPLICATE) if whole else alpha.astype(np.float32)
    if fraction:
        next_value = method(value, kernel, borderType=cv2.BORDER_REPLICATE)
        value = value*(1-fraction)+next_value*fraction
    return value


def add_native(filters, source, target, amount):
    size = abs(float(amount)); whole = math.floor(size); fraction = size-whole
    method = 'erosion' if amount < 0 else 'dilation'
    operations = [method+'=coordinates=255']*whole
    prefix = ','.join(operations) if operations else 'null'
    if not fraction:
        filters.append(f'[{source}]{prefix},format=gray[{target}]')
    else:
        filters.append(f'[{source}]{prefix},split=2[{target}_base][{target}_next]')
        filters.append(f'[{target}_next]{method}=coordinates=255,format=gray[{target}_shift]')
        filters.append(f"[{target}_base][{target}_shift]blend=all_expr='A*{1-fraction:.9f}+B*{fraction:.9f}',format=gray[{target}]")
