"""Persistent >=100-credit classification; explicit, bounded backup consent."""
import math
import time
import uuid

from .account_policy import identities
from .credit_policy import balance_ready
from .timing import value as timing_value

THRESHOLD = 100


def account_key(client, preferences):
    account = str(client.get('account') or '').strip().casefold()
    if account:
        return account
    links = preferences.get('high_credit_links', {})
    balances = preferences.get('credit_balance_links', {})
    for key in sorted(identities(client)):
        linked = links.get(key) or balances.get(key, {}).get('account')
        if linked:
            return linked
    return ''


def record(client, preferences):
    return preferences.get('high_credit_accounts', {}).get(account_key(client, preferences), {})


def is_high(client, preferences):
    points = client.get('credits')
    return bool(record(client, preferences)) or (bool(client.get('account')) and
        type(points) in (int, float) and math.isfinite(points) and points >= THRESHOLD)


def allowed(client, preferences):
    if not is_high(client, preferences):
        return True
    entry = record(client, preferences)
    if entry.get('permission') in {'allow','deny'}: return entry['permission'] == 'allow'
    return bool(preferences.get('high_credit_session') and entry.get('decision') == 'approved' and
        entry.get('session') == preferences['high_credit_session'] and
        entry.get('cycle') == preferences.get('normal_credit_spends', 0))


def reason(client, preferences):
    if not is_high(client, preferences) or allowed(client, preferences):
        return ''
    entry = record(client, preferences)
    if entry.get('permission') == 'deny':
        return '千分账号：用户已禁止调度；可右键设置使用权限，已提交任务仍保留核对与下载'
    if entry.get('decision') == 'asking':
        return '千分账号（曾检测到至少 100 分）：等待使用确认；窗口显示后才开始倒计时'
    if entry.get('decision') == 'skipped':
        return '千分账号（曾检测到至少 100 分）：本轮已跳过，任务仍保留在队列；可右键设置使用权限'
    return '千分账号（曾检测到至少 100 分）：保留备用；小额账号用完后询问，未确认不接单'


def with_marker(preferences, client):
    account = str(client.get('account') or '').strip().casefold()
    if not account or not is_high(client, preferences):
        return preferences
    entries = dict(preferences.get('high_credit_accounts', {}))
    entries[account] = entries.get(account) or {'detected_at': time.time(), 'decision': 'standby'}
    links = {**preferences.get('high_credit_links', {}), **{key: account for key in identities(client)}}
    return {**preferences, 'high_credit_accounts': entries, 'high_credit_links': links}


def candidates(service):
    from .connection_status import configured_profiles
    from .image_affinity import cached_profile
    online = [c for c in service.clients.values() if c.get('authorized')]
    profiles = {c.get('profile') for c in online}
    return online + [cached_profile(profile, service.preferences)
                     for profile in configured_profiles(service.preferences) if profile not in profiles]


def potential(service, client, queued):
    """Busy is not exhausted. Failed/unsupported/unaffordable accounts are."""
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold, MIN_CREDITS
    from .policy import block_for, estimated_cost
    from .routing import dispatch_rank
    now = time.time()
    preferences = service.preferences
    block = block_for(client, preferences)
    confirming = (client.get('setup_pending') and block == client.get('setup_blocked') and
        now - client.get('connected', 0) < timing_value(preferences, 'browser_startup_seconds'))
    if (account_hold(client, preferences) or credit_hold(client, preferences) or
            block and not confirming or client.get('blocked')):
        return False
    fresh = balance_ready(client, preferences=preferences)
    usable = any(dispatch_rank(client, row['payload'], preferences) is not None and
        (not fresh or client['credits'] >= max(MIN_CREDITS,
            estimated_cost(row['payload'], service.observed_costs) or 0)) for row in queued)
    if not usable:
        return False
    profile = client.get('profile')
    online = bool(client.get('authorized'))
    if online and fresh and client.get('page_ready'):
        return True
    if (online and client.get('page_ready') and not fresh and
            client.get('credit_last_request_observed_at') != client.get('credits_at', 0)):
        return True  # Let the ordinary one-shot balance refresh start before considering a backup.
    if not online and profile in preferences.get('browser_launch_failures', {}):
        failure = preferences['browser_launch_failures'][profile]
        retry = preferences.get('browser_probe_retries', {}).get(profile, {})
        return bool(failure.get('kind') == 'extension_probe_v3' and failure.get('retryable') and
            retry.get('count', timing_value(preferences, 'browser_probe_attempts')) < timing_value(preferences, 'browser_probe_attempts'))
    probe = getattr(service, 'small_balance_probes', {}).get(profile)
    if probe and probe <= now:
        return False
    if not online and profile in service.launched and profile not in service.launching:
        return False
    if not online and profile in service.launching and now - service.launching[profile] >= timing_value(preferences, 'browser_startup_seconds'):
        return False
    if online and now - client.get('connected', now) >= timing_value(preferences, 'browser_startup_seconds'):
        check = client.get('credit_check') or {}
        return check.get('state') == 'checking' and now - check.get('at', 0) < timing_value(preferences, 'request_timeout_seconds')
    return True


