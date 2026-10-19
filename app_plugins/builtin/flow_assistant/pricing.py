"""User-specified Omni tariff, not a claim about Google's official pricing."""

OMNI_DURATION_CREDITS = {6: 10, 8: 12, 10: 15}
RULE_NOTE = '固定积分：6 秒 10；8 秒 12；10 秒 15（每条视频）'


def fixed_cost(job):
    if job.get('model') != 'omni_1_1_flash':
        return None
    duration = job.get('duration')
    if type(duration) is not int:
        return None
    return OMNI_DURATION_CREDITS.get(duration)
