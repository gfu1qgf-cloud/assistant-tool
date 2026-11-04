"""Check declared segment identities before accepting a complete delivery."""
from pathlib import Path
import re
from .segmentation import existing_segments

PART = re.compile(r'第\s*0*(\d+)\s*段')
TASK = re.compile(r'任务(.+?)[_\s-]+第\s*\d+\s*段')


def delivery_plan(kind, text):
    if kind == 'image': return {'count': 1, 'segments': None, 'kind': 'image'}
    segments = existing_segments(text)
    return {'count': len(segments), 'segments': segments} if segments else None


def check_parts(files, plan, task_id, records=None):
    if not plan: return ''
    expected = plan['count']; found = []
    if plan.get('kind') == 'image':
        if len(files) > 1: raise ValueError('单张图片对应多个视频，未猜测采用哪一个。')
        return '' if len(files) == 1 else '图片任务尚未发现成品。'
    for item in files:
        name = item.get('name') or Path(str((records or {}).get(item['id'], {}).get('path') or '')).name
        owner = TASK.search(name)
        if owner and owner.group(1).strip() != str(task_id).strip():
            raise ValueError('成品文件名属于其他任务，未收取或归档：' + name)
        part = PART.search(name)
        if part: found.append(int(part.group(1)))
    if len(files) > expected or len(found) != len(set(found)) or any(number < 1 or number > expected for number in found):
        raise ValueError('成品存在多余、重复或越界段号，请核对当前版本；未猜测归档。')
    if len(files) < expected: return f'应交付{expected}段，目前仅发现{len(files)}段；缺段不标记已归档。'
    if expected > 1 and set(found) != set(range(1, expected + 1)):
        return '视频数量已到，但文件名段号不完整，未标记已归档；请按“任务编号_第01段”格式核对。'
    return ''
