"""Original looks/generators/geometry compiled into the one final FFmpeg job."""
import copy
from dataclasses import asdict
from fractions import Fraction
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from model.AppDataStorage import data_file, data_io
from .basic import finite_number
from .contracts import (ColorValue, NodeDefinition, NodeParameter, NodePort, VideoComposition,
                        FilterOperation, ResourceRef, VideoFilterFragment)
from .source_composition import SourceStage
from .video_stream import VideoStream
from .value_nodes import input_or

LOOKS = (('warm', '暖色', '柔和的暖色增益，不是Log转709'), ('cool', '冷色', '降低红色、轻微增加蓝色'),
         ('soft', '柔和', '降低对比和饱和度'), ('vintage', '复古', '暖色、抬黑与低饱和'),
         ('mono', '黑白', '按亮度生成中性灰'))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def resolved(params, inputs, numeric=(), colors=()):
    values = copy.deepcopy(params)
    for key in numeric:
        values[key] = finite_number(input_or(inputs, params, key))
    for key in colors:
        value = inputs.get(key)
        if value is not None:
            if not isinstance(value, ColorValue): raise TypeError('颜色输入必须是RGBA对象。')
            values[key] = value.hex_rgb + f'{round(value.alpha*255):02x}'
    return values


def background_values(params):
    for key, high in (('width', 8192), ('height', 8192)):
        if type(params[key]) is not int or not 1 <= params[key] <= high:
            raise ValueError('背景尺寸须为1–8192的整数。')
    if not 0 < finite_number(params['duration']) <= 86400 or not 1 <= finite_number(params['fps']) <= 240:
        raise ValueError('背景时长／帧率无效。')
    if params['style'] not in {'solid', 'linear', 'radial'}: raise ValueError('背景样式无效。')
    for key in ('color', 'color2'):
        ColorValue.from_hex(params[key])
    for key in ('x0', 'x1', 'y0', 'y1'):
        if not 0 <= finite_number(params[key]) <= 100: raise ValueError('渐变端点须为0–100%。')
    return params


def color_filter(value):
    color = ColorValue.from_hex(value)
    return '0x' + color.hex_rgb[1:] + f'@{color.alpha:.8f}'


def build_background(inputs, params, _context):
    values = resolved(params, inputs, ('duration', 'fps'), ('color', 'color2'))
    for key in ('width', 'height'):
        number = finite_number(input_or(inputs, params, key))
        if number != int(number): raise ValueError('背景尺寸不能含小数。')
        values[key] = int(number)
    background_values(values)
    info = {'duration': values['duration'], 'audio': False, 'video': {'width': values['width'], 'height': values['height'],
            'avg_frame_rate': str(Fraction(str(values['fps']))), 'color_transfer': 'bt709', 'color_space': 'bt709', 'pix_fmt': 'rgba'}}
    return {'video': VideoStream(VideoComposition('core.background', (), json_text(values), json_text(info))).verify()}


def compile_background(recipe, builder, *, need_audio=True):
    values = background_values(recipe.parameters)
    width, height = values['width'], values['height']
    prefix = builder.namespace()
    common = f's={width}x{height}:r={values["fps"]:.9f}:d={values["duration"]:.9f}'
    if values['style'] == 'solid':
        source = f'color=c={color_filter(values["color"])}:{common},format=rgba'
    else:
        # Every endpoint and seed is explicit, never FFmpeg's random defaults.
        points = {key: round(values[key]/100*((width if key[0] == 'x' else height)-1)) for key in ('x0', 'y0', 'x1', 'y1')}
        if points['x0'] == points['x1'] and points['y0'] == points['y1']:
            raise ValueError('渐变起点和终点不能相同。')
        source = f'gradients={common}:c0={color_filter(values["color"])}:c1={color_filter(values["color2"])}:n=2:' + \
            ':'.join(f'{key}={value}' for key, value in points.items()) + f':type={values["style"]}:speed=0:seed=1,format=rgba'
    builder.filters.append(source + f'[{prefix}background]')
    return SourceStage(prefix+'background', None, recipe.info)


