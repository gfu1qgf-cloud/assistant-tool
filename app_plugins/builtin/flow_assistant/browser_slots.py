"""A launch timeout is not evidence that a Chrome window has gone away."""
import time
import hmac

from .connection_status import configured_profiles
from .store import TRANSIENT
from .timing import value as timing_value


def recover_legacy_probe_failures(preferences):
    """Recheck old false negatives once, without losing real launch failures."""
    if preferences.get('extension_probe_revision', 0) >= 3:
        return preferences
    prefix = '扩展未启用或版本无法确认，请在连接向导修正后恢复；自动调度未打开此配置：'
    failures = {profile: entry for profile, entry in preferences.get('browser_launch_failures', {}).items()
                if entry.get('kind') != 'extension_probe_v2' and
                (entry.get('kind') or not str(entry.get('message') or '').startswith(prefix))}
    # This only re-admits a preflight check. The GUI must still verify the
    # exact installed extension before it can open a browser.
    return {**preferences, 'extension_probe_revision': 3, 'browser_launch_failures': failures}


def watch_probe_retries(service):
    failures = service.preferences.get('browser_launch_failures', {})
    retries = service.preferences.get('browser_probe_retries', {})
    limit = timing_value(service.preferences, 'browser_probe_attempts')
    due = {profile for profile, entry in failures.items() if entry.get('kind') == 'extension_probe_v3' and
        entry.get('retryable') and retries.get(profile, {}).get('count', limit) < limit and
        retries.get(profile, {}).get('next_at', float('inf')) <= time.time()}
    if not due: return False
    service.commit_preferences({**service.preferences, 'browser_launch_failures':
        {profile: entry for profile, entry in failures.items() if profile not in due}})
    for profile in due:
        service.launch_attempts.discard(profile)
        service.launched.discard(profile)
        service.launching.pop(profile, None)
        service.small_balance_probes.pop(profile, None)
    return True


def occupied_profiles(service):
    slots = set(service.preferences.get('browser_slots', {}))
    configured = set(configured_profiles(service.preferences))
    for client in service.clients.values():
        if not client.get('authorized') or (client.get('credit_close') or {}).get('state') == 'closed':
            continue
        profile = client.get('profile')
        if profile and (profile in configured or profile in service.launched):
            slots.add(profile)
    for row in service.store.rows():
        if row['status'] in TRANSIENT | {'uncertain', 'download_failed'} and row['remote'].get('profile'):
            slots.add(row['remote']['profile'])
    return slots


def limit(service):
    return max(1, min(8, int(service.preferences.get('concurrency', 2))))


def admission_profiles(service):
    """Count work and pending launches, never idle physical Chrome windows."""
    online = {c.get('profile'): c for c in service.clients.values() if c.get('authorized')}
    slots = {profile for profile, entry in service.preferences.get('browser_slots', {}).items()
             if entry.get('state') == 'opening' and profile not in online}
    slots.update(profile for profile in service.launching if profile not in online or
                 not (online[profile].get('page_ready') and online[profile].get('credits_local')))
    slots.update(row['remote']['profile'] for row in service.store.rows()
                 if row['status'] in TRANSIENT | {'uncertain', 'download_failed'} and row['remote'].get('profile'))
    return slots


def can_start(service, profile, occupied=None):
    # The legacy occupied argument described physical windows; it is no longer a gate.
    if profile in service.preferences.get('browser_launch_failures', {}):
        return False
    if profile in getattr(service, 'launch_attempts', set()):
        return False
    occupied = admission_profiles(service)
    return profile in occupied or len(occupied) < limit(service)


def summary(service):
    profiles = sorted(admission_profiles(service))
    return {'profiles': profiles, 'used': len(profiles), 'limit': limit(service),
            'message': (f'任务／启动名额 {len(profiles)}/{limit(service)}：等待当前任务或启动完成；'
                        '自动关闭已停用，空闲窗口不阻塞换号') if len(profiles) >= limit(service) else ''}


def request_start(service, profile, job=None):
    online = next((c for c in service.clients.values() if c.get('authorized') and c.get('profile') == profile), None)
    if job is not None:
        from .high_credit import allowed
        from .image_affinity import cached_profile
        if not allowed(online or cached_profile(profile, service.preferences), service.preferences):
            return False
    if online:
        # Reopening Chrome is not a page repair. Reuse the original tab.
        from .connection_recovery import repair_idle
        repair_idle(service, online)
        return False
    if not can_start(service, profile):
        return False
    slots = dict(service.preferences.get('browser_slots', {}))
    slots[profile] = {**slots.get(profile, {}), 'at': time.time(), 'state': 'opening'}
    service.commit_preferences({**service.preferences, 'browser_slots': slots})
    if not hasattr(service, 'launch_attempts'): service.launch_attempts = set()
    service.launch_attempts.add(profile)
    service.launched.add(profile)
    service.launching[profile] = service.last_launch = time.time()
    if job is not None:
        service.assign_lane({'profile': profile, 'id': ''}, job)
    service.launch_requested.emit(profile)
    return True


