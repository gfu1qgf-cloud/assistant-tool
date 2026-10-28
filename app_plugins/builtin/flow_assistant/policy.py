"""Pure queue policy: affordability, visible waiting reasons and duplicate identity."""
import hashlib
import json
import math
from pathlib import Path
import time

from .routing import cost_key, lane_key, dispatch_rank, parameter_block
from .credit_policy import balance_ready
from .connection_status import browser_keys, configured_profiles, browser_status
from .pricing import fixed_cost
from .account_policy import hold_reason
from .credit_cooldown import credit_reason, MIN_CREDITS

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
    from .exhausted_browser import reason as exhausted_reason
    protected = hold_reason(client, preferences) or credit_reason(client, preferences) or client.get('setup_blocked') or exhausted_reason(client, preferences)
    if protected: return protected
    from .problem_details import offline_retry
    return next((blocks[key] for key in sorted(browser_keys(client)) if blocks.get(key) and
                 not offline_retry(client, preferences, blocks[key])), '')


def capable(client, job, preferences, cost=None):
    from .bridge_recovery import holding
    from .high_credit import allowed
    from .browser_options import snapshot
    credits = client.get('credits')
    return (not holding(client,preferences) and
            (not snapshot(preferences)['ui_mutex'] or 'ui_mutex_v1' in client.get('capabilities', [])) and
            allowed(client, preferences) and not client.get('blocked') and not block_for(client, preferences)
            and not client.get('batch_running')
            and not client.get('native_busy')
            and (client.get('reuse_recovery') or {}).get('state') != 'stopped'
            and client.get('reset_wait_until', 0) <= time.time()
            and not (client.get('credit_check') or {}).get('hold_dispatch')
            and (client.get('connection_recovery') or {}).get('state') != 'checking'
            and client.get('page_ready') is not False
            and (client.get('prepare_wait') or {}).get('next_at', 0) <= time.time()
            and balance_ready(client, preferences=preferences)
            and credits >= MIN_CREDITS
            and (credits >= cost if cost is not None else credits > 0)
            and dispatch_rank(client, job, preferences) is not None)


def batch_wait(job, batches, cost=None):
    batch = batches.get(job.get('batch_id'), {})
    limit = batch.get('limit_total', 0)
    used = batch.get('reserved', 0)
    if limit and (used >= limit or cost is not None and used + cost > limit):
        return f'整批积分上限：已预留 {used:g} / {limit:g}，停止新增提交'
    return ''


def admitted_queue(rows, batches, observed, *, paused=False, released=()):
    """Only released, budget-admissible work may keep an image claim alive."""
    return [row for row in rows if row['status'] == 'queued'
            and (not paused or row['id'] in released)
            and not batch_wait(row['payload'], batches, estimated_cost(row['payload'], observed))
            and not (row['payload'].get('budget') and estimated_cost(row['payload'], observed) is not None
                     and estimated_cost(row['payload'], observed) > row['payload']['budget'])]


def backup_launch_failure(job, clients, preferences):
    online = {client.get('profile') for client in clients}
    failures = preferences.get('browser_launch_failures', {})
    failed = [profile for profile in configured_profiles(preferences) if profile not in online and
        profile in failures and dispatch_rank({'profile': profile}, job, preferences) is not None]
    if not failed:
        return ''
    profile = failed[0]
    from .problem_details import explain
    return f'{len(failed)} 个备用配置启动检查失败；{profile}：' + explain(failures[profile].get('message') or '浏览器未成功启动')


