"""Ordered, short-circuit If / Elif / Else, using the ordinary typed evaluator."""
from .contracts import NodeDefinition, NodeParameter, NodePort

KIND = 'core.choose'
MAX_BRANCHES = 32


def branch_count(params, *, strict=False):
    value = params.get('branches', 3)
    if type(value) is int and 1 <= value <= MAX_BRANCHES:
        return value
    if strict:
        raise ValueError('条件分支数量必须是 1～32 的整数。')
    return 3  # Keep malformed saved drafts displayable, but never executable.


def inputs(params):
    result = []
    for index in range(1, branch_count(params) + 1):
        result.extend((NodePort(f'condition_{index}', f'{"if" if index == 1 else "elif"} · 条件 {index}', 'boolean'),
                       NodePort(f'value_{index}', f'结果 {index}', 'any', type_group='value')))
    return tuple(result) + (NodePort('default', 'else · 默认结果', 'any', type_group='value'),)


def select(params, get):
    for index in range(1, branch_count(params, strict=True) + 1):
        condition = get(f'condition_{index}')
        if type(condition) is not bool:
            raise ValueError('条件输入必须是布尔值，不把数字或文本隐式当作真假。')
        if condition:
            return f'value_{index}'
    return 'default'


def register_selection(registry):
    registry.register('core', NodeDefinition(KIND, '多条件选择 · Switch',
        input_factory=inputs, select_input=select,
        outputs=(NodePort('value', '所选结果', 'any', type_group='value'),),
        parameters=(NodeParameter('branches', '条件分支数量（按 1 → 2 → 3 顺序）', 'integer', 3, 1, MAX_BRANCHES),),
        build=lambda i, _p, _c: {'value': next(iter(i.values()))},
        validate_parameters=lambda p: branch_count(p, strict=True),
        category='Python 原生/流程控制/条件分支', keywords=('switch', 'if', 'elif', 'else', '多条件', '首个满足'),
        description='逐个判断条件，输出第一个为真的对应结果；都不满足时输出默认结果。'
                    '运行阶段不计算后续条件及未选中的结果。所有结果和默认端必须是同一种类型。'
                    '可调整分支数量；减少已有连线的分支前须先断开，避免丢线。所有分支仍做静态预检。'))


def select_boolean(_params, get):
    condition = get('condition')
    if type(condition) is not bool:
        raise TypeError('条件输入必须是真／假，不隐式转换数字或文本。')
    return 'yes' if condition else 'no'