def seen(service, client):
    profile = client.get('profile')
    if not profile or (profile not in configured_profiles(service.preferences)
                       and profile not in service.preferences.get('browser_slots', {})):
        return
    slots = dict(service.preferences.get('browser_slots', {}))
    slots[profile] = {**slots.get(profile, {}), 'state': 'online', 'browser': client.get('id'),
                      'at': slots.get(profile, {}).get('at', time.time())}
    failures = {key: value for key, value in service.preferences.get('browser_launch_failures', {}).items() if key != profile}
    retries = {key: value for key, value in service.preferences.get('browser_probe_retries', {}).items() if key != profile}
    service.commit_preferences({**service.preferences, 'browser_slots': slots, 'browser_launch_failures': failures,
                                'browser_probe_retries': retries})


def disconnected(service, client):
    profile = client.get('profile')
    if not profile or (profile not in configured_profiles(service.preferences)
                       and profile not in service.preferences.get('browser_slots', {})):
        return
    if (client.get('credit_close') or {}).get('state') == 'closed':
        # Only a final native-close receipt proves the vacancy. An interrupted
        # close or a socket drop may leave other windows in this profile open.
        closed(service, profile)
        return
    slots = dict(service.preferences.get('browser_slots', {}))
    slots[profile] = {**slots.get(profile, {}), 'state': 'disconnected', 'browser': client.get('id')}
    service.commit_preferences({**service.preferences, 'browser_slots': slots})


def closed(service, profile):
    slots = {key: value for key, value in service.preferences.get('browser_slots', {}).items() if key != profile}
    if slots != service.preferences.get('browser_slots', {}):
        service.commit_preferences({**service.preferences, 'browser_slots': slots})
    getattr(service, 'launch_attempts', set()).discard(profile)
    service.launching.pop(profile, None)
    service.launched.discard(profile)


def confirm_absence(service, message):
    """A restarted background reports zero native windows without taking a slot."""
    from .setup_support import project_identity
    profile = message.get('profile')
    slot = service.preferences.get('browser_slots', {}).get(profile, {})
    if (message.get('protocol') != 1 or not hmac.compare_digest(str(message.get('token') or ''), service.pair['token']) or
            not slot.get('browser') or slot['browser'] != message.get('browser_id') or
            not project_identity(message.get('project')) or
            project_identity(message['project']) != project_identity(service.preferences.get('profile_projects', {}).get(profile)) or
            any(c.get('authorized') and c.get('profile') == profile and
                (c.get('credit_close') or {}).get('state') != 'closed' for c in service.clients.values())):
        return False
    closed(service, profile)
    return True


def launch_result(service, command):
    profile = command.get('profile')
    entry = service.preferences.get('browser_slots', {}).get(profile)
    if not entry or entry.get('state') != 'opening': return
    if command.get('opened') is True:
        return  # Opening is not yet an authenticated connection.
    if command.get('opened') is not False: return
    # The launcher reports this only when no process was started, so releasing
    # this particular reservation is safe. Do not keep probing it on enqueue.
    slots = {key: value for key, value in service.preferences.get('browser_slots', {}).items() if key != profile}
    if command.get('cancelled'):
        service.commit_preferences({**service.preferences, 'browser_slots': slots})
        getattr(service, 'launch_attempts', set()).discard(profile)
        service.launching.pop(profile, None)
        service.launched.discard(profile)
        return
    retryable = command.get('kind') == 'extension_probe_v3' and command.get('retryable') is True
    retries = dict(service.preferences.get('browser_probe_retries', {}))
    if retryable:
        previous = retries.get(profile, {})
        retries[profile] = {'count': previous.get('count', 0) + 1,
            'next_at': time.time() + timing_value(service.preferences, 'browser_probe_retry_seconds')}
    failures = {**service.preferences.get('browser_launch_failures', {}), profile: {
        'at': time.time(), 'message': str(command.get('error') or '浏览器未成功启动')[:500],
        'kind': command.get('kind', 'launch'), 'retryable': retryable}}
    service.commit_preferences({**service.preferences, 'browser_slots': slots, 'browser_launch_failures': failures,
                                'browser_probe_retries': retries})
    service.launching.pop(profile, None)
