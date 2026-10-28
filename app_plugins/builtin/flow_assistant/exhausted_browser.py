"""A disconnected exhausted account is not an automatic repair candidate."""
import math
import time

from .setup_support import project_identity

KEY = 'exhausted_browser_disconnects'


def remember(service, client):
    from .credit_refresh import deadline, record, next_daily_time
    points = client.get('credits')
    account = str(client.get('account') or '').strip().casefold()
    project = project_identity(client.get('saved_project'))
    if (not client.get('profile') or not account or not project or not client.get('credits_local')
            or type(points) not in (int, float) or not math.isfinite(points) or not 0 <= points < 10):
        return
    now = time.time()
    refresh = deadline(client, service.preferences)
    observed = client.get('credits_at', now)
    if refresh <= now and not (0 < observed < refresh):
        schedule = record(client, service.preferences)
        refresh = next_daily_time(schedule['daily_time'], schedule.get('timezone'), now) if schedule else 0
    entries = dict(service.preferences.get(KEY, {}))
    entries[client['profile']] = {'account': account, 'project': list(project),
        'balance': points, 'at': now, 'observed_at': observed, 'refresh_at': refresh}
    service.commit_preferences({**service.preferences, KEY: entries})


def reason(client, preferences, now=None):
    # Connected pages can be observed again, including manual use/refills.
    if client.get('id'): return ''
    profile = client.get('profile')
    entry = preferences.get(KEY, {}).get(profile, {})
    slot = preferences.get('browser_slots', {}).get(profile, {})
    if not entry and slot.get('state') != 'disconnected': return ''
    from .image_affinity import cached_profile
    cached = cached_profile(profile, preferences)
    if entry and (entry.get('project') != list(project_identity(cached.get('saved_project')) or ())
                  or entry.get('account') != cached.get('account')):
        return ''
    points = cached.get('credits')
    if type(points) not in (int, float) or not math.isfinite(points): return ''
    if not entry and (slot.get('state') != 'disconnected' or points >= 10): return ''
    now = time.time() if now is None else now
    if points >= 10 and cached.get('credits_at', 0) > entry.get('at', 0): return ''
    from .credit_refresh import deadline, record, next_daily_time
    from .timing import value
    observed = cached.get('credits_at') or entry.get('observed_at') or entry.get('at', 0)
    refresh = entry.get('refresh_at') or deadline(cached, preferences)
    if refresh and refresh <= observed:
        schedule = record(cached, preferences)
        refresh = next_daily_time(schedule['daily_time'], schedule.get('timezone'), observed) if schedule else 0
    known_daily = bool(refresh)
    if not refresh and observed:
        # Missing page labels cannot make a historical low balance permanent.
        # Reopen only for a fresh read, never infer a replenished balance.
        refresh = observed + max(24 * 3600, value(preferences, 'credit_cooldown_seconds'))
    if type(refresh) in (int, float) and math.isfinite(refresh) and refresh > 0 and now >= refresh: return ''
    suffix = ('；到已记录的积分刷新时间后重新检测' if known_daily else
        '；未读取到每日刷新时间，距上次确认至少24小时且冷却结束后重新检测')
    return '低积分账号已断开，跳过自动重开，继续其他账号' + suffix
