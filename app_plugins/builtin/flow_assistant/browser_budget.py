"""Bound physical Flow windows independently of executing task lanes."""
from .browser_slots import limit


def profiles(service):
    # A lost socket/expired startup is not proof that its window disappeared.
    result = set(service.preferences.get('browser_slots', {}))
    for client in service.clients.values():
        profile = client.get('profile')
        if not profile or not client.get('authorized'): continue
        if (client.get('credit_close') or {}).get('state') == 'closed':
            result.discard(profile)
        elif not client.get('credit_observation_only'):
            result.add(profile)
        elif profile not in getattr(service, 'launched', set()):
            result.discard(profile)  # Passive observation of the user's own Chrome.
    return result


def maximum(service):
    raw=service.preferences.get('browser_window_limit')
    return raw if type(raw) is int and limit(service)<=raw<=64 else min(8, 2 * limit(service))


def available(service, profile):
    current = profiles(service)
    return profile in current or len(current) < maximum(service)


def message(service):
    used = len(profiles(service)); normal = limit(service); ceiling = maximum(service)
    if used >= ceiling:
        return (f'Flow 浏览器 {used}/{ceiling}：已到窗口上限（通常 {normal} 个）；暂停补开，'
                '等待空闲故障窗口安全关闭。个人标签页、未完成任务和下载会保留；关闭失败原因见账号行。')
    if used > normal:
        return f'Flow 浏览器 {used}/{ceiling}：临时等待／恢复占用；空闲后收回至 {normal} 个。'
    return ''
