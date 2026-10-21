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


def refresh_needed(service, queued_rows):
    from .credit_cooldown import credit_hold
    from .account_policy import account_hold
    from .connection_recovery import occupied
    from .routing import dispatch_rank
    changed = False
    for client in list(service.clients.values()):
        if (not client.get('authorized') or not client.get('page_ready')
                or balance_ready(client, preferences=service.preferences)
                or credit_hold(client, service.preferences) or account_hold(client, service.preferences)
                or occupied(service, client) or client.get('setup_blocked')
                or client.get('credits_waiting_ready')
                or client.get('credit_last_request_observed_at') == client.get('credits_at', 0)):
            continue
        if not any(dispatch_rank(client, row['payload'], service.preferences) is not None for row in queued_rows): continue
        changed |= bool(service.request(client, 'get_points'))
    return changed
