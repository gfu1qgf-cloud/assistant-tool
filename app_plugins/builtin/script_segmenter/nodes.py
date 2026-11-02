"""Compute-only segmentation; a typed Python list contains source text only."""
import math

from app_plugins.api import NodeDefinition, NodeParameter, NodePort
from .duration_warning import duration_check, warning_text
from .engine import split_copy

KIND = 'script_segmenter.split'
DURATIONS = {'auto': [6, 8, 10], '10': [10], '8': [8], '6': [6]}
PARAMETERS = (
    NodeParameter('boundary_strategy', '分段策略', 'choice', 'punctuation_first', choices=(
        ('punctuation_first', '优先完整句'), ('fewest', '减少段数'))),
    NodeParameter('duration_mode', '时长规则', 'choice', 'auto', choices=(
        ('auto', '自动 6 / 8 / 10 秒'), ('10', '仅 10 秒'), ('8', '仅 8 秒'), ('6', '仅 6 秒'))),
    NodeParameter('words_per_minute', '朗读速度 · 词/分钟', 'integer', 150, 40, 300),
    NodeParameter('characters_per_minute', '无空格语言速度 · 字/分钟', 'integer', 240, 80, 600),
    NodeParameter('pause_seconds', '句末停顿 · 秒', 'number', .3, 0, 2),
    NodeParameter('padding_seconds', '每段预留 · 秒', 'number', .4, 0, 2),
    NodeParameter('total_limit_seconds', '总朗读提醒上限 · 秒', 'integer', 180, 30, 3600),
)


def validate_params(params):
    for parameter in PARAMETERS:
        value = params.get(parameter.key, parameter.default)
        if parameter.kind == 'choice':
            valid = value in [item[0] for item in parameter.choices]
        else:
            valid = (type(value) is int if parameter.kind == 'integer' else
                     type(value) in (int, float) and math.isfinite(value))
            valid = valid and parameter.minimum <= value <= parameter.maximum
        if not valid:
            raise ValueError('文案分段参数无效：' + parameter.title)


def build(inputs, params, context):
    context.check_canceled()
    validate_params(params)
    text = inputs.get('text')
    if not isinstance(text, str):
        raise TypeError('文案分段需要文本输入，请连接任务文案或文本节点。')
    settings = {item.key: params.get(item.key, item.default) for item in PARAMETERS}
    settings['durations'] = DURATIONS[settings.pop('duration_mode')]
    settings['preferred_seconds'] = max(settings['durations'])
    result = split_copy(text, settings, canceled=context.is_canceled)
    context.check_canceled()
    segments = [item['text'] for item in result['segments']]
    check = duration_check(result)
    over_limit = check['seconds'] > check['limit']
    notices = [warning_text(check)] if over_limit else []
    warned = [item for item in result['segments'] if item['warning']]
    notices.extend(f"第{item['index']}段：{item['warning']}" for item in warned[:8])
    if len(warned) > 8:
        notices.append(f'另有{len(warned) - 8}段超时，请检查时长参数。')
    warnings = '\n'.join(notices)
    context.report(f"文案分段：{len(segments)}段，预计朗读{result['estimated_seconds']:.1f}秒"
                   + ('；' + warnings if warnings else ''))
    context.check_canceled()
    return {'segments': segments, 'count': len(segments),
            'estimated_seconds': result['estimated_seconds'],
            'over_limit': over_limit, 'warnings': warnings}


def segment_node():
    return NodeDefinition(KIND, '智能文案分段',
        inputs=(NodePort('text', '原始文案', 'text'),),
        outputs=(NodePort('segments', '分段列表 · Python list', 'list'),
                 NodePort('count', '段数', 'number'),
                 NodePort('estimated_seconds', '预计朗读 · 秒', 'number'),
                 NodePort('over_limit', '整篇超时', 'boolean'),
                 NodePort('warnings', '时长提醒', 'text')),
        parameters=PARAMETERS, build=build, validate_parameters=validate_params,
        execution='compute', category='文字处理',
        keywords=('文案', '分段', '断句', 'Python', '列表', '6秒', '8秒', '10秒'),
        description='接入任务文案或文本，选择分段策略，直接输出 Python 字符串列表；'
                    '不含段号、秒数标签、代码围栏或说明。列表端口可直接接列表节点。'
                    '复用分段助手的数字估时、完整词及短尾合并规则；只分段，不改写原文。'
                    '参数随工作流保存，不读取窗口默认参数。默认180秒整篇提醒及单段超时'
                    '经独立端口和进度显示，不弹窗、不混入正文；不会复制、保存文件或调用AI。')
