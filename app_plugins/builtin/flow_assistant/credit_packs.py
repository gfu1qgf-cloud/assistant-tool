"""Credit-bounded, same-image packs; every paid send retains its own attempt."""
import json
import math
import random
import time
import uuid

from .browser_options import snapshot
from .media_identity import generated_bindings
from .policy import admitted_queue, capable, estimated_cost
from .routing import dispatch_rank
from .timing import value

from .pack_planner import UNIT_POINTS as MAX_POINTS, capacity
ACK_SECONDS = 20


def submitted(service, row, data):
    if not row['remote'].get('credit_pack') or not data.get('packed'):
        return row['remote']
    delay = value(service.preferences, 'pack_submit_interval_seconds') + random.uniform(
        0, value(service.preferences, 'pack_submit_jitter_seconds'))
    remote = {**row['remote'], 'pack_accepted_at': time.time(), 'pack_next_at': time.time() + delay}
    return remote


def enabled(preferences):
    return snapshot(preferences)['credit_packs']


def supported(client):
    # Both the current worker and the live page must implement the protocol.
    return ('credit_pack_submit_v1' in client.get('capabilities', []) and
            client.get('credit_pack_submit') is True)


def account(client):
    return str(client.get('account') or '').strip().casefold()


def active_count(rows):
    from .store import TRANSIENT
    keys = set()
    for index, row in enumerate(rows):
        if row['status'] not in TRANSIENT:
            continue
        pack = row['remote'].get('credit_pack') or {}
        key = ('pack', pack.get('id'), pack.get('account'), row.get('browser')) if pack.get('id') else ('job', row.get('id', index))
        keys.add(key)
    return len(keys)


def image_key(row):
    value = row['payload'].get('image_identity', {}).get('sha256')
    return value if isinstance(value, str) and len(value) == 64 else ''


def optimize(rows, credits, batches, observed, *, anchor, client, preferences):
    """Grouped 0/1 knapsack enforces each submission batch's remaining budget.

    State space is bounded by 100 points, independent of the account balance. Equal
    utilization chooses the earliest tuple of queue positions. Only the head's
    exact image hash is eligible, even if another image could spend more.
    """
    if type(credits) not in (int, float) or not math.isfinite(credits) or credits < 10 or not image_key(anchor):
        return []
    pack_capacity = capacity(credits)
    groups = {}
    for index, row in enumerate(rows):
        if (row['status'] != 'queued' or row['remote'].get('credit_pack') or
                image_key(row) != image_key(anchor) or
                row['remote'].get('prepare_retry', {}).get('next_at', 0) > time.time() or
                row['remote'].get('generation_retry') or
                dispatch_rank(client, row['payload'], preferences) is None):
            continue
        cost = estimated_cost(row['payload'], observed)
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost != int(cost) or not 10 <= cost <= pack_capacity:
            continue
        budget = row['payload'].get('budget', 0)
        if budget and cost > budget:
            continue
        group = row['payload'].get('batch_id', '')
        choices=groups.setdefault(group, [])
        if sum(existing_cost==cost for _,existing_cost in choices)<pack_capacity//cost:
            choices.append((index, int(cost)))
    dp = {0: ()}
    for group, choices in groups.items():
        batch = batches.get(group, {})
        remaining = max(0, batch.get('limit_total', 0) - batch.get('reserved', 0)) if batch.get('limit_total') else pack_capacity
        group_limit = min(pack_capacity, int(remaining))
        local = {0: ()}
        for index, cost in choices:
            for spent, selected in list(local.items()):
                total = spent + cost
                candidate = selected + (index,)
                if total <= group_limit and (total not in local or candidate < local[total]):
                    local[total] = candidate
        combined = {}
        for spent, selected in dp.items():
            for extra, indices in local.items():
                total = spent + extra
                candidate = tuple(sorted(selected + indices))
                if total <= pack_capacity and (total not in combined or candidate < combined[total]):
                    combined[total] = candidate
        dp = combined
    return [rows[index] for index in dp[max(dp)]]


def allocate(service, client, rows):
    """Only unpaid queue annotations are allocated; charges remain per send."""
    pack_id = uuid.uuid4().hex
    total = sum(estimated_cost(row['payload'], service.observed_costs) for row in rows)
    common = {'id': pack_id, 'browser': client['id'], 'profile': client.get('profile', ''),
              'account': account(client), 'created': time.time(), 'total': total,
              'balance': client['credits'], 'count': len(rows), 'image': image_key(rows[0])}
    with service.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for index, row in enumerate(rows):
            remote = {**row['remote'], 'credit_pack': {**common, 'position': index + 1}}
            changed = db.execute('UPDATE jobs SET remote=?,updated=? WHERE id=? AND status="queued" AND attempt=?',
                (json.dumps(remote, ensure_ascii=False), time.time(), row['id'], row['attempt'])).rowcount
            if changed != 1:
                raise ValueError('分包期间任务状态已变化，未提交生成。')
    return service.store.get(rows[0]['id'])


