"""Opaque, quantized sRGB pairs; contrast is not a promise over transparent video.

Formula: https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html
No external model, global RNG, download, or new package is involved.
"""
import colorsys
from .basic import finite_number
from .contracts import ColorValue, NodeDefinition, NodeParameter, NodePort
from .value_nodes import deterministic_rng

FAMILIES = (('cherry', '樱桃红', .96), ('coral', '鲜珊瑚粉', .98),
            ('rose', '玫瑰粉', .93), ('fuchsia', '霓虹洋红', .86),
            ('lavender', '亮紫／兰花紫', .77), ('indigo', '蓝紫／鸢尾', .68),
            ('ocean', '宝石蓝', .61), ('sky', '晴空蓝', .55),
            ('cyan', '冰青／湖水青', .49), ('forest', '翡翠／薄荷绿', .43),
            ('navy', '庄重 · 深海蓝', .61), ('burgundy', '庄重 · 宝石酒红', .96),
            ('amber', '奶油玫瑰 · 暖色卡片', .96), ('terracotta', '蜜桃珊瑚 · 暖色卡片', .98))
RANDOM_FAMILIES = ('cherry', 'coral', 'rose', 'fuchsia', 'lavender', 'indigo',
                   'ocean', 'sky', 'cyan', 'forest', 'navy', 'burgundy')