def transform_values(params):
    for key in ('x', 'y', 'rotation', 'anchor_x', 'anchor_y'):
        if abs(finite_number(params[key])) > 100000: raise ValueError('变换位置／角度超出范围。')
    for key in ('scale_x', 'scale_y'):
        if not .001 <= finite_number(params[key]) <= 100: raise ValueError('变换缩放须为0.001–100。')
    if params['unit'] not in {'percent', 'pixels'} or params['canvas'] not in {'keep', 'custom'}:
        raise ValueError('变换单位／画布模式无效。')
    if params['interpolation'] not in {'neighbor', 'bilinear', 'bicubic', 'lanczos'}: raise ValueError('缩放插值无效。')
    if params['edge'] not in {'transparent', 'solid'}: raise ValueError('边缘模式无效。')
    ColorValue.from_hex(params['color'])
    return params


def build_transform(inputs, params, context):
    from .render import probe_resource
    values = transform_values(resolved(params, inputs, ('x', 'y', 'scale_x', 'scale_y', 'rotation', 'anchor_x', 'anchor_y'), ('color',)))
    source = inputs['video']; source.verify()
    info = copy.deepcopy(probe_resource(source, context, getattr(context, 'ffmpeg', '')))
    if values['canvas'] == 'custom':
        for key in ('width', 'height'):
            if type(values[key]) is not int or not 1 <= values[key] <= 8192: raise ValueError('输出画布尺寸须为1–8192。')
            info['video'][key] = values[key]
        info['video'].pop('display_width', None); info['video'].pop('display_height', None)
    return {'video': VideoStream(VideoComposition('core.transform', (source,), json_text(values), json_text(info))).verify()}


def transform_fragment(video, values, info, output_size):
    values = transform_values(values)
    spec = info['video']; width, height = spec.get('display_width', spec['width']), spec.get('display_height', spec['height'])
    out_w, out_h = output_size
    sw, sh = max(1, round(width*values['scale_x'])), max(1, round(height*values['scale_y']))
    angle = math.radians(values['rotation'])
    rw, rh = max(1, math.ceil(abs(sw*math.cos(angle))+abs(sh*math.sin(angle)))), max(1, math.ceil(abs(sh*math.cos(angle))+abs(sw*math.sin(angle))))
    if max(sw, sh, rw, rh) > 16384 or rw*rh > 64*1024*1024: raise ValueError('变换后画面过大，请降低缩放。')
    ax, ay = (values['anchor_x']/100-.5)*sw, (values['anchor_y']/100-.5)*sh
    # Anchor coordinates describe the displayed element after mirroring.
    anchor_x, anchor_y = rw/2 + ax*math.cos(angle)-ay*math.sin(angle), rh/2 + ax*math.sin(angle)+ay*math.cos(angle)
    x = values['x']*out_w/100 if values['unit'] == 'percent' else values['x']
    y = values['y']*out_h/100 if values['unit'] == 'percent' else values['y']
    fps = float(Fraction(spec.get('avg_frame_rate') or '25/1'))
    geometry = f'format=rgba,scale={sw}:{sh}:flags={values["interpolation"]},setsar=1'
    if values.get('flip_x') == 'yes': geometry += ',hflip'
    if values.get('flip_y') == 'yes': geometry += ',vflip'
    if abs(values['rotation']) > 1e-9: geometry += f',rotate={angle:.10f}:ow={rw}:oh={rh}:c=none:bilinear={int(values["interpolation"] != "neighbor")}'
    else: rw, rh = sw, sh
    background = '0x000000@0' if values['edge'] == 'transparent' else color_filter(values['color'])
    lines = (f'[input]{geometry}[transformed]',
        f'color=c={background}:s={out_w}x{out_h}:r={fps:.9f}:d={info["duration"]:.9f},format=rgba[canvas]',
        f'[canvas][transformed]overlay=x={x-anchor_x:.8f}:y={y-anchor_y:.8f}:shortest=1:format=auto[output]')
    return VideoFilterFragment(video, (), lines)