def release(service, rows, reason):
    for row in rows:
        if row['status'] != 'queued' or not row['remote'].get('credit_pack'):
            continue
        remote = dict(row['remote'])
        remote.pop('credit_pack', None)
        service.store.update(row['id'], 'queued', remote=remote, error=reason)


def related(row, client):
    return (row['browser'] == client['id'] or row['remote'].get('profile') == client.get('profile') and bool(client.get('profile')) or
            str(row['remote'].get('account') or '').strip().casefold() == account(client) and bool(account(client)))


def planning_clients(service, first, ordered, rows, free, limit, affinity, holds):
    """Look only at distinct, idle, already-open accounts in the same tier."""
    from .store import choose_client
    from .high_credit import is_high
    available=max(1,limit-active_count(rows));result=[first]
    accounts={account(first)};profiles={first.get('profile')};browsers={first['id']}
    image=image_key(free[0]);now=time.time()
    for client in ordered:
        if len(result)>=available:break
        if (client['id'] in browsers or account(client) in accounts or client.get('profile') in profiles or
                not supported(client) or is_high(client,service.preferences)!=is_high(first,service.preferences)):
            continue
        if any(h['browser']==client['id'] or h['profile']==client.get('profile') or h['account']==account(client) for h in holds):continue
        eligible=next((r for r in free if image_key(r)==image and not r['remote'].get('generation_retry') and
            r['remote'].get('prepare_retry',{}).get('next_at',0)<=now and
            choose_client([client],rows,limit,r['payload'],service.preferences,
                estimated_cost(r['payload'],service.observed_costs),affinity=affinity)),None)
        if not eligible:continue
        result.append(client);accounts.add(account(client));profiles.add(client.get('profile'));browsers.add(client['id'])
    return result


def begin(service, row, client):
    """Shared unpaid prepare path for both classic dispatch and packed sends."""
    from .store import fingerprint
    from .browser_slots import seen
    from .browser_reuse import reset_recovery
    from .image_affinity import remember_owner
    from .ordered_dispatch import take
    from .prepare_snapshot import reuse
    service.browser_reuse_wait = {}
    reset_recovery(service, client)
    if not service.media_disks_ready(row):
        return False
    try:
        unchanged = fingerprint(row['payload']['image']) == row['payload']['image_identity']
    except OSError:
        unchanged = False
    if not unchanged:
        service.store.update(row['id'], 'failed', error='参考图片在提交后变化或丢失，请重新添加任务。')
        service.publish()
        return True
    seen(service, client)
    remember_owner(service, row, client)
    take(service, client)
    service.assign_lane(client, row['payload'])
    remote = {'account': client['account'], 'profile': client.get('profile', ''), 'activity_at': time.time()}
    for key in ('prepare_retry', 'upload_recovery', 'credit_pack', 'ui_permission_retry',
                'transfer_retries','upload_reference','upload_phase','upload_cycle'):
        if row['remote'].get(key):
            remote[key] = row['remote'][key]
    if row['remote'].get('generation_retry'):
        remote['generation_retry'] = {**row['remote']['generation_retry'], 'state': 'running'}
    attempt=uuid.uuid4().hex
    if remote.get('transfer_retries',{}).get('upload'):
        remote['transfer_retries']={**remote['transfer_retries'],'upload':{
            **remote['transfer_retries']['upload'],'attempt':attempt}}
    service.store.update(row['id'], 'preparing', attempt=attempt, browser=client['id'], remote=remote)
    job = row['payload']
    if not reuse(service, client, service.store.get(row['id'])):
        service.request(client, 'prepare', {'videoModel': 'omni_1_1_flash', 'duration': job['duration'],
            'aspectRatio': 'ASPECT_RATIO_' + job['aspect'].replace(':', '_'),
            'resolution': 'VIDEO_RESOLUTION_' + job['resolution'].upper(), 'footageType': 'VIDEO_FRAMES', 'count': 1}, row['id'])
    service.publish()
    return True