# Deliberately selected roles, not arbitrary HSL triples. No brown/olive ink:
# warm paper is paired with berry/coral ink, not muddy gold. Video foregrounds
# deliberately carry visible colour, instead of four nearly identical off-whites.
# Deep jewel outlines preserve contrast without muting the bright foreground.
# name, dark ink, light body, light heading, coloured panel, tinted paper
CURATED = {
    'rose': (
        ('亮玫瑰', '#94104E', '#FFC4E0', '#FFE0EE', '#710A43', '#FFE0F0'),
        ('樱花糖粉', '#921242', '#FFAFD8', '#FFD1E8', '#700A44', '#FFE0ED'),
        ('覆盆子粉', '#870F60', '#FFC2EB', '#FFDBF4', '#640A50', '#FFE0F8'),
        ('绯红玫瑰', '#92103B', '#FFBDC9', '#FFDBE3', '#700B32', '#FFE0E8')),
    'ocean': (
        ('宝石海蓝', '#1244A0', '#95D0FF', '#C5E6FF', '#143780', '#E0EFFF'),
        ('电光钴蓝', '#173B98', '#ABC7FF', '#D2DFFF', '#182D78', '#E0E8FF'),
        ('蔚蓝浪花', '#084883', '#8ADCFF', '#C0EFFF', '#103D72', '#E0F5FF'),
        ('明亮矢车菊', '#30369A', '#B8C5FF', '#DDE4FF', '#222A73', '#E4E6FF')),
    'forest': (
        ('翡翠流光', '#005A43', '#8DF2C1', '#C2FFE0', '#004D38', '#DFFFF0'),
        ('明亮薄荷', '#00594E', '#83F5D8', '#C1FFEF', '#004C42', '#DFFFF6'),
        ('碧玉青绿', '#00584B', '#98EFD1', '#CAFFE9', '#004C3E', '#E0FFF1'),
        ('极光薄荷', '#00584E', '#8AF4E4', '#C4FFF5', '#004B45', '#DFFFF9')),
    'lavender': (
        ('电光薰衣草', '#612099', '#D4B3FF', '#EAD7FF', '#441273', '#F0E0FF'),
        ('明亮兰花', '#751A86', '#EAAEFF', '#F5D4FF', '#540C67', '#FCE0FF'),
        ('梦幻紫晶', '#5421A1', '#C8B8FF', '#E3DBFF', '#361475', '#EAE0FF'),
        ('紫罗兰糖霜', '#742088', '#F1BCFF', '#F9DDFF', '#580D68', '#FBE2FF')),
    'cherry': (
        ('樱桃气泡', '#951136', '#FFB5BA', '#FFD6DB', '#690B2B', '#FFE0E4'),
        ('亮红莓', '#92103A', '#FF9EAF', '#FFC9D7', '#65092A', '#FFE0E9'),
        ('红宝石光', '#90102B', '#FFC1C7', '#FFE0E3', '#660B24', '#FFE0E5'),
        ('石榴花红', '#93123E', '#FFB0CD', '#FFD5E5', '#680B35', '#FFE0ED')),
    'coral': (
        ('鲜珊瑚', '#901B3E', '#FFC1AD', '#FFE0D2', '#690B2B', '#FFE8E0'),
        ('珊瑚樱粉', '#921744', '#FFB7CD', '#FFD6E3', '#690C34', '#FFE0E9'),
        ('蜜桃花瓣', '#901D35', '#FFCCB8', '#FFE6D9', '#680C28', '#FFECE0'),
        ('粉珊瑚光', '#901948', '#FFC2D5', '#FFDFEB', '#670C38', '#FFE0ED')),
    'fuchsia': (
        ('霓虹洋红', '#80128D', '#F9A8FF', '#FDD6FF', '#53095F', '#FDE0FF'),
        ('亮丽玫紫', '#891071', '#FFB0EE', '#FFD8F6', '#5B0950', '#FFE0F8'),
        ('电光紫粉', '#73138F', '#EDA8FF', '#F7D4FF', '#490A64', '#F7E0FF'),
        ('芭比花粉', '#8B106E', '#FFB1E5', '#FFD9F1', '#5F094C', '#FFE0F5')),
    'indigo': (
        ('鸢尾星光', '#382697', '#B9B7FF', '#DEDDFF', '#241773', '#E8E0FF'),
        ('蓝紫极光', '#29349A', '#AFC5FF', '#D6E3FF', '#1B2275', '#E0E9FF'),
        ('星河紫蓝', '#492194', '#C8AEFF', '#E3D5FF', '#301572', '#EEE0FF'),
        ('靛蓝流星', '#26379B', '#A7C2FF', '#D1E0FF', '#192576', '#E0EBFF')),
    'sky': (
        ('晴空亮蓝', '#075083', '#7AE2FF', '#BAF1FF', '#0D3F76', '#E0F7FF'),
        ('冰川蓝', '#094B90', '#96D8FF', '#CAF0FF', '#103878', '#E0F3FF'),
        ('海岛天蓝', '#00517E', '#87E9FF', '#C7F6FF', '#073F70', '#DFFAFF'),
        ('蓝色晴日', '#0B4791', '#A3D5FF', '#D1EAFF', '#123375', '#E0F0FF')),
    'cyan': (
        ('冰青流光', '#005565', '#61F1EC', '#B9FFFA', '#06445B', '#DFFFFC'),
        ('湖水青蓝', '#00516E', '#77ECFF', '#BDF8FF', '#073E63', '#DFFAFF'),
        ('热带水光', '#005650', '#72F4D6', '#B9FFEB', '#03483F', '#DFFFF4'),
        ('水晶青', '#00535C', '#9AF5EB', '#CDFFF8', '#03474C', '#E0FFFA')),
    'navy': (
        ('深海珍珠', '#163A82', '#E2EDFF', '#A8D4FF', '#17315D', '#E0EBFF'),
        ('午夜冰蓝', '#173579', '#CDE5FF', '#F1F7FF', '#172E57', '#E0EEFF'),
        ('皇家蓝银', '#193885', '#DBDEFF', '#FAF7FF', '#1C2D63', '#E3E5FF'),
        ('深蓝星辉', '#103D78', '#BDEAFF', '#ECFAFF', '#11345B', '#DFF4FF')),
    'burgundy': (
        ('宝石酒红', '#800D35', '#FFDCE7', '#FFB2D1', '#650A30', '#FFE0EB'),
        ('红丝绒', '#850F33', '#FFE0E6', '#FFBBC8', '#690B2B', '#FFE0E7'),
        ('庄重莓红', '#791044', '#FFD2EB', '#FFF0F8', '#600B3B', '#FFE0F1'),
        ('绯红珍珠', '#82112C', '#FFE2E6', '#FFC0C9', '#660D25', '#FFE2E6')),
    'amber': (
        ('香槟玫瑰', '#742E4D', '#FFF5DF', '#FFE5AA', '#702B49', '#FFEDCD'),
        ('奶油莓果', '#702844', '#FFF7E7', '#FFE8BF', '#722B46', '#FBEACF'),
        ('杏桃覆盆子', '#702B58', '#FFF4E7', '#FFDDB6', '#682C53', '#FFE7D4'),
        ('奶油樱花', '#80163F', '#FFF1E7', '#FFD3DE', '#690D36', '#FFE9D9')),
    'terracotta': (
        ('蜜桃玫瑰', '#7A2E4B', '#FFF2EB', '#FFDAC8', '#702B49', '#FFE2D6'),
        ('珊瑚柔粉', '#72313A', '#FFF0F0', '#FFD4DA', '#70323C', '#FFE1E5'),
        ('樱桃奶杏', '#732A46', '#FFF2E8', '#FFDDC4', '#682C48', '#FFE5D5'),
        ('蜜桃红莓', '#881944', '#FFE5DB', '#FFC9CF', '#690D36', '#FFE5DC')),
}