def normal_remaining(service, queued):
    return any(not is_high(client, service.preferences) and potential(service, client, queued)
               for client in candidates(service))


def submission_allowed(service, client, row):
    if not allowed(client, service.preferences):
        return False
    if not is_high(client, service.preferences):
        return True
    # A smaller account can finish detecting between two response callbacks,
    # before the next scheduler tick revokes the previous backup permission.
    from .policy import admitted_queue
    queued = admitted_queue(service.store.rows(), service.store.batches(), service.observed_costs,
                            paused=service.paused, released=service.released)
    return not normal_remaining(service, [row] + queued)


def new_session_entries(preferences):
    entries = {}
    for key, original in preferences.get('high_credit_accounts', {}).items():
        entry = dict(original)
        # v1 recorded automatic dismissal as refusal. Re-offer those ambiguous
        # decisions once; explicit v2 refusals/timeouts keep their durable hold.
        if entry.get('decision') == 'asking' or (entry.get('decision') == 'skipped' and not entry.get('decision_source')):
            entry.update(decision='standby', decision_source='interrupted_or_legacy_prompt')
            entry.pop('asked_cycle', None)
        entries[key] = entry
    return entries


def prompt_shown(service, command):
    pending = getattr(service, 'high_credit_pending', None)
    shown_at = command.get('shown_at')
    if (not pending or command.get('nonce') != pending['nonce'] or pending['phase'] != 'queued' or
            type(shown_at) not in (int, float) or not math.isfinite(shown_at) or
            shown_at < pending['issued_at'] or shown_at > time.time() + 1):
        return False
    service.high_credit_pending = {**pending, 'phase': 'shown', 'shown_at': shown_at,
                                  'expires_at': shown_at + pending['seconds']}
    return True


def decide(service, command):
    pending = getattr(service, 'high_credit_pending', None)
    if not pending or command.get('nonce') != pending['nonce']:
        return False
    source = command.get('source', 'user')
    shown = pending['phase'] == 'shown'
    if command.get('approved') is True and not shown:
        return False
    from .policy import admitted_queue
    queued = admitted_queue(service.store.rows(), service.store.batches(), service.observed_costs,
                            paused=service.paused, released=service.released)
    current = [c for c in service.clients.values() if c.get('authorized') and c.get('profile') == pending['profile']]
    unchanged = not current or all(account_key(c, service.preferences) == pending['account'] for c in current)
    needed = (unchanged and bool(queued) and not service.safety_paused and not normal_remaining(service, queued)
              and pending['cycle'] == service.preferences.get('normal_credit_spends', 0))
    timely = shown and time.time() < pending['expires_at']
    approved = command.get('approved') is True and timely and needed
    declined = command.get('approved') is False and source == 'user'
    timeout = shown and (source == 'timeout' or command.get('approved') is True and not timely)
    skipped = declined or timeout and needed
    if approved:
        decision, decision_source = 'approved', 'user_approved'
    elif declined:
        decision, decision_source = 'skipped', 'user_declined'
    elif skipped:
        decision, decision_source = 'skipped', 'timeout'
    else:
        decision, decision_source = 'standby', source if source != 'user' else 'conditions_changed'
    entries = dict(service.preferences.get('high_credit_accounts', {}))
    entries[pending['account']] = {**entries[pending['account']],
        'decision': decision, 'decision_source': decision_source,
        'session': service.preferences['high_credit_session'],
        'cycle': pending['cycle'], 'decided_at': time.time()}
    if not approved and not skipped:
        entries[pending['account']].pop('asked_cycle', None)
        if source == 'not_shown':
            entries[pending['account']]['retry_after'] = time.time() + timing_value(service.preferences, 'request_timeout_seconds')
    service.commit_preferences({**service.preferences, 'high_credit_accounts': entries})
    service.high_credit_pending = None
    return True


