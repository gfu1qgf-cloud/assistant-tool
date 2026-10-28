"""Idle-only closure, physical-window collection and cooled fault retries."""
import time

from .browser_options import snapshot
from .connection_status import browser_keys
from .credit_cooldown import credit_hold
from .problem_details import temporary_code

CAPABILITY = 'automatic_browser_close_v1'
AUTO_CLOSING = '自动关闭核对中／冷却中；不占用其他账号调度，收到关闭确认后按策略重新排队。'


def reserve(service, client, kind):
    profile = client['profile']; key = 'profile:' + profile
    blocks = dict(service.preferences.get('browser_blocks', {}))
    entries = dict(service.preferences.get('browser_lifecycle', {}))
    previous = entries.get(profile, {})
    originals = {k: blocks[k] for k in browser_keys(client) if blocks.get(k)}
    entries[profile] = {**previous, 'state': 'closing', 'kind': kind, 'at': time.time(),
        'original_blocks': originals, 'profile_block': blocks.get(key),
        'count': previous.get('count', 0) + int(kind == 'page_fault')}
    blocks[key] = AUTO_CLOSING
    service.commit_preferences({**service.preferences, 'browser_lifecycle': entries, 'browser_blocks': blocks})


def finish(service, client, success, *, reason=''):
    profile = client.get('profile'); entries = dict(service.preferences.get('browser_lifecycle', {}))
    entry = entries.get(profile)
    if not entry or entry.get('state') not in {'closing','close_failed'}:
        return
    blocks = dict(service.preferences.get('browser_blocks', {})); key = 'profile:' + profile
    if success:
        # A confirmed native exit ends the failed page's recovery epoch.
        # Otherwise a fresh browser inherits reuse_recovery=stopped and is
        # immediately skipped/closed again without ever rechecking credits.
        for recovery_key in list(getattr(service,'reuse_recoveries',{})):
            if recovery_key[0] == profile:service.reuse_recoveries.pop(recovery_key,None)
        getattr(service,'connection_recoveries',{}).pop(profile,None)
        getattr(service,'small_balance_probes',{}).pop(profile,None)
        hold = credit_hold(client, service.preferences)
        count = entry.get('count', 0) + int(entry['kind'] == 'page_fault' and entry['state'] == 'close_failed')
        delay = 5 if entry['kind']=='bridge_update' else 120 if entry['kind']=='idle_capacity' else (60, 180, 1800)[min(2, max(0, count-1))]
        until = hold.get('until', time.time() + 21600) if entry['kind'] == 'low_credit' else time.time() + delay
        entries[profile] = {**entry, 'state': 'cooldown', 'reopen_at': until,
            'count': count}
        client['runtime_note'] = ('积分低于10，浏览器已自动关闭；积分刷新／冷却结束后重新检测。' if entry['kind'] == 'low_credit'
            else '扩展重载未恢复，已确认浏览器安全关闭；5秒后重新加入候选，打开时加载当前磁盘扩展。' if entry['kind']=='bridge_update'
            else '空闲备用浏览器已收回，保留积分和任务记录；需要补位时再打开。' if entry['kind']=='idle_capacity'
            else f'暂时故障，浏览器已安全关闭；{delay}秒后重新加入候选，有任务和窗口名额才重试。')
    else:
        if blocks.get(key) == AUTO_CLOSING:
            if entry.get('profile_block'): blocks[key] = entry['profile_block']
            else: blocks.pop(key)
        attempts = entry.get('close_attempts', 0) + 1
        entries[profile] = {**entry, 'state': 'close_failed', 'next_at': time.time() + (600 if attempts>=3 else 30),
            'last_error':str(reason or client.get('runtime_note') or '扩展未确认安全关闭')[:500],
            'close_attempts': attempts,
            'count': max(0, entry.get('count', 0) - int(entry['kind'] == 'page_fault'))}
    service.commit_preferences({**service.preferences, 'browser_lifecycle': entries, 'browser_blocks': blocks})