def luminance(color):
    if not isinstance(color, ColorValue): raise TypeError('配色需要 sRGB 颜色对象。')
    if color.alpha != 1: raise ValueError('透明颜色依赖底下的视频，不能承诺对比度；请使用不透明颜色。')
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
              for v in (color.red, color.green, color.blue)]
    return sum(a*b for a, b in zip(values, (.2126, .7152, .0722)))


def contrast_ratio(a, b):
    low, high = sorted((luminance(a), luminance(b)))
    return (high + .05) / (low + .05)


def hls(hue, lightness, saturation):
    # Check the actual 8-bit colours that will reach QColor/libass, not ideal floats.
    values = colorsys.hls_to_rgb(hue % 1, lightness, saturation)
    return ColorValue(*(round(v*255)/255 for v in values))


def curated_colors(family, style, variation, threshold, rng):
    name, ink, body, heading, panel, paper = rng.choice(CURATED[family])
    # A little controlled variation, never +/-30 degrees of random colour drift.
    limit = 3 if family in {'amber', 'terracotta'} else 6
    shift = rng.uniform(-min(variation, limit), min(variation, limit)) / 360

    def adjusted(value):
        color = ColorValue.from_hex(value)
        hue, lightness, saturation = colorsys.rgb_to_hls(color.red, color.green, color.blue)
        return hls(hue + shift, lightness, saturation)

    text, title, background = (map(adjusted, (ink, ink, paper)) if style == 'light'
                               else map(adjusted, (body, heading, panel)))
    # Validate quantized output after the small shift. Darken only the already
    # selected ink/panel, never switch to black or generate another arbitrary hue.
    for step in range(41):
        ratio = min(contrast_ratio(text, background), contrast_ratio(title, background))
        if ratio >= threshold:
            return text, title, background, ratio, name
        def darker(value):
            hue, lightness, saturation = colorsys.rgb_to_hls(value.red, value.green, value.blue)
            return hls(hue, lightness - .004, saturation)
        if style == 'light':
            text, title = darker(text), darker(title)
        else:
            background = darker(background)
    raise ValueError('精选色板未达到所选对比度，请调整对比度或色系。')


def result(text, title, background, ratio, name):
    return {'text': text, 'title': title, 'background': background,
        'outline': background, 'contrast': ratio,
        'description': f'{name}；正文 {text.hex_rgb} / 标题 {title.hex_rgb} / '
            f'底色与推荐描边 {background.hex_rgb}；最低对比度 {ratio:.3f}:1（不透明原色）'}


def palette(inputs, params, context):
    rng = deterministic_rng(inputs, params, context)
    family = params['family']
    hues = {key: hue for key, _title, hue in FAMILIES}
    if family == 'random': family = rng.choice(RANDOM_FAMILIES)
    if family == 'custom':
        base = inputs.get('base') or ColorValue.from_hex(params['base'])
        if not isinstance(base, ColorValue): raise TypeError('基准色需要 RGB／RGBA 颜色。')
        hue = colorsys.rgb_to_hls(base.red, base.green, base.blue)[0]
    elif family in hues:
        hue = hues[family]
    else:
        raise ValueError('文字配色色系无效。')
    variation = finite_number(params['variation'])
    if not 0 <= variation <= 30: raise ValueError('色相变化须为0–30度。')
    threshold = finite_number(params['minimum'])
    if not 4.5 <= threshold <= 7: raise ValueError('文字配色最低对比度须为4.5–7。')
    style = params['style']
    if style not in {'light', 'colored'}: raise ValueError('底色风格无效。')
    if family != 'custom':
        return result(*curated_colors(family, style, variation, threshold, rng))
    hue = (hue + rng.uniform(-variation, variation)/360) % 1
    saturation = rng.uniform(.58, .78)
    # A bounded tonal search preserves the requested hue. No black/gray fallback.
    for step in range(81):
        fraction = step/80
        if style == 'light':
            background = hls(hue, .90 + .025*fraction, min(.9, saturation+.1))
            text = hls(hue, .32 - .20*fraction, saturation)
            title = hls(hue+.06, .29 - .18*fraction, min(.9, saturation+.1))
        else:
            background = hls(hue, .38 - .20*fraction, saturation)
            text = hls(hue, .97, .35)
            title = hls(hue+.06, .98, .45)
        ratio = min(contrast_ratio(text, background), contrast_ratio(title, background))
        if ratio >= threshold:
            return result(text, title, background, ratio, '自选色系')
    raise ValueError('此色系未找到满足对比度的配色，请选择浅色底色或降低至4.5；没有退回黑色。')


