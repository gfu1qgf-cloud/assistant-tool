"""A launch timeout is not evidence that a Chrome window has gone away."""
import time
import math
import hmac
import re

from .connection_status import configured_profiles
from .store import TRANSIENT
from .timing import value as timing_value


def obsolete_version_failure(entry):
    """Only re-admit a verified bridge rejected by an older program's ceiling."""
    from .setup_support import compatible_extension
    probe = entry.get('probe') or {}
    version = str(probe.get('version') or '')
    if entry.get('kind') != 'extension_probe_v3' or not compatible_extension(version):
        return False
    verified = probe.get('status') == 'enabled' or (probe.get('status') == 'unknown' and
        probe.get('reason') == '已核对本配置安装路径及扩展身份；启动后等待扩展在线确认。')
    match = re.search(r'检测到扩展 ' + re.escape(version) +
        r'，当前程序支持 1\.1\.8～1\.1\.(0|[1-9][0-9]*)；请使程序与桥接扩展版本匹配',
        str(entry.get('message') or ''))
    return bool(verified and match and int(match[1]) < int(version.rsplit('.', 1)[1]))


def recover_legacy_probe_failures(preferences):
    """Recheck old false negatives and newly supported bridges, retaining real faults."""
    if preferences.get('extension_probe_revision', 0) < 4:
        prefix = '扩展未启用或版本无法确认，请在连接向导修正后恢复；自动调度未打开此配置：'
        failures = {profile: entry for profile, entry in preferences.get('browser_launch_failures', {}).items()
                    if entry.get('kind') != 'extension_probe_v2' and
                    (entry.get('kind') or not str(entry.get('message') or '').startswith(prefix)) and
                    not (entry.get('kind') == 'extension_probe_v3' and not entry.get('probe') and
                         str(entry.get('message') or '').endswith('已核对本配置安装路径及扩展身份；启动后等待扩展在线确认。'))}
        preferences = {**preferences, 'extension_probe_revision': 4, 'browser_launch_failures': failures}
    failures = preferences.get('browser_launch_failures', {})
    recovered = {profile for profile, entry in failures.items() if obsolete_version_failure(entry)}
    if not recovered:
        return preferences
    # This only re-admits a preflight check. The GUI must still verify the
    # exact installed extension before it can open a browser. A new rejection
    # records the current ceiling, so another restart cannot clear it again.
    return {**preferences, 'browser_launch_failures':
        {profile: entry for profile, entry in failures.items() if profile not in recovered},
        'browser_probe_retries': {profile: entry for profile, entry in
            preferences.get('browser_probe_retries', {}).items() if profile not in recovered}}


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
    """Count executing work and starts, not accounts holding unresolved results.

    Uncertain/download-failed rows continue to lock their own browser/account
    in choose_client and the dispatch launch filter. They cannot reserve all
    startup lanes and strand unrelated queued work.
    """
    slots = live_starting_profiles(service)
    slots.update(row['remote']['profile'] for row in service.store.rows()
                 if row['status'] in TRANSIENT and row['remote'].get('profile'))
    return slots


def live_starting_profiles(service):
    """An old physical-window record is deduplication, not a perpetual lane."""
    from .browser_reuse import stopped
    now=time.time();maximum=timing_value(service.preferences,'browser_startup_seconds')
    online={c.get('profile'):c for c in service.clients.values() if c.get('authorized')}
    records=service.preferences.get('browser_slots',{})
    def recent(at):
        return type(at) in (int,float) and math.isfinite(at) and at>0 and now-at<maximum
    slots={profile for profile,entry in records.items() if entry.get('state')=='opening' and profile not in online
        and recent(entry.get('opened_at',entry.get('at',0)))}
    for profile,at in service.launching.items():
        client=online.get(profile)
        if not recent(at) or client and stopped(service,client):continue
        if not client or not (client.get('page_ready') and client.get('credits_local')):slots.add(profile)
    return slots


