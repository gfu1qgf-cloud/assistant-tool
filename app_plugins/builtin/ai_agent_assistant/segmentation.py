"""Adapt the segmenter node's list output to one Agent-table text cell."""
import re
import math
import threading

KIND = 'script_segmenter.split'
MARKER = re.compile(r'(?m)^[ \t]*【第\s*(\d+)\s*段[｜|]\s*(?:约\s*)?(\d+(?:\.\d+)?)\s*秒】[ \t]*')


def existing_segments(text):
    """Recognize user-provided boundaries, never re-estimate or rewrite them."""
    text = str(text or '')
    matches = list(MARKER.finditer(text))
    if not matches:
        if re.search(r'【第\s*\d+\s*段', text): raise ValueError('已有分段标记格式不完整，请核对段号和秒数；未重新分段。')
        return None
    if len(list(re.finditer(r'【第\s*\d+\s*段', text))) != len(matches):
        raise ValueError('已有分段混有不完整标记，请核对；未把错误标记并入其他段。')
    if text[:matches[0].start()].strip(): raise ValueError('第一段之前有未归入分段的正文，请先核对；未重新分段。')
    parts = []
    for index, match in enumerate(matches):
        number = int(match.group(1)); seconds = float(match.group(2))
        body = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
        if number != index + 1 or not math.isfinite(seconds) or seconds <= 0 or not body:
            raise ValueError('已有分段存在重复／缺号、无效秒数或空正文，请核对；未重新分段。')
        parts.append({'number': number, 'seconds': seconds, 'text': body})
    return parts


def parameters():
    from ..script_segmenter.nodes import segment_node
    values = {item.key: item.default for item in segment_node().parameters}
    values['duration_mode'] = '10'
    return values


def require_available(context):
    if KIND not in context.available_nodes().definitions:
        raise ValueError('请先启用“智能文案分段助手”插件并重启。未退回未分段原文，任务仍保留。')


def split_spoken(text, run=None):
    provided = existing_segments(text)
    if provided is not None:
        if len(text) > 45000: raise ValueError('原有分段超过单元格安全长度；未截断台词。')
        return {'text': text, 'count': len(provided), 'warnings': '', 'preserved': True}
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
