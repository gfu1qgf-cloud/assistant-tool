"""Deterministic value operations, shared by the editor and headless executor."""
import hashlib
import json
import math
import random

from .basic import finite_number
from .contracts import ColorValue, NodeDefinition, NodeParameter, NodePort
from .python_values import PythonValue, validate_data


def input_or(inputs, params, key):
    value = inputs.get(key)
    return params[key] if value is None else value


def fit(inputs, params, _context):
    values = [finite_number(input_or(inputs, params, key))
              for key in ('value', 'source_min', 'source_max', 'target_min', 'target_max')]
    value, low, high, start, end = values
    if high == low:
        raise ValueError('Fit 源范围的两个端点不能相同。')
    fraction = (value - low) / (high - low)
    if params['clamp'] == 'yes':
        fraction = min(1, max(0, fraction))
    return {'number': finite_number(start + fraction * (end - start))}


def rgb(inputs, params, _context):
    scale = 255 if params['unit'] == 'byte' else 1
    channels = [finite_number(input_or(inputs, params, key)) / scale for key in ('red', 'green', 'blue', 'alpha')]
    if params['range'] == 'clamp':
        channels = [min(1, max(0, value)) for value in channels]
    return {'color': ColorValue(*channels)}


def split_color(inputs, params, _context):
    scale = 255 if params['unit'] == 'byte' else 1
    color = inputs['color']
    return dict(zip(('red', 'green', 'blue', 'alpha'),
                    (value * scale for value in (color.red, color.green, color.blue, color.alpha))))


def color_hex(inputs, _params, _context):
    color = inputs['color']
    return {'text': color.hex_rgb + f'{round(color.alpha * 255):02x}'}


def random_ports(params):
    mode = params.get('mode', 'integer')
    dtype = {'integer': 'integer', 'float': 'float', 'choice': 'data', 'shuffle': 'list'}.get(mode)
    if dtype is None:
        raise ValueError('随机模式无效。')
    return (NodePort('value', '随机结果', dtype),)


def deterministic_rng(inputs, params, context):
    seed = str(input_or(inputs, params, 'seed'))
    scope = []
    if params['scope'] == 'task':
        task = getattr(context, 'task', None)
        scope += [getattr(task, 'key', ''), getattr(task, 'directory', '')]
    elif params['scope'] == 'iteration':
        scope.append(getattr(getattr(context, 'workflow_globals', None), 'iteration', 1))
    iteration = inputs.get('iteration')
    if iteration is not None:
        scope.append(finite_number(iteration))
    identity = [getattr(context, 'node_id', ''), seed, scope]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).digest()
    return random.Random(int.from_bytes(digest, 'big'))


def random_value(inputs, params, context):
    rng = deterministic_rng(inputs, params, context)
    mode = params['mode']
    if mode in {'choice', 'shuffle'}:
        items = inputs.get('items')
        if type(items) is not list or not items:
            raise ValueError('列表随机需要连接非空列表；不会替换为默认项。')
        validate_data(items)
        if mode == 'choice':
            return {'value': PythonValue.capture(rng.choice(items))}
        values = items.copy()
        rng.shuffle(values)
        return {'value': values}
    low, high = (finite_number(input_or(inputs, params, key)) for key in ('minimum', 'maximum'))
    if low > high:
        raise ValueError('随机下限不能大于上限。')
    if mode == 'integer':
        if int(low) != low or int(high) != high:
            raise ValueError('整数随机的范围必须为整数。')
        return {'value': rng.randint(int(low), int(high))}
    return {'value': float(low + (high - low) * rng.random())}


def convert_number(inputs, params, _context):
    value = finite_number(inputs['number'])
    if params['mode'] == 'float':
        return {'value': float(value)}
    if params['rounding'] == 'exact' and int(value) != value:
        raise ValueError('该数值不是整数；请选择明确的取整方式。')
    methods = {'exact': int, 'truncate': int, 'floor': math.floor, 'ceil': math.ceil, 'round': round}
    return {'value': methods[params['rounding']](value)}