def can_start(service, profile, occupied=None):
    # The legacy occupied argument described physical windows; it is no longer a gate.
    from .launch_retries import cooling
    from .browser_budget import available
    if not available(service, profile): return False
    if cooling(service.preferences,profile):return False
    if profile in service.preferences.get('browser_launch_failures', {}):
        return False
    if profile in getattr(service, 'launch_attempts', set()):
        return False
    if service.preferences.get('browser_slots',{}).get(profile,{}).get('state')=='opening':
        return False  # Expiry permits OTHER profiles; it never proves this window closed.
    occupied = admission_profiles(service)
    return profile in occupied or len(occupied) < limit(service)


def summary(service):
    profiles = sorted(admission_profiles(service))
    unresolved = sum(row['status'] in {'uncertain','download_failed'} for row in service.store.rows())
    message = (f'任务／启动名额 {len(profiles)}/{limit(service)}：等待当前任务或启动完成；'
               '空闲窗口不阻塞换号，自动关闭须先确认原任务与下载已结束') if len(profiles) >= limit(service) else ''
    if unresolved:
        message += ('；' if message else '') + f'待核对／待下载 {unresolved} 条：保留原任务及原账号绑定，不占用其他账号启动名额'
    from .browser_budget import message as budget_message, profiles as physical_profiles, maximum
    physical = physical_profiles(service)
    detail = budget_message(service)
    if detail: message = detail + ('；' + message if message else '')
    return {'profiles': profiles, 'used': len(profiles), 'limit': limit(service),
            'physical_used': len(physical), 'physical_limit': maximum(service),
            'unresolved': unresolved, 'message': message}


def request_start(service, profile, job=None):
    online = next((c for c in service.clients.values() if c.get('authorized') and c.get('profile') == profile), None)
    if job is not None:
        from .high_credit import allowed
        from .image_affinity import cached_profile
        from .policy import block_for
        candidate = online or cached_profile(profile, service.preferences)
        if block_for(candidate, service.preferences) or not allowed(candidate, service.preferences):
            return False
    if online:
        # Reopening Chrome is not a page repair. Reuse the original tab.
        from .connection_recovery import repair_idle
        repair_idle(service, online)
        return False
    if job is not None:
        from .browser_reuse import defer_launch
        if defer_launch(service,job): return False
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
    from .exhausted_browser import remember as remember_exhaustion
    remember_exhaustion(service, client)
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
    if service.preferences.get('browser_lifecycle', {}).get(profile, {}).get('state') in {'closing','close_failed'}:
        from .browser_lifecycle import finish
        finish(service, {'profile': profile}, True)
    closed(service, profile)
    return True


def launch_result(service, command):
    profile = command.get('profile')
    entry = service.preferences.get('browser_slots', {}).get(profile)
    if not entry or entry.get('state') != 'opening': return
    if command.get('opened') is True:
        if not entry.get('opened_at'):
            slots=dict(service.preferences.get('browser_slots',{}))
            slots[profile]={**entry,'opened_at':time.time()}
            service.commit_preferences({**service.preferences,'browser_slots':slots})
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
    from .launch_retries import deferred_result
    if deferred_result(service,command,slots):return
    retryable = command.get('kind') == 'extension_probe_v3' and command.get('retryable') is True
    retries = dict(service.preferences.get('browser_probe_retries', {}))
    if retryable:
        previous = retries.get(profile, {})
        retries[profile] = {'count': previous.get('count', 0) + 1,
            'next_at': time.time() + timing_value(service.preferences, 'browser_probe_retry_seconds')}
    failures = {**service.preferences.get('browser_launch_failures', {}), profile: {
        'at': time.time(), 'message': str(command.get('error') or '浏览器未成功启动')[:500],
        'kind': command.get('kind', 'launch'), 'retryable': retryable,
        **({'probe': command['probe']} if command.get('probe') else {})}}
    service.commit_preferences({**service.preferences, 'browser_slots': slots, 'browser_launch_failures': failures,
                                'browser_probe_retries': retries})
    service.launching.pop(profile, None)
