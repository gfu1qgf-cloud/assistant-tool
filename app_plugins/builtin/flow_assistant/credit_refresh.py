"""Account-scoped page deadlines: observe, expire old evidence, then re-read."""
import math
import re
import time


def record(client, preferences):
    account = str(client.get('account') or '').strip().casefold()
    value = preferences.get('credit_refreshes', {}).get(account, {}) if account else {}
    return value if isinstance(value,dict) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(value.get('daily_time',''))) else {}


def deadline(client, preferences):
    value = record(client, preferences).get('next_at')
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else 0


def due(client, preferences, now=None):
    now = time.time() if now is None else now
    at = deadline(client, preferences)
    return bool(at and client.get('credits_at', 0) < at <= now)


def next_daily_time(clock, timezone, now):
    from qt_compat import QtCore
    try:
        zone = QtCore.QTimeZone(timezone.encode('utf-8'))
        if not zone.isValid(): return 0
        local = QtCore.QDateTime.fromSecsSinceEpoch(int(now),zone)
        hour, minute = map(int, clock.split(':'))
        target = QtCore.QDateTime(local.date(),QtCore.QTime(hour,minute),zone)
        if not target.isValid(): return 0
        if target.toMSecsSinceEpoch()/1000 <= now:
            target = QtCore.QDateTime(local.date().addDays(1),QtCore.QTime(hour,minute),zone)
        return target.toMSecsSinceEpoch()/1000 if target.isValid() else 0
    except (ValueError, TypeError, AttributeError): return 0


def observe(service, client, data, *, started_at=0):
    account = str(client.get('account') or '').strip().casefold()
    if not account or not client.get('credits_local'): return
    schedules = dict(service.preferences.get('credit_refreshes', {}))
    if data.get('credit_refresh_checked') is not True:
        previous = record(client,service.preferences); at = deadline(client,service.preferences); now = time.time()
        if not at or now < at: return
        if started_at < at: return started_at  # A pre-refill read arriving late is still old evidence.
        following = next_daily_time(previous['daily_time'],previous.get('timezone'),now)
        if following:
            schedules[account] = {**previous,'next_at':following}
            service.commit_preferences({**service.preferences,'credit_refreshes':schedules})
        return
    raw = data.get('credit_refresh')
    if raw is None:
        schedules.pop(account, None)
    else:
        if (not isinstance(raw, dict) or str(raw.get('account') or '').strip().casefold() != account or
                not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(raw.get('daily_time', '')))): return
        at, observed = raw.get('next_at'), raw.get('observed_at')
        if (any(type(n) not in (int,float) or not math.isfinite(n) for n in (at,observed)) or
                not 0 < at-observed <= 26*3600 or abs(observed-time.time()) > 300): return
        schedules[account] = {'daily_time':raw['daily_time'],'next_at':at,'observed_at':observed,
            'timezone':str(raw.get('timezone') or '')[:100], 'text':str(raw.get('text') or '')[:160]}
    if schedules != service.preferences.get('credit_refreshes', {}):
        service.commit_preferences({**service.preferences, 'credit_refreshes':schedules})
    return raw['observed_at'] if raw is not None else None


def watch(service):
    from .connection_recovery import occupied
    from .store import TRANSIENT
    from .policy import block_for
    from .timing import value
    now = time.time(); changed = False
    if service.safety_paused: return False
    rows = service.store.rows()
    for client in list(service.clients.values()):
        if (not client.get('authorized') or not due(client, service.preferences, now) or
                not client.get('page_ready') or client.get('native_busy') or
                client.get('blocked') not in (None,'','积分不足') or block_for(client, service.preferences) or
                occupied(service, client) or any(p['client']==client['id'] for p in service.pending.values())): continue
        if any(row['status'] in TRANSIENT and (row['browser']==client['id'] or
                row['remote'].get('profile')==client.get('profile')) for row in rows): continue
        at = deadline(client, service.preferences)
        previous = client.get('credit_refresh_probe', {})
        state = previous if previous.get('deadline') == at else {'deadline':at,'count':0,'next_at':0}
        if state['count'] >= value(service.preferences,'credit_read_attempts') or state['next_at'] > now: continue
        if service.request(client,'get_points'):
            client['credit_refresh_probe'] = {**state,'count':state['count']+1,
                'next_at':now+value(service.preferences,'credit_retry_seconds')}
            changed = True
    return changed