def register_value_nodes(registry):
    def add(kind, title, **kwargs):
        registry.register('core', NodeDefinition('core.' + kind, title, **kwargs))
    numeric = lambda key, title: NodePort(key, title + ' · 可覆盖参数', 'number', required=False)
    add('integer', '整数', category='Python 原生/数据与转换',
        outputs=(NodePort('number', '整数', 'integer'),),
        parameters=(NodeParameter('value', '整数', 'integer', 0, -1000000000, 1000000000),),
        build=lambda _i, p, _c: {'number': p['value']})
    add('float', '浮点数', category='Python 原生/数据与转换',
        outputs=(NodePort('number', '浮点数', 'float'),),
        parameters=(NodeParameter('value', '浮点数', 'number', 0., -1e12, 1e12),),
        build=lambda _i, p, _c: {'number': float(finite_number(p['value']))})
    add('numeric_cast', '数值类型转换 · 整数／浮点', category='Python 原生/数据与转换',
        inputs=(NodePort('number', '数值', 'number'),),
        output_factory=lambda p: (NodePort('value', '转换后的数值', p.get('mode', 'float')),),
        parameters=(NodeParameter('mode', '目标类型', 'choice', 'float', choices=(('float', '浮点'), ('integer', '整数'))),
            NodeParameter('rounding', '转整数时', 'choice', 'exact', choices=(('exact', '必须原本为整数'),
                ('truncate', '截去小数'), ('floor', '向下取整'), ('ceil', '向上取整'), ('round', '四舍六入五成双')))),
        build=convert_number)
    defaults = (('value', '输入值', 0), ('source_min', '源范围起点', 0), ('source_max', '源范围终点', 1),
                ('target_min', '目标范围起点', 0), ('target_max', '目标范围终点', 1))
    add('fit', 'Fit · 范围映射', category='Python 原生/数学',
        inputs=tuple(numeric(key, title) for key, title, _ in defaults),
        outputs=(NodePort('number', '映射数值', 'number'),),
        parameters=tuple(NodeParameter(key, title, 'number', value, -1e12, 1e12) for key, title, value in defaults) +
            (NodeParameter('clamp', '超出源范围', 'choice', 'yes', choices=(('yes', '夹紧到范围内'), ('no', '允许外推'))),),
        build=fit, description='两端范围可反向；源起点和终点不能相同。0–1 可映射到字号、位置或缩放。')
    channels = (('red', 'R · 红'), ('green', 'G · 绿'), ('blue', 'B · 蓝'), ('alpha', 'A · 不透明度'))
    unit = NodeParameter('unit', '通道单位', 'choice', 'normalized', choices=(('normalized', '0–1'), ('byte', '0–255')))
    add('rgb', 'RGB／RGBA · 构造颜色', category='颜色',
        inputs=tuple(numeric(key, title) for key, title in channels), outputs=(NodePort('color', 'sRGB 颜色', 'color'),),
        parameters=tuple(NodeParameter(key, title, 'number', 1 if key == 'alpha' else 0, 0, 255) for key, title in channels) +
            (unit, NodeParameter('range', '超范围策略', 'choice', 'error', choices=(('error', '报错'), ('clamp', '夹紧')))),
        build=rgb,
        description='显示参考 sRGB，不是线性光颜色。Alpha 与 RGB 使用相同单位；255 模式的不透明 Alpha 应填255。')
    add('color_split', '拆分 RGBA', category='颜色', inputs=(NodePort('color', '颜色', 'color'),),
        outputs=tuple(NodePort(key, title, 'number') for key, title in channels), parameters=(unit,), build=split_color)
    add('color_from_hex', '十六进制转颜色', category='颜色', inputs=(NodePort('text', '#RRGGBB / #RRGGBBAA', 'text', required=False),),
        outputs=(NodePort('color', '颜色', 'color'),), parameters=(NodeParameter('value', '颜色', 'color', '#ffffff'),),
        build=lambda i, p, _c: {'color': ColorValue.from_hex(i['text'] if i.get('text') is not None else p['value'])})
    add('color_to_hex', '颜色转十六进制', category='颜色', inputs=(NodePort('color', '颜色', 'color'),),
        outputs=(NodePort('text', '#RRGGBBAA', 'text'),), build=color_hex)
    add('random', '通用随机', category='Python 原生/随机',
        inputs=(numeric('minimum', '下限'), numeric('maximum', '上限'), NodePort('items', '随机列表', 'list', required=False),
                NodePort('seed', '种子', 'text', required=False), numeric('iteration', '循环次数')),
        output_factory=random_ports,
        parameters=(NodeParameter('mode', '随机类型', 'choice', 'integer', choices=(('integer', '整数 · 两端包含'),
            ('float', '浮点'), ('choice', '列表随机选一项'), ('shuffle', '列表洗牌'))),
            NodeParameter('minimum', '下限', 'number', 0, -1e12, 1e12), NodeParameter('maximum', '上限', 'number', 1, -1e12, 1e12),
            NodeParameter('seed', '种子', default='0'), NodeParameter('scope', '作用范围', 'choice', 'task',
                choices=(('fixed', '固定 · 预览刷新不重抽'), ('task', '按任务身份变化'), ('iteration', '按Line当前循环次数变化')))),
        build=random_value, description='节点独立随机状态，固定种子可复现；不修改池进度和输入列表。'
            'Begin/End 内如需每轮变化，连接 Begin 的循环次数。列表选项输出通用数据，可接显式数据转换。')
    from .text_palette import register_palette_nodes
    register_palette_nodes(registry)


