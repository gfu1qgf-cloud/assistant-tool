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


def dispatch_rank(client, job, preferences):
    rank = lane_rank(client, job, preferences)
    if rank is not None:
        return rank
    if job.get('duration') in DURATIONS and automatic_lane(client, preferences):
        return 2  # Busy/account/credit checks happen before assignment.
    return None
