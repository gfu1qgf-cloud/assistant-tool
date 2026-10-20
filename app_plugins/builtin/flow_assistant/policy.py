"""Pure queue policy: affordability, visible waiting reasons and duplicate identity."""
import hashlib
import json
import math
from pathlib import Path
import time

from .routing import cost_key, lane_key, dispatch_rank
from .credit_policy import balance_ready
from .connection_status import browser_keys, configured_profiles, browser_status
from .pricing import fixed_cost
from .account_policy import hold_reason

FAILURES = {'failed', 'uncertain', 'download_failed', 'archive_failed'}
ACTIVE = {'queued', 'preparing', 'uploading', 'submitting', 'generating', 'downloading'}


def signature(job):
    value = {key: job.get(key) for key in ('model', 'duration', 'aspect', 'resolution')}
    value.update(image=job.get('image_identity', {}).get('sha256'),
                 directory=str(Path(job['directory']).resolve()).casefold(),
                 prompt=' '.join(job['prompt'].split()))
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def draft_key(directory, image):
    return hashlib.sha256(json.dumps([str(Path(directory).resolve()).casefold(),
        str(Path(image).resolve()).casefold()], ensure_ascii=False).encode()).hexdigest()


def estimated_cost(job, observed):
    fixed = fixed_cost(job)
    if fixed is not None: return fixed
    value = observed.get(cost_key(job))
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def block_for(client, preferences):
    blocks = preferences.get('browser_blocks', {})
    return hold_reason(client, preferences) or client.get('setup_blocked') or next((blocks[key] for key in sorted(browser_keys(client)) if blocks.get(key)), '')


def capable(client, job, preferences, cost=None):
    credits = client.get('credits')
    return (not client.get('blocked') and not block_for(client, preferences)
            and not client.get('batch_running')
            and not (client.get('credit_check') or {}).get('hold_dispatch')
            and client.get('page_ready') is not False
            and balance_ready(client)
            and (credits >= cost if cost is not None else credits > 0)
            and dispatch_rank(client, job, preferences) is not None)


def batch_wait(job, batches, cost=None):
    batch = batches.get(job.get('batch_id'), {})
    limit = batch.get('limit_total', 0)
    used = batch.get('reserved', 0)
    if limit and (used >= limit or cost is not None and used + cost > limit):
        return f'整批积分上限：已预留 {used:g} / {limit:g}，停止新增提交'
    return ''


def waiting_reason(row, clients, rows, preferences, observed, batches, *, paused=False, released=()):
    if row['status'] != 'queued':
        return row['error']
    if paused and row['id'] not in released:
        return '队列已暂停，等待开始／只提交选中项'
    cost = estimated_cost(row['payload'], observed)
    if cost is not None and row['payload'].get('budget') and cost > row['payload']['budget']:
        source = '固定档位费用' if fixed_cost(row['payload']) is not None else '最近确认费用'
        return f'单条预算 {row["payload"]["budget"]} 低于{source} {cost:g}，请修改排队项预算'
    budget = batch_wait(row['payload'], batches, cost)
    if budget:
        return budget
    lane = [c for c in clients if dispatch_rank(c, row['payload'], preferences) is not None]
    duration = row['payload'].get('duration')
    label = f'{duration} 秒档' if duration else '通用档'
    if not lane:
        configured = configured_profiles(preferences)
        useful = [p for p in configured if dispatch_rank({'profile': p}, row['payload'], preferences) is not None]
        if not useful:
            if not configured:
                return '尚无可启动的项目配置：请为一个浏览器保存项目地址；时长由程序自动分配，无需手动分档'
            return f'{label}暂无匹配浏览器：当前配置为手动固定档；可切回“自动分配”后由程序接单'
        return f'{label}等待浏览器自动启动／空闲后自动分配时长，无需手动分档'
    if not any(capable(c, row['payload'], preferences, cost) for c in lane):
        funded = [c for c in lane if balance_ready(c)
                  and (c['credits'] >= cost if cost is not None else c['credits'] > 0)]
        if funded:
            # A funded browser may still be pairing, loading or quarantined.
            # A separate zero-credit backup must not turn that into a shortage.
            client = funded[0]
            status = browser_status(client, preferences, rows)
            return str(client.get('profile') or client.get('id') or '浏览器') + '：' + status['reason']
    if all(block_for(c, preferences) or c.get('blocked') for c in lane):
        return '对应浏览器已暂停：' + str(next((block_for(c, preferences) or c.get('blocked') for c in lane), ''))
    if all(c.get('page_ready') is False for c in lane):
        return '扩展已连接，但 Flow 项目页面未响应；请打开对应浏览器检查'
    if not any(c.get('account') for c in lane):
        return '需要登录／确认当前 Flow 账号，请打开对应浏览器'
    if not any(balance_ready(c) for c in lane):
        return '等待本次连接首次读取账号积分'
    if not any(capable(c, row['payload'], preferences, cost) for c in lane):
        return '积分不足以承担下一条任务；等待补充积分或备用账号'
    held = [r for r in rows if r['status'] in ACTIVE - {'queued'} | {'uncertain', 'download_failed'}]
    accounts = {r['remote'].get('account') for r in held}
    if any(c.get('account') in accounts for c in lane):
        return '等待原结果生成／下载；可核对结束后暂存下载并释放账号'
    return f'等待空闲{label}／并行名额'


def media_review(info, job):
    """Keep anomalous results for review; never delete or regenerate them."""
    if not isinstance(info, dict):
        return []
    issues = []
    duration = job.get('duration')
    if duration and abs(info['duration'] - duration) > max(.75, duration * .1):
        issues.append(f'时长 {info["duration"]:.2f} 秒，与要求 {duration} 秒不符')
    video = info.get('video', {})
    width = video.get('display_width', video.get('width', 0))
    height = video.get('display_height', video.get('height', 0))
    if width and height:
        ratio = 9 / 16 if job.get('aspect') == '9:16' else 16 / 9
        if abs(math.log((width / height) / ratio)) > .08:
            issues.append(f'画面比例 {width} × {height} 与要求 {job.get("aspect")} 不符')
        wanted = int(job.get('resolution', '720p').rstrip('p'))
        if min(width, height) < wanted:
            issues.append(f'分辨率 {width} × {height} 低于要求 {wanted}p')
    return issues
