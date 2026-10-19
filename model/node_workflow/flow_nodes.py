"""Line-scoped flow outcomes; keep skipped work distinct from failed work."""
from .contracts import NodeDefinition, NodeParameter, NodePort


class WorkflowLineSkipped(RuntimeError):
    workflow_skipped = True


def select_or_skip(params, get):
    condition = get('condition')
    if type(condition) is not bool:
        raise TypeError('跳过条件必须是真／假。')
    if not condition:
        message = get('message')
        message = str(params['message'] if message is None else message).strip()
        raise WorkflowLineSkipped(message or '条件不满足，跳过本行。')
    return 'value'


def register_flow_nodes(registry):
    registry.register('core', NodeDefinition('core.skip_if', '条件通过／跳过本行',
        inputs=(NodePort('condition', '继续条件 · 真才执行', 'boolean'),
                NodePort('value', '通过时传递的值', 'any', type_group='value'),
                NodePort('message', '跳过原因 · 可选', 'text', required=False)),
        outputs=(NodePort('value', '通过的值', 'any', type_group='value'),),
        parameters=(NodeParameter('message', '默认跳过原因', default='条件不满足，跳过本行。'),),
        select_input=select_or_skip, build=lambda inputs, _params, _context: {'value': inputs['value']},
        execution_scope='line', category='Python 原生/流程控制/条件分支',
        description='真时原样传递值，假时记录原因并将当前任务标记为“已跳过”；'
                    '不会执行“值”端口的上游，其他任务继续。仅用于逐行任务工作流；'
                    '请把它接在耗时节点之前且连到最终输出链，否则未连接的节点不会运行。'))