def measure(inputs, params, _context):
    threshold = finite_number(params['minimum'])
    if not 1 <= threshold <= 21: raise ValueError('对比度门槛须为1–21。')
    value = contrast_ratio(inputs['text'], inputs['background'])
    return {'contrast': value, 'readable': value >= threshold}


def register_palette_nodes(registry):
    registry.register('core', NodeDefinition('core.text_palette', '文字配色 · 色系与可读底色', category='颜色/文字配色',
        inputs=(NodePort('base', '自选色系基准 · 自选模式使用', 'color', required=False),
                NodePort('seed', '随机种子', 'text', required=False),
                NodePort('iteration', '循环次数 · 可选', 'number', required=False)),
        outputs=(NodePort('text', '正文颜色', 'color'), NodePort('title', '标题颜色', 'color'),
                 NodePort('background', '底色 · 不含黑色', 'color'), NodePort('contrast', '最小对比度', 'number'),
                 NodePort('description', '配色说明', 'text'), NodePort('outline', '推荐描边颜色', 'color')),
        parameters=(NodeParameter('family', '色系', 'choice', 'random', choices=(('random', '每项随机色系'),) +
                         tuple((key, title) for key, title, _ in FAMILIES) + (('custom', '自选基准色'),)),
            NodeParameter('base', '自选基准色', 'color', '#c74b68'),
            NodeParameter('style', '配色用途', 'choice', 'colored', choices=(('colored', '视频推荐 · 鲜亮文字／深彩描边'), ('light', '浅色卡片 · 深色文字／浅底'))),
            NodeParameter('minimum', '最低对比度', 'number', 7., 4.5, 7),
            NodeParameter('variation', '色相微调上限（度）', 'number', 6., 0, 30),
            NodeParameter('seed', '种子', default='0'),
            NodeParameter('scope', '随机范围', 'choice', 'task', choices=(('task', '按任务身份'), ('fixed', '固定'), ('iteration', '按当前循环次数')))),
        build=palette, description='14个色系、56套精选色板；自动随机从12个鲜亮／庄重色系中等概率选择，不再局限4种。'
            '包括樱桃红、珊瑚粉、玫瑰、洋红、亮紫、蓝紫、宝石蓝、晴空蓝、冰青、翡翠，以及庄重深蓝／酒红。'
            '视频推荐模式加强正文颜色差异，配高对比深彩描边；浅色卡片模式保留深色正文，不要把它当成鲜亮文字模式。'
            '排除土黄／焦糖棕／橄榄色；奶油／蜜桃卡片只在手动选择时使用，配莓果／珊瑚文字。'
            '正文接文字颜色，推荐描边接描边颜色；需要底框时把底色接底色并开启底框。相同种子可复现，默认7:1，不输出黑底。'
            '独立随机仍可能重复，固定相同种子与范围会得到相同配色；需要逐任务变化请使用按任务身份，或接入不同种子。'
            '精选色板色相微调最多6度（两类暖色卡片最多3度）；自选模式保留所设范围。'
            '对比度按不透明原色计算，透明度、过细描边与视频画面仍会影响可读性。'))
    registry.register('core', NodeDefinition('core.color_contrast', '颜色对比度检查', category='颜色/文字配色',
        inputs=(NodePort('text', '文字颜色', 'color'), NodePort('background', '底色', 'color')),
        outputs=(NodePort('contrast', '对比度', 'number'), NodePort('readable', '达到门槛', 'boolean')),
        parameters=(NodeParameter('minimum', '对比度门槛', 'number', 4.5, 1, 21),), build=measure,
        description='按sRGB相对亮度计算，不四舍五入再判断。不透明原色检查，不保证半透明视频合成后的结果。'))
