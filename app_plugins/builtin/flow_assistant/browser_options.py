"""Program settings for optional browser foreground activation."""

CONFIG_KEY = 'flow_assistant_browser_options'
DEFAULTS = {'focus_before_action': True, 'visual_upload_fallback': True, 'ui_mutex': False,
            'credit_packs': True, 'auto_close_low_credit': True, 'auto_reopen_page_faults': True,
            'auto_bridge_recovery': True}


def normalize(raw):
    if not isinstance(raw, dict) or set(raw) - DEFAULTS.keys():
        raise ValueError('Flow 浏览器操作设置格式无效。')
    if any(type(item) is not bool for item in raw.values()):
        raise ValueError('Flow 浏览器操作选项：请选择开启或关闭。')
    return {**DEFAULTS, **raw}


def snapshot(preferences):
    try:
        return normalize((preferences or {}).get('browser_options', {}))
    except ValueError:
        return {key:False for key in DEFAULTS}
