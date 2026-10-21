"""A launch timeout is not evidence that a Chrome window has gone away."""
import time

from .connection_status import configured_profiles
from .store import TRANSIENT


def recover_legacy_probe_failures(preferences):
    """Recheck old false negatives once, without losing real launch failures."""
    if preferences.get('extension_probe_revision', 0) >= 2:
        return preferences
    prefix = '扩展未启用或版本无法确认，请在连接向导修正后恢复；自动调度未打开此配置：'
    failures = {profile: entry for profile, entry in preferences.get('browser_launch_failures', {}).items()
                if entry.get('kind') or not str(entry.get('message') or '').startswith(prefix)}
    return {**preferences, 'extension_probe_revision': 2, 'browser_launch_failures': failures}


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


def can_start(service, profile, occupied=None):
    if profile in service.preferences.get('browser_launch_failures', {}):
        return False
    if profile in getattr(service, 'launch_attempts', set()):
        return False
    occupied = occupied_profiles(service) if occupied is None else occupied
    return profile in occupied or len(occupied) < limit(service)


def summary(service):
    profiles = sorted(occupied_profiles(service))
    return {'profiles': profiles, 'used': len(profiles), 'limit': limit(service),
            'message': (f'浏览器启动名额 {len(profiles)}/{limit(service)}：未就绪、断线或等待关闭的原窗口仍占位；'
                        '先恢复原窗口或确认低积分窗口关闭，再启用新配置') if len(profiles) >= limit(service) else ''}


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
    service.commit_preferences({**service.preferences, 'browser_slots': slots, 'browser_launch_failures': failures})


def disconnected(service, client):
    profile = client.get('profile')
    if not profile or (profile not in configured_profiles(service.preferences)
                       and profile not in service.preferences.get('browser_slots', {})):
        return
    if (client.get('credit_close') or {}).get('state') in {'closing', 'closed'}:
        # Only a successful explicit idle/low-credit close can release a slot
        # on disconnect. An ordinary socket drop never proves Chrome exited.
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
    failures = {**service.preferences.get('browser_launch_failures', {}), profile: {
        'at': time.time(), 'message': str(command.get('error') or '浏览器未成功启动')[:500],
        'kind': command.get('kind', 'launch')}}
    service.commit_preferences({**service.preferences, 'browser_slots': slots, 'browser_launch_failures': failures})
    service.launching.pop(profile, None)
