"""UI contention before Chrome starts is backpressure, not an account fault."""
import math
import re
import time
from .timing import value

KIND = 'ui_launch_wait'

def ui_wait_error(error):
    text=str(error or '')
    return bool(re.fullmatch(r'启动浏览器等待UI互斥超过 [0-9.]+ 秒，已跳过此配置；任务保留，继续其他账号。',text) or
        text == '启动浏览器的UI互斥等待超时或已取消；已跳过此配置，任务保留原队列。' or
        text == 'UI互斥服务尚未就绪，未启动浏览器；请等主程序连接服务就绪后重试。')

def recover_legacy(preferences):
    failures=preferences.get('browser_launch_failures',{})
    recovered={profile for profile,entry in failures.items() if
        (entry.get('kind')==KIND or entry.get('kind','launch')=='launch' and ui_wait_error(entry.get('message'))) and
        not preferences.get('browser_slots',{}).get(profile,{}).get('opened_at')}
    if not recovered:return preferences
    slots={key:entry for key,entry in preferences.get('browser_slots',{}).items() if key not in recovered or entry.get('state')!='opening'}
    return {**preferences,'browser_launch_failures':{key:entry for key,entry in failures.items() if key not in recovered},
            'browser_slots':slots}

def cooling(preferences, profile, now=None):
    deadline=preferences.get('browser_ui_waits',{}).get(profile,{}).get('next_at',0)
    return type(deadline) in (int,float) and math.isfinite(deadline) and deadline>(time.time() if now is None else now)

def deferred_result(service, command, slots):
    """Called only for an explicit opened=False receipt from the GUI."""
    if command.get('opened') is not False or not (
            command.get('kind')==KIND or ui_wait_error(command.get('error'))):return False
    # A late pre-launch error cannot revoke an already confirmed process start.
    if service.preferences.get('browser_slots',{}).get(command['profile'],{}).get('opened_at'):return True
    profile=command['profile'];now=time.time()
    waiting=dict(service.preferences.get('browser_ui_waits',{}));previous=waiting.get(profile,{})
    waiting[profile]={'at':now,'next_at':now+value(service.preferences,'browser_ui_retry_seconds'),
                      'count':previous.get('count',0)+1,'reason':str(command.get('error') or '启动前等待UI权限超时')[:500]}
    service.commit_preferences({**service.preferences,'browser_slots':slots,'browser_ui_waits':waiting})
    service.launching.pop(profile,None);service.launched.discard(profile)
    getattr(service,'launch_attempts',set()).discard(profile)
    service.small_balance_probes.pop(profile,None)
    return True

def wait_reason(preferences, profiles):
    now=time.time()
    waiting=[(entry['next_at'],profile) for profile in profiles
             if cooling(preferences,profile,now) for entry in [preferences['browser_ui_waits'][profile]]]
    if waiting:
        deadline,profile=min(waiting)
        return f'当前已连接账号积分不足；{profile} 启动前等待UI权限超时，{max(1,math.ceil(deadline-now))} 秒后自动重试；账号未判故障'
    def starting(profile):
        entry=preferences.get('browser_slots',{}).get(profile,{})
        at=entry.get('opened_at') or entry.get('at',0)
        return (entry.get('state')=='opening' and type(at) in (int,float) and
                math.isfinite(at) and at>0 and now-at<value(preferences,'browser_startup_seconds'))
    opening=[profile for profile in profiles if starting(profile)]
    if opening:return '当前已连接账号积分不足；正在等待备用浏览器启动／UI权限：'+opening[0]
    return ''