def compile_transform(recipe, builder, *, need_audio=True):
    from .filter_graph import append_chain, VideoFilterChain
    stage = builder.build(recipe.inputs[0], need_audio=need_audio)
    output_size = (recipe.info['video']['width'], recipe.info['video']['height'])
    fragment = transform_fragment(recipe.inputs[0], recipe.parameters, stage.info, output_size)
    prefix = builder.namespace(); tag = prefix+'transformed'
    append_chain(VideoFilterChain((fragment,)), builder.command, builder.filters, stage.video, builder.input_count(), tag, prefix)
    return SourceStage(tag, stage.audio, recipe.info)


def look_rgb(style, red, green, blue):
    luminance = .2126*red+.7152*green+.0722*blue
    if style == 'warm': result = (red*1.05+.012, green*1.01, blue*.94)
    elif style == 'cool': result = (red*.94, green*1.015, blue*1.04+.01)
    elif style == 'soft': result = tuple((luminance+(v-luminance)*.82)*.9+.05 for v in (red, green, blue))
    elif style == 'vintage': result = ((luminance+(red-luminance)*.78)*.96+.04,
                                      (luminance+(green-luminance)*.78)*.94+.035,
                                      (luminance+(blue-luminance)*.78)*.88+.025)
    elif style == 'mono': result = (luminance,)*3
    else: raise ValueError('未知原创LUT预设。')
    return tuple(min(1, max(0, value)) for value in result)