def waiting_reason(row, clients, rows, preferences, observed, batches, *, paused=False, released=(), affinity=None):
    if row['status'] == 'collected':
        return '此记录仍是“只保存记录”状态，尚未放行生成；可在“选中项操作”中选择“只提交选中项”。'
    if row['status'] != 'queued':
        return row['error']
    if paused and row['id'] not in released:
        return '队列已暂停，等待开始／只提交选中项'
    if (row['remote'].get('ui_permission_retry',{}).get('kind')=='waiting' and
            row['remote'].get('prepare_retry',{}).get('next_at',0)>time.time()):
        return row['error'] or '等待前台UI锁，稍后自动重新申请；不计业务失败次数'
    from .credit_packs import wait_reason as pack_wait_reason
    if row['remote'].get('credit_pack'):
        from .browser_options import snapshot
        pack = row['remote']['credit_pack']
        client = next((c for c in clients if c.get('id') == pack.get('browser')), {})
        lock = client.get('pack_ui') or {}
        if snapshot(preferences)['ui_mutex'] and lock.get('id') == pack.get('id') and lock.get('state') == 'waiting':
            return str(lock.get('message') or '整包保留，等待下一轮UI锁') + '；' + pack_wait_reason(row)
        return pack_wait_reason(row)
    from .image_affinity import allows_image, retained_pool
    waiting = row['remote'].get('small_balance_wait', {})
    if waiting.get('until', 0) > time.time():
        return '优先恢复小余额账号 ' + waiting['profile'] + '，高余额账号暂作备用'
    cost = estimated_cost(row['payload'], observed)
    eligible = [client for client in clients if capable(client, row['payload'], preferences, cost)]
    if eligible and not any(allows_image(client, row['payload'], rows, preferences,
            affinity['images'] if affinity is not None else None) for client in eligible):
        failure = backup_launch_failure(row['payload'], clients, preferences)
        if failure:
            return '现有可用账号被其他参考图独占；' + failure
        return '现有可用账号被其他参考图独占，继续寻找其他已打开账号或启动备用浏览器；原账号只保留原图任务'
    if cost is not None and row['payload'].get('budget') and cost > row['payload']['budget']:
        source = '固定档位费用' if fixed_cost(row['payload']) is not None else '最近确认费用'
        return f'单条预算 {row["payload"]["budget"]} 低于{source} {cost:g}，请修改排队项预算'
    budget = batch_wait(row['payload'], batches, cost)
    if budget:
        return budget
    pool = affinity['pool'] if affinity is not None else retained_pool(clients, rows, preferences)
    if len(pool) >= max(1, min(8, int(preferences.get('concurrency', 2)))):
        return '优先用完已选账号的积分，等待当前账号／图片完成或并行名额；不轮换仍有积分的账号'
    lane = [c for c in clients if dispatch_rank(c, row['payload'], preferences) is not None]
    from .high_credit import reason as high_credit_reason
    from .high_credit import allowed as high_credit_allowed
    high_reasons = [high_credit_reason(c, preferences) for c in lane]
    if high_reasons and all(high_reasons):
        return '排队等待其他可用账号；千分账号未获准使用，仅跳过该账号'
    duration = row['payload'].get('duration')
    label = f'{duration} 秒档' if duration else '通用档'
    if not lane:
        limits = [parameter_block(c, row['payload'], preferences) for c in clients]
        if limits and all(limits):
            return '已连接浏览器暂不支持本条参数：' + str(limits[0]) + '；等待其他账号，可在恢复浏览器后重新检测'
        configured = configured_profiles(preferences)
        useful = [p for p in configured if dispatch_rank({'profile': p}, row['payload'], preferences) is not None]
        if not useful:
            if not configured:
                return '尚无可启动的项目配置：请为一个浏览器保存项目地址；时长由程序自动分配，无需手动分档'
            return f'{label}暂无匹配浏览器：当前配置为手动固定档；可切回“自动分配”后由程序接单'
        holds = [credit_reason({'profile': p}, preferences) for p in useful]
        if holds and all(holds): return '对应浏览器积分冷却中：' + holds[0]
        failure = backup_launch_failure(row['payload'], clients, preferences)
        if failure: return failure
        return f'{label}等待浏览器自动启动／空闲后自动分配时长，无需手动分档'
    if not any(capable(c, row['payload'], preferences, cost) for c in lane):
        funded = [c for c in lane if high_credit_allowed(c, preferences) and balance_ready(c, preferences=preferences)
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
        recovering = next((c for c in lane if c.get('project_missing') and
            (c.get('connection_recovery') or {}).get('message')), None)
        if recovering:
            return str(recovering.get('profile') or '原浏览器') + '：' + recovering['connection_recovery']['message']
        return '扩展已连接，但 Flow 项目页面未响应；请打开对应浏览器检查'
    if not any(c.get('account') for c in lane):
        return '需要登录／确认当前 Flow 账号，请打开对应浏览器'
    if not any(balance_ready(c, preferences=preferences) for c in lane):
        stopped=[c for c in lane if (c.get('reuse_recovery') or {}).get('state')=='stopped']
        if stopped:
            return f'已跳过 {len(stopped)} 个准备失败账号，不占其他账号名额；'+str(stopped[0].get('profile') or '原账号')+'：'+stopped[0]['reuse_recovery'].get('reason','积分未能确认')
        return '等待确认当前账号／重新检测积分；上次余额保留，过期时间可在“时间与重试”设置'
    if not any(capable(c, row['payload'], preferences, cost) for c in lane):
        from .launch_retries import wait_reason as launch_wait_reason
        starting=launch_wait_reason(preferences,[p for p in configured_profiles(preferences)
            if dispatch_rank({'profile':p},row['payload'],preferences) is not None])
        if starting:return starting
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
