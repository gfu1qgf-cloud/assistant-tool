"""Duration lanes are host preferences, not assumptions about Flow pricing."""
DURATIONS = (4, 6, 8, 10)


def normalize_lane(value):
    if value is None:
        return None
    if isinstance(value, bool) or value not in DURATIONS:
        raise ValueError('浏览器时长分档只能为通用、4、6、8、10 秒。')
    return int(value)


def lane_key(client):
    profile = str(client.get('profile') or '').strip()
    return ('profile', profile) if profile else ('browser', str(client['id']))


def lane_for(client, preferences):
    scope, key = lane_key(client)
    return preferences.get(scope + '_lanes', {}).get(key)


def lane_rank(client, job, preferences):
    lane, duration = lane_for(client, preferences), job.get('duration')
    if lane is not None:
        return 0 if duration == lane else None
    if duration is None or preferences.get('allow_general', True):
        return 1
    return None


def cost_key(job):
    return (job.get('model'), job.get('duration'), job.get('aspect'), job.get('resolution'))


def automatic_lane(client, preferences):
    """Old automatic lanes remain automatic; explicit manual edits fix one."""
    scope, key = lane_key(client)
    return (preferences.get('auto_lane', True) and
            preferences.get(scope + '_lane_modes', {}).get(key) != 'fixed')


def parameter_block(client, job, preferences):
    scope, key = lane_key(client)
    defaults = {'model': 'omni_1_1_flash', 'duration': None, 'aspect': '9:16', 'resolution': '720p'}
    for entry in preferences.get('parameter_blocks', {}).get(scope + ':' + key, []):
        if all(entry.get(field) == job.get(field, fallback) for field, fallback in defaults.items()):
            return entry.get('reason') or '本条时长参数在此浏览器不可用'
    return ''


def dispatch_rank(client, job, preferences):
    if parameter_block(client, job, preferences):
        return None
    rank = lane_rank(client, job, preferences)
    if rank is not None:
        return rank
    if job.get('duration') in (None, *DURATIONS) and automatic_lane(client, preferences):
        return 2  # Busy/account/credit checks happen before assignment.
    return None