def fault_reason(client, preferences):
    from .account_policy import account_hold
    from .runtime_close import PAUSED
    if account_hold(client, preferences) or client.get('blocked') and not temporary_code(client['blocked']): return ''
    blocks = preferences.get('browser_blocks', {})
    reasons = [blocks[key] for key in browser_keys(client) if blocks.get(key)]
    if PAUSED in reasons or any(not temporary_code(reason) for reason in reasons): return ''
    return next((reason for reason in reasons if temporary_code(reason)), '') or (
        str(client['reuse_recovery'].get('reason') or '自动准备连续失败')
        if (client.get('reuse_recovery') or {}).get('state') == 'stopped' else '')


def watch(service):
    if service.closing: return False
    from .runtime_close import request_close, low_balance, occupied, CAPABILITY as CLOSE_CAPABILITY
    from .browser_slots import closed as release_slot
    options = snapshot(service.preferences); now = time.time(); changed = False
    from .credit_policy import balance_ready
    from .account_policy import account_hold
    from .runtime_close import PAUSED
    # A new, verified live page supersedes an old close/cooldown receipt. Do
    # not keep the fresh page permanently excluded or erase its live slot.
    for client in service.clients.values():
        profile=client.get('profile');entry=service.preferences.get('browser_lifecycle',{}).get(profile,{})
        if (not client.get('authorized') or entry.get('state')!='cooldown' or
            client.get('connected',0)<=entry.get('at',0) or
            (client.get('runtime_close') or {}).get('state') in {'checking','closing'} or
            not client.get('page_ready') or not balance_ready(client,preferences=service.preferences) or
            account_hold(client,service.preferences) or credit_hold(client,service.preferences)):continue
        blocks=dict(service.preferences.get('browser_blocks',{}));key='profile:'+profile
        if any(blocks.get(k)==PAUSED or str(blocks.get(k,'')).startswith('用户手动') for k in browser_keys(client)):continue
        if blocks.get(key)==AUTO_CLOSING:blocks.pop(key)
        service.commit_preferences({**service.preferences,'browser_blocks':blocks,'browser_lifecycle':{
            **service.preferences.get('browser_lifecycle',{}),profile:{**entry,'state':'eligible','close_attempts':0,'next_at':0}}})
        for key in ('runtime_close','credit_close'):
            if (client.get(key) or {}).get('state')=='closed':client.pop(key,None)
        from .browser_reuse import reset_recovery
        reset_recovery(service,client);changed=True
    for profile, entry in list(service.preferences.get('browser_lifecycle', {}).items()):
        if entry.get('state') != 'cooldown' or now < entry.get('reopen_at', float('inf')):
            continue
        if entry.get('kind') == 'page_fault' and not options['auto_reopen_page_faults']: continue
        if entry.get('kind') == 'bridge_update' and not options['auto_bridge_recovery']: continue
        # Only an exact native-close receipt creates this cooldown. A timeout
        # or disconnect alone must never authorize another Chrome launch.
        blocks = dict(service.preferences.get('browser_blocks', {}))
        key = 'profile:' + profile
        if blocks.get(key) != AUTO_CLOSING: continue
        blocks.pop(key)
        repairs = dict(service.preferences.get('environment_recoveries', {}))
        for old_key, reason in entry.get('original_blocks', {}).items():
            if blocks.get(old_key) == reason and temporary_code(reason): blocks.pop(old_key)
            if repairs.get(old_key, {}).get('error') == reason and temporary_code(reason): repairs.pop(old_key)
        entries = {**service.preferences['browser_lifecycle'], profile: {**entry, 'state': 'eligible',
            'count':0 if entry.get('count',0)>=3 else entry.get('count',0), 'close_attempts':0}}
        service.commit_preferences({**service.preferences, 'browser_blocks': blocks,
            'environment_recoveries': repairs, 'browser_lifecycle': entries})
        release_slot(service, profile)
        changed = True
    # One outstanding close at a time keeps Chrome and download checks from
    # spiking. Native receipts, not a timer/socket drop, release window budget.
    if any(p.get('action')=='close_browser' for p in service.pending.values()): return changed
    from .browser_budget import profiles as physical_profiles
    from .browser_slots import limit
    from .ordered_dispatch import browser_order
    physical = physical_profiles(service)
    closing = {p for p,e in service.preferences.get('browser_lifecycle',{}).items() if e.get('state')=='closing'}
    surplus = max(0, len(physical-closing)-limit(service))
    clients = sorted(service.clients.values(), key=lambda c:browser_order(c,service.preferences), reverse=True)
    from .policy import admitted_queue,estimated_cost,block_for
    from .routing import dispatch_rank
    rows=service.store.rows()
    queued=admitted_queue(rows,service.store.batches(),service.observed_costs,paused=service.paused,released=service.released)
    head=queued[0]['payload'] if queued else None
    cost=estimated_cost(head,service.observed_costs) if head else None
    candidates=[];idle=[]
    for client in clients:
        if client.get('credit_observation_only'):continue
        if (not client.get('authorized') or CAPABILITY not in client.get('capabilities', []) or
                CLOSE_CAPABILITY not in client.get('capabilities', [])): continue
        entry = service.preferences.get('browser_lifecycle', {}).get(client.get('profile'), {})
        if entry.get('state')=='close_failed' and entry.get('kind')=='low_credit' and entry.get('close_attempts',0)>=3 and not entry.get('parked_close_fix'):
            from .close_receipts import parked
            if parked(service,client):
                # One bounded retry cycle for the old missing-receipt bug.
                entry={**entry,'close_attempts':0,'next_at':0,'parked_close_fix':True}
                service.commit_preferences({**service.preferences,'browser_lifecycle':{
                    **service.preferences.get('browser_lifecycle',{}),client['profile']:entry}})
                changed=True
        if entry.get('state') in {'closing', 'cooldown'} or entry.get('next_at', 0) > now: continue
        if entry.get('close_attempts',0)>=3:
            # Retry a bounded close cycle after ten minutes, without claiming
            # that the original window has exited or launching its replacement.
            entry={**entry,'close_attempts':0}
            service.commit_preferences({**service.preferences,'browser_lifecycle':{
                **service.preferences.get('browser_lifecycle',{}),client['profile']:entry}})
        from .runtime_close import PAUSED
        if any(service.preferences.get('browser_blocks', {}).get(key) == PAUSED for key in browser_keys(client)): continue
        confirmed_account = client.get('account') and client.get('credits_local') and not client.get('identity_unconfirmed')
        kind = 'low_credit' if options['auto_close_low_credit'] and confirmed_account and low_balance(client, service.preferences) else ''
        if not kind and options['auto_reopen_page_faults'] and fault_reason(client, service.preferences):
            kind = 'page_fault'
        if kind: candidates.append((0,client,kind))
        elif (options['auto_close_low_credit'] and head and cost is not None and confirmed_account and
                client.get('profile') in physical and not client.get('blocked') and
                not block_for(client,service.preferences) and
                balance_ready(client,preferences=service.preferences) and
                client['credits']<cost and dispatch_rank(client,head,service.preferences) is not None and not occupied(service,client)):
            # Ten credits can still fund six seconds. Close only the window
            # that cannot fund the FIFO head; retain its balance for later.
            candidates.append((1,client,'idle_capacity'))
        elif surplus and client.get('profile') in physical:
            from .policy import block_for
            from .credit_policy import balance_ready
            if (not occupied(service,client) and not block_for(client,service.preferences) and not client.get('blocked') and client.get('page_ready') and
                    balance_ready(client,preferences=service.preferences) and now-client.get('connected',now)>=15):
                idle.append(client)
    # Keep enough funded idle browsers to fill all execution lanes. Protected
    # failed windows may temporarily account for the other half of the budget;
    # collecting the only usable replacements would serialize the queue again.
    from .credit_packs import active_count
    from .browser_slots import live_starting_profiles
    rows=service.store.rows()
    needed=max(0,limit(service)-active_count(rows)-len(live_starting_profiles(service))) if any(r['status']=='queued' for r in rows) else 0
    for client in idle[:min(surplus,max(0,len(idle)-needed))]:
        candidates.append((2,client,'idle_capacity'))
    for _,client,kind in sorted(candidates,key=lambda item:item[0]):
        if request_close(service, client, automatic_reason=kind): return True
    return changed
