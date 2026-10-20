"""Small balances also take priority while bringing an offline profile online."""
import time

from .account_policy import account_hold
from .credit_cooldown import credit_hold, MIN_CREDITS
from .credit_policy import credit_order, balance_ready
from .connection_status import configured_profiles
from .page_readiness import recoverable_prepare_reason
from .policy import block_for, estimated_cost
from .routing import dispatch_rank
from .timing import value as timing_value
from .image_affinity import retained, allows_image, cached_profile, dispatch_order
from .browser_slots import request_start, can_start, summary as browser_capacity


def profile_order(profile, preferences):
    client = cached_profile(profile, preferences)
    return scheduling_order(client)


def scheduling_order(client):
    # Unknown is a priority hint only; never write it into observed credits.
    return credit_order(client) if type(client.get('credits')) in (int, float) else (0, 50)


def prefer_smaller(service, row, selected):
    if retained(selected, service.preferences):
        return False
    selected_order = dispatch_order(selected, service.preferences)
    now = time.time(); cost = estimated_cost(row['payload'], service.observed_costs)
    probes = getattr(service, 'small_balance_probes', {})
    service.small_balance_probes = probes
    online = {c.get('profile'): c for c in service.clients.values() if c.get('authorized')}
    rows = service.store.rows()
    active = [r for r in rows if r['status'] in {
        'preparing','uploading','submitting','generating','downloading','uncertain','download_failed'}]
    held = {r['remote'].get('profile') for r in active}
    held_accounts = {str(r['remote']['account']).strip().casefold() for r in active if r['remote'].get('account')}
    held.update(h['profile'] for h in service.store.reset_holds())
    options = []
    profiles = list(dict.fromkeys(configured_profiles(service.preferences) + [p for p in online if p]))
    for profile in profiles:
        client = online.get(profile) or cached_profile(profile, service.preferences)
        fresh = balance_ready(client, preferences=service.preferences)
        if (profile in held or str(client.get('account') or '').strip().casefold() in held_accounts or
                fresh and client['credits'] < max(MIN_CREDITS, cost or 0) or
                (0 if retained(client, service.preferences) else 1, *scheduling_order(client)) >= selected_order or
                not allows_image(client, row['payload'], rows, service.preferences) or
                account_hold(client, service.preferences) or credit_hold(client, service.preferences) or
                client.get('setup_blocked') or client.get('batch_running') or client.get('native_busy') or
                dispatch_rank(client, row['payload'], service.preferences) is None): continue
        block = block_for(client, service.preferences) or client.get('blocked')
        if block and not recoverable_prepare_reason(block, client.get('extension_version')): continue
        if probes.get(profile, 0) and probes[profile] <= now: continue
        if profile not in online and profile in service.launched and profile not in service.launching: continue
        options.append(((0 if retained(client, service.preferences) else 1, *scheduling_order(client)), profile, client))
    if not options: return False
    _, profile, client = min(options, key=lambda value: (value[0], profiles.index(value[1])))
    if profile not in online and not can_start(service, profile):
        message = browser_capacity(service)['message'] or '原浏览器启动尚未确认，保留名额等待原连接；不再打开其他配置'
        if row['error'] != message:
            service.store.update(row['id'], 'queued', remote=row['remote'], error=message)
            service.publish()
        return True
    if profile not in probes:
        until = service.launching.get(profile, now) + timing_value(service.preferences, 'browser_startup_seconds')
        probes[profile] = until
        if profile in online:
            if not balance_ready(client, preferences=service.preferences):
                service.request(client, 'get_points')
            if not any(p['client'] == client['id'] and p['action'] == 'snapshot' for p in service.pending.values()):
                service.request(client, 'snapshot')
        else:
            if not request_start(service, profile, row['payload']): return True
    if probes[profile] <= now: return False
    waiting = {'profile':profile, 'until':probes[profile]}
    if row['remote'].get('small_balance_wait') != waiting:
        service.store.update(row['id'], 'queued', remote={**row['remote'], 'small_balance_wait':waiting},
            error='优先恢复小余额账号 ' + profile + '；完成核对后再决定是否使用千分等高余额账号')
        service.publish()
    return True
