"""Adapt the segmenter node's list output to one Agent-table text cell."""
import re
import threading

KIND = 'script_segmenter.split'


def parameters():
    from ..script_segmenter.nodes import segment_node
    values = {item.key: item.default for item in segment_node().parameters}
    values['duration_mode'] = '10'
    return values


def require_available(context):
    if KIND not in context.available_nodes().definitions:
        raise ValueError('请先启用“智能文案分段助手”插件并重启。未退回未分段原文，任务仍保留。')


def split_spoken(text, run=None):
    from ..script_segmenter.nodes import build
    if run is None:
        from model.BackgroundTasks import TaskRunContext
        run = TaskRunContext(threading.Event(), lambda *_args: None, lambda *_args: None)
    result = build({'text': text}, parameters(), run)
    parts = result['segments']
    if not isinstance(parts, list) or not parts or any(not isinstance(item, str) or not item.strip() for item in parts):
        raise ValueError('分段节点没有返回有效的外文正文列表；未使用空白或未分段原文替代。')
    compact = lambda value: re.sub(r'\s+', '', value)
    if compact(text) != ''.join(compact(item) for item in parts):
        raise ValueError('分段正文完整性校验失败；未提交。')
    formatted = '\n\n'.join(f'【第{index}段｜约10秒】\n{part}' for index, part in enumerate(parts, 1))
    if len(formatted) > 45000:
        raise ValueError('分段后文本超过单元格安全长度，请先拆分任务；没有截断台词。')
    return {'text': formatted, 'count': len(parts), 'warnings': str(result.get('warnings') or '')}
