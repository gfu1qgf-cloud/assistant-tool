"""Project-owned canvas policy shared by the editor, posters and export."""
import math

DEFAULTS = {'aspect': 'portrait', 'canvas_width': 1080, 'canvas_height': 1920}
KEYS = tuple(DEFAULTS)
PRESETS = (
    ('portrait', '竖屏 9:16', 1080, 1920),
    ('landscape', '横屏 16:9', 1920, 1080),
    ('square', '方形 1:1', 1080, 1080),
    ('portrait_4_5', '竖屏 4:5', 1080, 1350),
    ('portrait_3_4', '竖屏 3:4', 1080, 1440),
    ('classic', '横屏 4:3', 1440, 1080),
)
CHOICES = tuple((f'{title} · {width}×{height}', key) for key, title, width, height in PRESETS) + (
    ('跟随底层背景尺寸（逐条任务）', 'source'), ('自定义宽高', 'custom'))
MAX_PIXELS = 16_777_216


def normalized(values):
    result = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    if result['aspect'] not in {item[0] for item in PRESETS} | {'source', 'custom'}:
        raise ValueError('画布比例无效，请选择常用比例、原素材规格或自定义宽高。')
    for key in ('canvas_width', 'canvas_height'):
        value = result[key]
        if type(value) is not int or not 64 <= value <= 8192 or value % 2:
            raise ValueError('自定义宽高须为64–8192之间的偶数像素，例如1080×1920。')
    if result['canvas_width']*result['canvas_height'] > MAX_PIXELS:
        raise ValueError('自定义画布超过1677万像素，请减小宽度或高度。')
    return result


def output_size(settings, media=None):
    values = normalized(settings)
    aspect = values['aspect']
    if aspect == 'custom': return values['canvas_width'], values['canvas_height']
    for key, _, width, height in PRESETS:
        if aspect == key: return width, height
    if media and media.get('video'):
        stream = media['video']
        width, height = int(stream['width']), int(stream['height'])
        rotation = stream.get('tags', {}).get('rotate', 0)
        rotation = next((s.get('rotation', rotation) for s in stream.get('side_data_list', []) if 'rotation' in s), rotation)
        if not math.isfinite(float(rotation)): raise ValueError('原素材的旋转信息无效。')
        if abs(float(rotation)) % 180 == 90: width, height = height, width
        if width <= 0 or height <= 0 or width*height > MAX_PIXELS:
            raise ValueError('原素材分辨率无效或过大，请选择常用比例或自定义画布。')
        return max(2, width//2*2), max(2, height//2*2)
    return 1080, 1920


def summary(settings):
    values = normalized(settings)
    if values['aspect'] == 'source': return '跟随底层背景（无背景时1080×1920）'
    width, height = output_size(values)
    factor = math.gcd(width, height)
    return f'{width//factor}:{height//factor} · {width}×{height}'
