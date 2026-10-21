"""Program settings for optional browser foreground activation."""

CONFIG_KEY = 'flow_assistant_browser_options'
DEFAULTS = {'focus_before_action': True}


def normalize(raw):
    if not isinstance(raw, dict) or set(raw) - DEFAULTS.keys():
        raise ValueError('Flow 浏览器操作设置格式无效。')
    if any(type(item) is not bool for item in raw.values()):
        raise ValueError('网页操作前切到前台：请选择开启或关闭。')
    return {**DEFAULTS, **raw}


def snapshot(preferences):
    try:
        return normalize((preferences or {}).get('browser_options', {}))
    except ValueError:
        return {'focus_before_action': False}