def dispatch(service, rows, batches, admitted):
    """Fill a pack after exact acceptance, never after an ambiguous paid send."""
    from .store import TRANSIENT, choose_client
    from .ordered_dispatch import snapshot_clients
    from .policy import block_for
    from .balance_dispatch import prefer_smaller
    from .image_affinity import affinity_context
    planned = [r for r in rows if r['status'] == 'queued' and r['remote'].get('credit_pack')]
    if not enabled(service.preferences):
        release(service, planned, '积分分包已关闭，恢复逐条调度；已提交任务继续生成和下载。')
        return False
    clients = [c for c in service.clients.values() if c.get('authorized')]
    admitted_ids = {r['id'] for r in admitted}
    limit = max(1, min(8, int(service.preferences.get('concurrency', 2))))
    # A stale/disconnected allocation only releases unsent members. Paid rows
    # keep their account/media identities and all original recovery protections.
    for pack_id in dict.fromkeys(r['remote']['credit_pack']['id'] for r in planned):
        pending = [r for r in planned if r['remote']['credit_pack']['id'] == pack_id]
        meta = pending[0]['remote']['credit_pack']
        client = next((c for c in clients if c['id'] == meta['browser'] and account(c) == meta['account']), None)
        owned = [r for r in rows if r['remote'].get('credit_pack', {}).get('id') == pack_id and r['status'] != 'queued']
        ambiguous = [r for r in owned if r['status'] in {'submitting', 'generating', 'uncertain'} and
                     (r['status'] == 'uncertain' or not any(len(b.get('mediaIds', [])) == 1 for b in generated_bindings(r['remote'])))]
        invalid = (not client or not supported(client) or client.get('blocked') or
                   block_for(client, service.preferences) or any(r['status'] == 'failed' for r in owned))
        from .pack_mutex import waiting_submission
        if invalid or any(not waiting_submission(service,r) and time.time() - r['remote'].get('pack_sent_at', r.get('updated', time.time())) >= ACK_SECONDS for r in ambiguous):
            release(service, pending, '原分包账号不可用或提交回执超时，未提交的任务已释放回队列；已提交任务保留核对。')
            continue
        if service.safety_paused or not client or client.get('native_busy') or client.get('batch_running') or ambiguous:
            continue
        held = [r for r in rows if related(r, client) and r['status'] in TRANSIENT | {'uncertain', 'download_failed'}]
        if any(r['remote'].get('credit_pack', {}).get('id') != pack_id for r in held) or any(
                h['browser']==client['id'] or h['profile']==client.get('profile') or h['account']==account(client)
                for h in service.store.reset_holds()):
            release(service,pending,'原账号已有其他任务或重置清理占位，未提交分包成员已释放，重新调度。')
            continue
        if any(r['status'] in {'preparing', 'uploading', 'submitting', 'uncertain'} for r in held):
            continue
        if any(p['client'] == client['id'] and p['action'] in {'prepare', 'upload_images', 'run_tasks', 'get_points', 'retry_download'} for p in service.pending.values()):
            continue
        if max((r['remote'].get('pack_next_at', 0) for r in owned), default=0) > time.time():
            continue
        next_row = next((r for r in pending if r['id'] in admitted_ids), None)
        if not next_row:
            if not service.paused:
                release(service,pending,'批次预算或放行条件变化，未提交分包成员已释放。')
            continue
        if (client.get('prepare_wait') or {}).get('next_at',0)>time.time():
            continue
        if not capable(client, next_row['payload'], service.preferences, estimated_cost(next_row['payload'], service.observed_costs)):
            release(service, pending, '分包剩余积分或页面条件变化，未提交任务已释放，重新选择账号。')
            continue
        return begin(service, next_row, client)
    # New packs only use otherwise idle, eligible accounts. Other images and
    # ordinary/high-credit priorities continue through the existing policy.
    if service.safety_paused or active_count(rows) >= limit:
        return False
    free = [r for r in admitted if not r['remote'].get('credit_pack')]
    if not free:
        return False
    anchor = free[0]
    affinity = affinity_context(clients, rows, service.preferences, admitted_ids)
    ordered = snapshot_clients(service, clients)
    holds=service.store.reset_holds()
    for client in ordered:
        if not supported(client):
            continue
        choices = optimize(free, client.get('credits'), batches, service.observed_costs,
                           anchor=anchor, client=client, preferences=service.preferences)
        if not choices:
            continue
        selected = choose_client([client], rows, limit, choices[0]['payload'], service.preferences,
            estimated_cost(choices[0]['payload'], service.observed_costs), affinity=affinity)
        if not selected or prefer_smaller(service, choices[0], client, affinity=affinity):
            continue
        if any(h['browser'] == client['id'] or h['profile'] == client.get('profile') or h['account'] == account(client) for h in holds):
            continue
        from .pack_planner import improve
        peers=planning_clients(service,client,ordered,rows,free,limit,affinity,holds)
        choices=improve(free,peers,choices,batches,service.observed_costs,anchor=anchor,preferences=service.preferences)
        # Lookahead never reserves peers or bypasses the final affordability,
        # account/image ownership and concurrency checks for this real send.
        if not choose_client([client],rows,limit,choices[0]['payload'],service.preferences,
                estimated_cost(choices[0]['payload'],service.observed_costs),affinity=affinity):continue
        first = allocate(service, client, choices)
        return begin(service, first, client)
    return False


def wait_reason(row):
    pack = row['remote'].get('credit_pack')
    if not pack:
        return ''
    return (f"同图积分分包 {pack['position']}/{pack['count']} · {pack['total']:g}/{pack['balance']:g} 分；"
            f"{pack.get('profile') or '原账号'}：等待上一条提交回执及随机间隔后连续提交，生成和下载独立进行")


def ui_burst(service, client):
    """Return the active submission burst, never a cloud-only/download phase."""
    if not enabled(service.preferences) or service.safety_paused:
        return ''
    rows = service.store.rows()
    candidates = [r for r in rows if related(r, client) and r['remote'].get('credit_pack') and
                  r['status'] in {'preparing', 'uploading', 'submitting'}]
    if not candidates:
        candidates = [r for r in rows if r['status'] == 'queued' and r['remote'].get('credit_pack', {}).get('browser') == client['id'] and
                      (not service.paused or r['id'] in service.released)]
    return candidates[0]['remote']['credit_pack']['id'] if candidates else ''