def read_cube(path):
    file = Path(path)
    if file.suffix.lower() != '.cube' or file.stat().st_size > 8*1024*1024: raise ValueError('仅接受不超过8MB的.cube LUT。')
    size, values = None, []
    with data_io.open(file, 'rb') as reader:
        raw = reader.read(8*1024*1024+1)
    if len(raw) > 8*1024*1024: raise ValueError('LUT超过8MB，未读取全部数据。')
    for line in raw.decode('utf-8-sig').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line: continue
        words = line.split()
        if words[0] == 'TITLE': continue
        if words[0] == 'LUT_3D_SIZE':
            if size is not None or len(words) != 2 or not words[1].isdigit() or not 2 <= int(words[1]) <= 65:
                raise ValueError('3D LUT尺寸须为2–65，不能重复声明。')
            size = int(words[1]); continue
        if words[0] in {'DOMAIN_MIN', 'DOMAIN_MAX'}:
            expected = 0 if words[0] == 'DOMAIN_MIN' else 1
            if len(words) != 4 or any(float(v) != expected for v in words[1:]): raise ValueError('第一版仅接受0–1域的SDR LUT。')
            continue
        if size is None or len(words) != 3 or any(not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', v) for v in words):
            raise ValueError('LUT存在未知指令或损坏数据；不执行文件中的代码。')
        row = tuple(float(v) for v in words)
        if any(not math.isfinite(v) or not -16 <= v <= 16 for v in row) or len(values) >= size**3:
            raise ValueError('LUT数据超范围或条数过多。')
        values.append(row)
    if size is None or len(values) != size**3: raise ValueError('LUT数据不完整。')
    return size, values


def lut_rows(style, size=17):
    return [look_rgb(style, r/(size-1), g/(size-1), b/(size-1))
            for b in range(size) for g in range(size) for r in range(size)]


def prepare_lut(_task, params, _services):
    # Connected paths are evaluated later and override this fallback parameter.
    # Stamp an existing fallback, but validate only the file actually selected.
    # A missing/invalid fallback must not prevent using the file input port.
    if params['style'] == 'file' and params.get('file'):
        try:
            existing = Path(params['file']).is_file()
        except (OSError, ValueError):
            existing = False
        if existing:
            return {'file': asdict(ResourceRef.capture(params['file'], 'lut'))}
    return {}


def build_lut(inputs, params, context):
    strength = finite_number(input_or(inputs, params, 'strength'))
    if not 0 <= strength <= 1: raise ValueError('LUT强度须为0–1。')
    if strength == 0: return {'operations': (FilterOperation('LUT 已关闭', None, 'null'),)}
    values = dict(params, strength=strength); assets = ()
    if values['style'] == 'file':
        path = values['file'] if inputs.get('file') is None else inputs['file']
        if not isinstance(path, str) or not path.strip():
            raise ValueError('LUT文件路径为空；连接的文件端口不会回退到面板中的旧文件。')
        record = getattr(context, 'node_prepared', {}).get('file')
        ref = ResourceRef(**record) if record and str(Path(path).resolve()) == record['path'] else ResourceRef.capture(path, 'lut')
        ref.verify(); read_cube(ref.path); assets = (('lut', ref),)
    elif values['style'] not in {s[0] for s in LOOKS}: raise ValueError('LUT预设无效。')
    return {'operations': (FilterOperation('原创SDR LUT', None, 'core.lut', (('settings', json_text(values)),), assets),)}


def compile_lut(operation, directory, context, video_info):
    values = json.loads(dict(operation.options)['settings'])
    stream = video_info['video']; transfer = stream.get('color_transfer', 'unknown')
    if transfer in {'smpte2084', 'arib-std-b67', 'smpte428'} or stream.get('color_primaries') in {'bt2020', 'smpte432'}:
        raise ValueError('该视频是HDR／宽色域，外观LUT不负责色彩空间转换；请先转为SDR/Rec.709。')
    if transfer not in {'bt709', 'iec61966-2-1'} and values.get('input_space') != 'assume_sdr':
        raise ValueError('无法确认源色彩空间。请核对源为SDR后，在LUT节点选择“我确认源为SDR”；不要对未知Log直接套用。')
    strength = finite_number(values['strength'])
    if not 0 <= strength <= 1: raise ValueError('LUT强度无效。')
    if values['style'] == 'file':
        ref = dict(operation.assets)['lut']; ref.verify(); size, rows = read_cube(ref.path)
    else:
        size = 17; rows = lut_rows(values['style'], size)
    lines = ['TITLE "AssistantTool original display-referred SDR look"', f'LUT_3D_SIZE {size}', 'DOMAIN_MIN 0 0 0', 'DOMAIN_MAX 1 1 1']
    for index, row in enumerate(rows):
        context.check_canceled()
        identity = (index % size/(size-1), index//size % size/(size-1), index//(size*size)/(size-1))
        lines.append(' '.join(f'{(1-strength)*base+strength*value:.8f}' for base, value in zip(identity, row)))
    name = 'look_' + uuid4().hex + '.cube'
    data_file(Path(directory)/name).write_text_direct('\n'.join(lines)+'\n', encoding='utf-8')
    interp = values.get('interpolation', 'tetrahedral')
    if interp not in {'nearest', 'trilinear', 'tetrahedral'}: raise ValueError('LUT插值选项无效。')
    return (VideoFilterFragment(operation.video, (), (f'[input]format=gbrap,lut3d=file={name}:interp={interp},format=rgba[output]',)),)


def register_visual_nodes(registry):
    def add(kind, title, **kwargs):
        registry.register('core', NodeDefinition('core.'+kind, title, category='视频与画面', **kwargs))
    optional = lambda key, title: NodePort(key, title+' · 可覆盖参数', 'number', required=False)
    add('background', 'Background · 背景生成',
        inputs=tuple(optional(k, t) for k, t in (('width', '宽度'), ('height', '高度'), ('fps', '帧率'), ('duration', '时长'))) +
            (NodePort('color', '颜色1', 'color', required=False), NodePort('color2', '颜色2', 'color', required=False)),
        outputs=(NodePort('video', '背景视频流', 'video', capabilities=('seek', 'replay')),), parameters=(
            NodeParameter('style', '类型', 'choice', 'solid', choices=(('solid', '纯色／透明'), ('linear', '线性渐变'), ('radial', '径向渐变'))),
            NodeParameter('color', '颜色1 · #RRGGBBAA', 'color', '#202b42'), NodeParameter('color2', '颜色2', 'color', '#6b3056'),
            NodeParameter('width', '宽(px)', 'integer', 1080, 1, 8192), NodeParameter('height', '高(px)', 'integer', 1920, 1, 8192),
            NodeParameter('fps', '帧率', 'number', 25, 1, 240), NodeParameter('duration', '时长(秒)', 'number', 15, .01, 86400)) +
            tuple(NodeParameter(k, k+' (%)', 'number', v, 0, 100) for k, v in (('x0', 0), ('y0', 0), ('x1', 100), ('y1', 100))),
        build=build_background, compile_source=compile_background, validate_parameters=background_values,
        description='纯色／透明／线性或径向渐变。直接生成过滤器源，不预写MP4；接解算器可叠加文字／图片。'
            '支持RGBA，最终普通MP4不存透明通道；Alpha应在合成阶段使用。尺寸也可接视频信息节点。')
    choices = lambda key, title: NodeParameter(key, title, 'choice', 'no', choices=(('no', '否'), ('yes', '是')))
    values = (('x', '锚点放到画布X', 50), ('y', '锚点放到画布Y', 50), ('scale_x', '横向缩放倍数', 1),
              ('scale_y', '纵向缩放倍数', 1), ('rotation', '旋转(度)', 0), ('anchor_x', '素材锚点X(%)', 50), ('anchor_y', '素材锚点Y(%)', 50))
    add('transform', 'Transform · 通用变换', inputs=(NodePort('video', '视频流', 'video'),) +
        tuple(optional(k, t) for k, t, _ in values) + (NodePort('color', '边缘底色', 'color', required=False),),
        outputs=(NodePort('video', '变换后视频流', 'video', capabilities=('seek', 'replay')),),
        parameters=tuple(NodeParameter(k, t, 'number', v, -100000 if k not in {'scale_x', 'scale_y'} else .001,
                                      100000 if k not in {'scale_x', 'scale_y'} else 100) for k, t, v in values) + (
            NodeParameter('unit', '画布位置单位', 'choice', 'percent', choices=(('percent', '百分比 · 中心50/50'), ('pixels', '像素 · 原点左上'))),
            NodeParameter('canvas', '输出画布', 'choice', 'keep', choices=(('keep', '保持原尺寸'), ('custom', '明确改变画布'))),
            NodeParameter('width', '自定义画布宽(px)', 'integer', 1080, 1, 8192), NodeParameter('height', '自定义画布高(px)', 'integer', 1920, 1, 8192),
            choices('flip_x', '水平镜像'), choices('flip_y', '垂直镜像'),
            NodeParameter('edge', '超出／空白边缘', 'choice', 'transparent', choices=(('transparent', '透明 · 超出画布裁切'), ('solid', '指定底色 · 超出裁切'))),
            NodeParameter('color', '底色', 'color', '#202b42'), NodeParameter('interpolation', '缩放插值', 'choice', 'bicubic',
                choices=(('neighbor', '最近邻'), ('bilinear', '双线性'), ('bicubic', '双三次'), ('lanczos', 'Lanczos')))),
        build=build_transform, compile_source=compile_transform, validate_parameters=transform_values,
        description='平移、缩放、旋转、素材锚点与镜像；接收并返回公共视频流，只在最后编码。'
            '百分比位置以输出画布为基准，锚点以素材为基准；保持尺寸与改变画布是两个明确选项。')
    add('lut', 'LUT · 原创风格／自选文件', inputs=(NodePort('file', '.cube路径 · 覆盖参数', 'text', required=False), optional('strength', '强度0–1')),
        outputs=(NodePort('operations', '调色操作', 'operations'),), parameters=(
            NodeParameter('style', '预设／文件', 'choice', 'warm', choices=tuple((k, t) for k, t, _ in LOOKS)+(('file', '自选.cube'),)),
            NodeParameter('file', '.cube文件', required=False), NodeParameter('strength', '强度0–1', 'number', .5, 0, 1),
            NodeParameter('input_space', '源色彩空间', 'choice', 'strict', choices=(('strict', '必须有SDR元数据'), ('assume_sdr', '我确认源为SDR/Rec.709'))),
            NodeParameter('interpolation', '插值', 'choice', 'tetrahedral', choices=(('nearest', '最近邻'), ('trilinear', '三线性'), ('tetrahedral', '四面体')))),
        build=build_lut, prepare=prepare_lut, compile_operation=compile_lut,
        description='暖／冷／柔和／复古／黑白为原创数学外观预设，无第三方LUT授权问题；适用于显示参考SDR。'
            '不是Log/HDR技术转换。强度0不改画面；预览和导出使用同一3D LUT，RGBA保留到合成。')
