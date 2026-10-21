"""Durable six-hour holds for balances below the minimum usable 10 credits."""
import math
import time

from .account_policy import identities, account_hold
from .credit_cache import with_balance
from .timing import value as timing_value, duration_label

MIN_CREDITS = 10
COOLDOWN_SECONDS = 6 * 60 * 60


def credit_record(client, preferences):
    records = preferences.get('credit_cooldowns', {})
    account = str(client.get('account') or '').strip().casefold()
    if account:
        value = records.get(account)
        return value if isinstance(value, dict) else {}
    links = preferences.get('credit_cooldown_links', {})
    for identity in sorted(identities(client)):
        value = records.get(links.get(identity))
        if isinstance(value, dict): return value
    return {}


def credit_hold(client, preferences, now=None):
    now = time.time() if now is None else now
    record = credit_record(client, preferences)
    until = record.get('until')
    return record if type(until) in (int, float) and math.isfinite(until) and until > now else {}


def credit_reason(client, preferences, now=None):
    hold = credit_hold(client, preferences, now)
    if not hold: return ''
    deadline = time.strftime('%m-%d %H:%M', time.localtime(hold['until']))
    duration = duration_label(hold['until'] - hold['at'])
    return f'积分低于 10，冷却 {duration}至 {deadline}；手动检测积分可提前重新确认'


def remember_balance(service, client, *, manual=False, source='page'):
    """Persist before dispatch; a late automatic read cannot release an active hold."""
    account = str(client.get('account') or '').strip().casefold()
    points = client.get('credits')
    if (not account or not client.get('credits_local') or type(points) not in (int, float)
            or not math.isfinite(points) or points < 0):
        return
    now = time.time()
    records = dict(service.preferences.get('credit_cooldowns', {}))
    links = dict(service.preferences.get('credit_cooldown_links', {}))
    related = [c for c in service.clients.values() if
               str(c.get('account') or '').strip().casefold() == account]
    for current in related + [client]:
        links.update({key: account for key in identities(current)})
    held = credit_hold({'account': account}, service.preferences, now)
    if points < MIN_CREDITS:
        if not held or manual:
            records[account] = {'account': account, 'balance': points, 'at': now,
                                'until': now + timing_value(service.preferences, 'credit_cooldown_seconds'), 'source': source}
        elif points < held['balance']:
            records[account] = {**held, 'balance': points}
    elif manual or not held:
        records.pop(account, None)
    preferences = with_balance({**service.preferences, 'credit_cooldowns': records,
                                'credit_cooldown_links': links}, client, service.clients.values())
    if preferences != service.preferences: service.commit_preferences(preferences)
    for current in related + [client]:
        if manual or not held:
            current.pop('credit_close', None)
        if points < MIN_CREDITS or held and not manual:
            current.update(credit_cooldown_waiting=True, credits_waiting_ready=False,
                           credits_waiting_account=False, credits_retry_at=0)
            # Release this startup slot now. Once the hold expires, an offline
            # profile may be launched once again in the ordinary candidate list.
            service.launching.pop(current.get('profile'), None)
            service.launched.discard(current.get('profile'))
        else:
            current['credit_cooldown_waiting'] = False
            if current.get('blocked') == '积分不足': current['blocked'] = ''


def refresh_expired(service, queued_rows):
    """One fresh read when queued work needs a connected, expired account."""
    from .connection_recovery import occupied
    from .routing import dispatch_rank
    changed = False
    for client in list(service.clients.values()):
        if (not client.get('authorized') or not client.get('credit_cooldown_waiting')
                or credit_hold(client, service.preferences) or account_hold(client, service.preferences)
                or not client.get('page_ready') or client.get('setup_blocked') or occupied(service, client)):
            continue
        if not any(dispatch_rank(client, row['payload'], service.preferences) is not None for row in queued_rows):
            continue
        if any(p['client'] == client['id'] and p['action'] == 'get_points' for p in service.pending.values()):
            continue
        client['credit_cooldown_waiting'] = False
        client['credits_ready_failures'] = 0
        changed |= bool(service.request(client, 'get_points'))
    return changed


def close_idle_exhausted(service):
    """Compatibility entry point: exhausted browsers now remain open."""
    return False
