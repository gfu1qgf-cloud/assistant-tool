"""Persist only confirmed account balances and exact profile/project bindings."""
import math
import time

from .account_policy import identities
from .credit_policy import balance_ready
from .setup_support import project_identity


def with_balance(preferences, client, clients):
    account = str(client.get('account') or '').strip().casefold()
    balance, observed = client.get('credits'), client.get('credits_at')
    if (not account or not client.get('credits_local') or type(balance) not in (int, float)
            or not math.isfinite(balance) or balance < 0 or type(observed) not in (int, float)
            or not math.isfinite(observed) or observed <= 0):
        return preferences
    from .high_credit import with_marker
    preferences = with_marker(preferences, client)
    records = dict(preferences.get('credit_balances', {}))
    records[account] = {'account': account, 'balance': balance, 'observed_at': observed,
        'balance_at': time.time(), 'estimated': bool(client.get('credits_estimated'))}
    links = dict(preferences.get('credit_balance_links', {}))
    for current in list(clients) + [client]:
        if str(current.get('account') or '').strip().casefold() != account: continue
        project = project_identity(current.get('saved_project'))
        if project:
            links.update({key: {'account': account, 'project': list(project)} for key in identities(current)})
    return {**preferences, 'credit_balances': records, 'credit_balance_links': links}


def restore_balance(client, preferences, now=None):
    """A cache is an expected account, never proof of the page's actual login."""
    project = project_identity(client.get('saved_project'))
    if not project: return False
    links = preferences.get('credit_balance_links', {})
    for identity in sorted(identities(client)):
        link = links.get(identity, {})
        if not isinstance(link, dict) or link.get('project') != list(project): continue
        record = preferences.get('credit_balances', {}).get(link.get('account'), {})
        if not isinstance(record, dict): continue
        account, balance, observed = record.get('account'), record.get('balance'), record.get('observed_at')
        if (not account or account != link.get('account') or type(balance) not in (int, float)
                or not math.isfinite(balance) or balance < 0 or type(observed) not in (int, float)
                or not math.isfinite(observed) or observed <= 0): continue
        client.update(account=account, credits=balance, credits_at=observed, credits_local=True,
                      credits_cached=True, credits_estimated=bool(record.get('estimated')))
        client['credit_check'] = {'state': 'cached', 'at': observed,
            'message': '已保留上次确认余额；准备和提交前仍核对当前网页账号'}
        return balance_ready(client, now, preferences)
    return False


def refresh_needed(service, queued_rows, *, capacity=None):
    from .browser_reuse import stopped
    from .credit_cooldown import credit_hold
    from .account_policy import account_hold
    from .connection_recovery import occupied
    from .routing import dispatch_rank
    from .ordered_dispatch import browser_order
    from .high_credit import allowed, is_high
    from .policy import block_for, estimated_cost, admitted_queue
    from .image_affinity import allows_image, affinity_context
    from .store import TRANSIENT, choose_client
    rows = service.store.rows()
    clients = [c for c in service.clients.values() if c.get('authorized')]
    admitted = admitted_queue(rows,service.store.batches(),service.observed_costs,
        paused=service.paused,released=service.released)
    affinity = affinity_context(clients,rows,service.preferences,{row['id'] for row in admitted})
    useful = lambda client: any(dispatch_rank(client,row['payload'],service.preferences) is not None and
        allows_image(client,row['payload'],rows,service.preferences,affinity['images']) for row in queued_rows)
    limit = max(1,min(8,int(service.preferences.get('concurrency',2))))
    if queued_rows:
        job = queued_rows[0]['payload']
        ready = choose_client(clients,rows,limit,job,service.preferences,estimated_cost(job,service.observed_costs))
        if ready and not is_high(ready,service.preferences): return False
    if capacity is None:
        capacity = max(0,int(service.preferences.get('concurrency',2))-sum(r['status'] in TRANSIENT for r in rows))
    token = lambda c: ('account',str(c['account']).strip().casefold()) if c.get('account') else ('profile',c.get('profile') or c['id'])
    held = {r['browser'] for r in rows if r['status'] in TRANSIENT}
    reading = {token(c) for c in clients if c.get('id') not in held and not stopped(service,c) and useful(c) and any(
        p['client']==c.get('id') and p['action'] in {'get_points','repair_project'} for p in service.pending.values())}
    changed = False
    for client in sorted(service.clients.values(),key=lambda c:browser_order(c,service.preferences)):
        if len(reading) >= capacity: break
        from .setup_support import required_bridge_update
        if required_bridge_update(client, service.preferences): continue
        if (not client.get('authorized') or not client.get('page_ready')
                or stopped(service,client)
                or balance_ready(client, preferences=service.preferences)
                or credit_hold(client, service.preferences) or account_hold(client, service.preferences)
                or occupied(service, client) or client.get('native_busy') or client.get('setup_blocked')
                or block_for(client,service.preferences) or client.get('blocked') and client['blocked'] != '积分不足'
                or not allowed(client,service.preferences) or token(client) in reading
                or client.get('credits_waiting_ready')
                or client.get('credit_last_request_observed_at') == client.get('credits_at', 0)):
            continue
        if not useful(client): continue
        if service.request(client, 'get_points'):
            changed = True
            reading.add(token(client))
    return changed


def pending_browser_checks(service, queued_rows, affinity):
    """Reserve only a live, bounded read that could supply an admitted job."""
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold
    from .high_credit import allowed
    from .image_affinity import allows_image
    from .policy import block_for
    from .routing import dispatch_rank
    from .timing import request_timeout
    from .browser_reuse import stopped
    now = time.time(); rows = service.store.rows()
    result = set()
    for client in service.clients.values():
        reading = any(request['client'] == client.get('id') and request['action'] in {'get_points','repair_project'} and
                      now-request['at'] < request_timeout(service.preferences,request['action'],client)
                      for request in service.pending.values())
        if (not reading or stopped(service,client) or not client.get('authorized') or not client.get('profile')
                or client.get('blocked') or block_for(client, service.preferences)
                or account_hold(client, service.preferences) or credit_hold(client, service.preferences)
                or not allowed(client, service.preferences) or client.get('setup_blocked')
                or client.get('batch_running')
                or client.get('reset_wait_until', 0) > now): continue
        if any(dispatch_rank(client, row['payload'], service.preferences) is not None and
               allows_image(client, row['payload'], rows, service.preferences, affinity['images'])
               for row in queued_rows): result.add(client['profile'])
    return result
