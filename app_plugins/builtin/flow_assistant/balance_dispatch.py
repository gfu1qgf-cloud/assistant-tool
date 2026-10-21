"""Small balances also take priority while bringing an offline profile online."""
import time

from .account_policy import account_hold
from .credit_cooldown import credit_hold, credit_record, MIN_CREDITS
from .credit_policy import credit_order, balance_ready
from .connection_status import configured_profiles
from .page_readiness import recoverable_prepare_reason
from .policy import block_for, estimated_cost, admitted_queue
from .routing import dispatch_rank
from .timing import value as timing_value
from .image_affinity import retained, allows_image, cached_profile, dispatch_order, affinity_context
from .browser_slots import request_start, can_start


def refresh_probe_cycle(service, rows):
    from .image_affinity import PENDING
    batches = {row['payload'].get('batch_id') or row['id'] for row in rows if row['status'] in PENDING}
    previous = getattr(service, '_probe_batches', None)
    if previous is not None and batches != previous and not batches & previous:
        service.small_balance_probes.clear()
    service._probe_batches = batches


def profile_order(profile, preferences):
    client = cached_profile(profile, preferences)
    return scheduling_order(client)


def scheduling_order(client):
    # Unknown is a priority hint only; never write it into observed credits.
    return credit_order(client) if type(client.get('credits')) in (int, float) else (0, 50)


def can_fund(client, job, preferences, observed):
    # An unknown/expired balance may be probed, never treated as paid credit.
    # The cooldown has its own deadline: after it ends, a low account may be
    # reopened for a fresh observation even if balance caching is indefinite.
    if (type(client.get('credits')) in (int, float) and client['credits'] < MIN_CREDITS
            and credit_record(client, preferences) and not credit_hold(client, preferences)):
        return True
    return not balance_ready(client, preferences=preferences) or client['credits'] >= max(
        MIN_CREDITS, estimated_cost(job, observed) or 0)


def prefer_smaller(service, row, selected, *, affinity=None):
    from .high_credit import allowed
    selected_order = dispatch_order(selected, service.preferences)
    now = time.time(); cost = estimated_cost(row['payload'], service.observed_costs)
    probes = getattr(service, 'small_balance_probes', {})
    service.small_balance_probes = probes
    online = {c.get('profile'): c for c in service.clients.values() if c.get('authorized')}
    rows = service.store.rows()
    if affinity is None:
        admitted = admitted_queue(rows, service.store.batches(), service.observed_costs,
            paused=service.paused, released=service.released)
        affinity = affinity_context(list(service.clients.values()), rows, service.preferences,
                                    {item['id'] for item in admitted})
    active = [r for r in rows if r['status'] in {
        'preparing','uploading','submitting','generating','downloading','uncertain','download_failed'}]
    held = {r['remote'].get('profile') for r in active}
    held_accounts = {str(r['remote']['account']).strip().casefold() for r in active if r['remote'].get('account')}
    resets = service.store.reset_holds()
    held.update(h['profile'] for h in resets)
    held_accounts.update(str(h['account']).strip().casefold() for h in resets if h['account'])
    options = []
    profiles = list(dict.fromkeys(configured_profiles(service.preferences) + [p for p in online if p]))
    for profile in profiles:
        client = online.get(profile) or cached_profile(profile, service.preferences)
        if profile not in online and profile in service.preferences.get('browser_launch_failures', {}): continue
        fresh = balance_ready(client, preferences=service.preferences)
        if (not allowed(client, service.preferences) or profile in held or str(client.get('account') or '').strip().casefold() in held_accounts or
                fresh and client['credits'] < max(MIN_CREDITS, cost or 0) or
                dispatch_order(client, service.preferences) >= selected_order or
                not allows_image(client, row['payload'], rows, service.preferences, affinity['images']) or
                account_hold(client, service.preferences) or credit_hold(client, service.preferences) or
                client.get('setup_blocked') or client.get('batch_running') or client.get('native_busy') or
                client.get('reset_wait_until', 0) > now or
                dispatch_rank(client, row['payload'], service.preferences) is None): continue
        block = block_for(client, service.preferences) or client.get('blocked')
        if block and not recoverable_prepare_reason(block, client.get('extension_version')): continue
        if probes.get(profile, 0) and probes[profile] <= now: continue
        if profile not in online and profile in service.launched and profile not in service.launching: continue
        options.append((dispatch_order(client, service.preferences), profile, client))
    if not options: return False
    _, profile, client = min(options, key=lambda value: (value[0], profiles.index(value[1])))
    if profile not in online and not can_start(service, profile):
        # A preference for an unopened backup cannot veto funded, idle work
        # already selected in an occupied window. Keep the window limit, but
        # make progress with the best currently usable account.
        return False
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
