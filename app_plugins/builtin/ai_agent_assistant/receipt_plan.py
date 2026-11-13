"""Check declared segment identities before accepting a complete delivery."""
from pathlib import Path
import re
from .segmentation import existing_segments

PART = re.compile(r'第\s*0*(\d+)\s*段')
TASK = re.compile(r'任务(.+?)[_\s-]+第\s*\d+\s*段')
LOCAL_PREFIX = re.compile(r'^Agent_[a-f0-9]{12}_[a-f0-9]{12}_', re.IGNORECASE)
SHORT_PART = re.compile(r'(?:(\d+)[_\s-]+)?S0*(\d+)', re.IGNORECASE)
HANDOFF_POLICY = 'isolated-segments-v1'


def handoff_files(files, plan, task_id, records=None):
    """Receive unambiguous handoff segments, never choose between versions.

    Explicit candidate/unchecked videos are not deliverables. A conflicted
    ordinal must not prevent unrelated, uniquely identified parts downloading.
    No candidate is promoted merely because a formal version is absent.
    """
    selected, numbered, problems = [], {}, []
    for item in files:
        name = item.get('name') or Path(str((records or {}).get(item['id'], {}).get('path') or '')).name
        if any(marker in name for marker in ('候选', '待验', '待审核', '未通过')):
            problems.append('暂不收取候选／待验文件：' + name)
            continue
        if not plan:
            selected.append(item)
            continue
        try:
            number = part_number(name, task_id)
        except ValueError as error:
            problems.append(str(error))
            continue
        if number is None:
            problems.append('未能确认段号，暂不收取：' + (name or item['id']))
        elif not 1 <= number <= plan['count']:
            problems.append('段号越界，暂不收取：' + name)
        else:
            numbered.setdefault(number, []).append(item)
    for number, versions in numbered.items():
        if len(versions) == 1:
            selected.append(versions[0])
        else:
            problems.append(f'第{number}段存在重复版本，暂不收取该段：' + '、'.join(value.get('name', value['id']) for value in versions))
    # Keep a finite diagnostic without letting one issue hide all the others.
    detail = '\n'.join(problems[:30])
    if len(problems) > 30: detail += f'\n另有{len(problems)-30}项需核对。'
    return selected, detail


def part_number(name, task_id):
    """Recognize explicit segment names, not arbitrary digits or file order."""
    stem = Path(LOCAL_PREFIX.sub('', str(name))).stem
    owner = TASK.search(stem)
    if owner and owner.group(1).strip() != str(task_id).strip():
        raise ValueError('成品文件名属于其他任务，未收取或归档：' + str(name))
    matches = PART.findall(stem)
    if len(matches) > 1: raise ValueError('成品文件名包含多个段号，未猜测顺序：' + str(name))
    if matches: return int(matches[0])
    short = SHORT_PART.fullmatch(stem)
    if short:
        number = int(short.group(2))
        if short.group(1) is not None and int(short.group(1)) != number:
            raise ValueError('成品排序前缀与S段号不一致，未猜测顺序：' + str(name))
        return number
    return None


def delivery_plan(kind, text):
    if kind == 'image': return {'count': 1, 'segments': None, 'kind': 'image'}
    segments = existing_segments(text)
    return {'count': len(segments), 'segments': segments} if segments else None


def completed_files(files, plan, task_id, records=None):
    """Separate unnumbered extras after proving the complete planned set.

    This is not a loose filename filter or a version-selection heuristic.
    Missing, repeated, out-of-range, and wrong-owner segments keep the strict
    validation path. Image/unplanned/public unnamed deliveries are unchanged.
    """
    if not plan or plan.get('kind') == 'image': return files, ''
    numbered, extras = [], []
    for item in files:
        name = item.get('name') or Path(str((records or {}).get(item['id'], {}).get('path') or '')).name
        if part_number(name, task_id) is None:
            extras.append(name or item['id'])
        else:
            numbered.append(item)
    if not extras or len(numbered) != plan['count']: return files, ''
    # Raises on ambiguity; never pick a version based on order/name/mtime.
    if check_parts(numbered, plan, task_id, records): return files, ''
    note = f'计划内{plan["count"]}段已唯一对应；另有{len(extras)}个未标段号的附加视频，单独收取到“Agent附加素材”（不计为口播段落）：' + '、'.join(extras[:20])
    if len(extras) > 20: note += f'，另有{len(extras)-20}个。'
    return numbered, note


def check_parts(files, plan, task_id, records=None):
    if not plan: return ''
    expected = plan['count']; found = []
    if plan.get('kind') == 'image':
        if len(files) > 1: raise ValueError('单张图片对应多个视频，未猜测采用哪一个。')
        return '' if len(files) == 1 else '图片任务尚未发现成品。'
    for item in files:
        name = item.get('name') or Path(str((records or {}).get(item['id'], {}).get('path') or '')).name
        number = part_number(name, task_id)
        if number is not None: found.append(number)
    if len(files) > expected or len(found) != len(set(found)) or any(number < 1 or number > expected for number in found):
        raise ValueError('成品存在多余、重复或越界段号，请核对当前版本；未猜测归档。')
    if len(files) < expected:
        missing = sorted(set(range(1, expected + 1)) - set(found))
        detail = '；尚未核实段号：' + '、'.join(str(number) for number in missing[:30]) if missing else ''
        return f'应交付{expected}段，目前仅发现{len(files)}段{detail}；缺段不标记已归档。'
    if expected > 1 and set(found) != set(range(1, expected + 1)):
        return '视频已收齐，但段号仍需核对，未标记已归档；支持“任务编号_第01段”“01_S01”或“S01”，不凭列表顺序猜测。'
    return ''