def watch(service, queued):
    preferences = service.preferences
    for client in candidates(service):
        preferences = with_marker(preferences, client)
    if preferences != service.preferences:
        service.commit_preferences(preferences)
    normal = bool(queued) and normal_remaining(service, queued)
    pending = getattr(service, 'high_credit_pending', None)
    if pending:
        if pending['phase'] == 'shown' and time.time() >= pending['expires_at']:
            decide(service, {'nonce': pending['nonce'], 'approved': False, 'source': 'timeout'})
        elif pending['phase'] == 'queued' and time.time() - pending['issued_at'] >= timing_value(service.preferences, 'request_timeout_seconds'):
            decide(service, {'nonce': pending['nonce'], 'approved': False, 'source': 'not_shown'})
        # Changing availability never closes a visible question prematurely.
        # Small accounts can proceed concurrently; Yes revalidates eligibility.
    if normal:
        entries = {key: {**entry, 'decision': 'standby'} if entry.get('decision') == 'approved' else entry
                   for key, entry in service.preferences.get('high_credit_accounts', {}).items()}
        if entries != service.preferences.get('high_credit_accounts', {}):
            service.commit_preferences({**service.preferences, 'high_credit_accounts': entries})
        return
    if not queued or service.safety_paused or getattr(service, 'high_credit_pending', None):
        return
    from .store import TRANSIENT, choose_client
    rows = service.store.rows()
    clients = [c for c in service.clients.values() if c.get('authorized')]
    limit = max(1, min(8, int(service.preferences.get('concurrency', 2))))
    if sum(row['status'] in TRANSIENT for row in rows) >= limit:
        return
    from .policy import estimated_cost
    if any(choose_client(clients, rows, limit, row['payload'], service.preferences,
            estimated_cost(row['payload'], service.observed_costs)) is not None for row in queued):
        return  # Use an already approved idle account before asking for another.
    if any(allowed(c, service.preferences) and is_high(c, service.preferences) and
           (not c.get('id') or not c.get('page_ready')) and potential(service, c, queued) for c in candidates(service)):
        return  # Give an approved original browser its bounded startup interval.
    cycle = service.preferences.get('normal_credit_spends', 0)
    options = [c for c in candidates(service) if is_high(c, service.preferences) and
        record(c, service.preferences).get('permission') != 'deny' and
        not allowed(c, service.preferences) and (record(c, service.preferences).get('asked_cycle') != cycle or
            record(c, service.preferences).get('decision') == 'approved' and
            record(c, service.preferences).get('session') != service.preferences.get('high_credit_session')) and
        record(c, service.preferences).get('retry_after', 0) <= time.time() and potential(service, c, queued)]
    if not options:
        return
    client = min(options, key=lambda c: (c.get('credits') if type(c.get('credits')) in (int, float) else 10**9,
                                        c.get('profile', '')))
    account = account_key(client, service.preferences)
    entries = dict(service.preferences['high_credit_accounts'])
    entries[account] = {**entries[account], 'decision': 'asking', 'asked_cycle': cycle}
    service.commit_preferences({**service.preferences, 'high_credit_accounts': entries})
    pending = {'nonce': uuid.uuid4().hex, 'account': account, 'profile': client.get('profile', ''),
        'credits': client.get('credits'), 'cycle': cycle,
        'phase': 'queued', 'issued_at': time.time(), 'expires_at': None,
        'seconds': timing_value(service.preferences, 'high_credit_confirmation_seconds')}
    service.high_credit_pending = pending
    service.publish()  # Persist the skip/ask state before showing UI or dispatching.
    service.high_credit_confirmation_requested.emit(pending)


def note_normal_spend(service, client):
    if not is_high(client, service.preferences):
        service.commit_preferences({**service.preferences,
            'normal_credit_spends': service.preferences.get('normal_credit_spends', 0) + 1})


def retry_selected(service, keys):
    entries = dict(service.preferences.get('high_credit_accounts', {}))
    selected = {account_key(c, service.preferences) for c in candidates(service) if identities(c) & set(keys)}
    for account in selected:
        if account in entries:
            entries[account] = {key: value for key, value in entries[account].items() if key not in {'asked_cycle', 'retry_after'}}
    service.commit_preferences({**service.preferences, 'high_credit_accounts': entries})


def set_permission(service, command):
    permission = command.get('permission')
    if permission not in {'allow','deny','auto'}: raise ValueError('千分账号权限无效')
    keys = set(command.get('keys', []))
    selected = [c for c in candidates(service) if identities(c) & keys and is_high(c, service.preferences)]
    if not selected: raise ValueError('请先选择已识别的千分账号，可选择离线配置')
    preferences = service.preferences
    for client in selected: preferences = with_marker(preferences, client)
    entries = dict(preferences.get('high_credit_accounts', {})); accounts = set()
    for client in selected:
        account = account_key(client, preferences)
        if not account: continue
        accounts.add(account)
        entry = {k:v for k,v in entries.get(account, {}).items() if k not in {'asked_cycle','retry_after','permission'}}
        entry.update(decision='standby', decision_source='manual_permission', decided_at=time.time())
        if permission != 'auto': entry['permission'] = permission
        entries[account] = entry
    service.commit_preferences({**preferences,'high_credit_accounts':entries})
    if getattr(service,'high_credit_pending',None) and service.high_credit_pending['account'] in accounts:
        service.high_credit_pending = None
