"""Read-only, socket-free view of the worker's actual foreground leases."""
import time

from .browser_options import snapshot as browser_options
from .timing import value

OPERATIONS={'get_points':'检测积分','prepare':'准备参数','upload_images':'上传／选择参考图',
    'submit':'提交生成','retry_download':'唤醒视频／重试下载','focus_window':'激活窗口',
    'repair_project':'刷新／恢复原项目','open_project':'打开原项目','browser_launch':'启动浏览器'}


def snapshot(service,now=None):
    from .ui_lease import deadline
    now=time.time() if now is None else now
    current=getattr(service,'ui_mutex',{})
    enabled=browser_options(service.preferences)['ui_mutex']
    clients={c.get('id'):c for c in service.clients.values()}
    def profile(entry):
        client=clients.get(entry.get('client'),{})
        return str(entry.get('profile') or client.get('profile') or '未识别浏览器')
    def entry_view(entry,owner=False):
        result={'profile':profile(entry),'host':bool(entry.get('host')),
            'operation':OPERATIONS.get(entry.get('operation'),'页面操作'),
            'since':entry.get('at' if owner else 'queued_at',now)}
        if owner:
            from .pack_mutex import requests
            result['jobs']={p['job']:p['attempt'] for p in requests(service,entry) if p.get('job')}
            result['expires_at']=min(entry.get('cancel_deadline',float('inf')),deadline(service,entry))
            result['stopping']=bool(entry.get('stopping'))
        return result
    owner=current.get('owner') if enabled else None
    queue=list(current.get('queue',[])) if enabled else []
    burst=current.get('burst') if enabled else None
    priority=None
    if burst:
        # Project the pack's priority without advancing its clock or granting
        # anyone a lease. A reservation and an actual owner stay distinct.
        mine=[e for e in queue if not e.get('host') and e.get('client')==burst.get('client')]
        queue=mine+[e for e in queue if e not in mine]
        elapsed=burst.get('elapsed',0)+ (max(0,now-burst['clock_at']) if burst.get('clock_at') is not None else 0)
        priority={'profile':profile(burst),'remaining':max(0,value(service.preferences,'pack_ui_slice_seconds')-elapsed)
            if burst.get('started_at') is not None else None}
    represented={e['profile'] for e in ([entry_view(owner,True)] if owner else [])+[entry_view(e) for e in queue] if e['host']}
    launches=[{'profile':e['profile'],'since':e['at'],'operation':'等待启动浏览器'}
        for e in getattr(service,'ui_launch_waits',{}).values() if enabled and e['profile'] not in represented]
    return {'enabled':enabled,'observed_at':now,'owner':entry_view(owner,True) if owner else None,
        'queue':[entry_view(e) for e in queue],'priority':priority,'launches':launches}


def seconds(since,now):
    return max(0,int(now-since))


def summary(data,now=None):
    now=time.time() if now is None else now
    if not data:return 'UI锁：等待主程序同步状态'
    if not data['enabled']:return 'UI锁：已关闭'
    owner=data.get('owner');parts=['UI锁：空闲']
    if owner:
        left=seconds(now,owner['expires_at'])
        parts=[f'UI锁：{owner["profile"]} · {owner["operation"]} · 已持有 {seconds(owner["since"],now)} 秒',
            '正在停止旧操作，确认后释放' if owner.get('stopping') else f'租约剩余 {left} 秒' if left else '租约到期，待确认释放']
    if data.get('priority'):
        parts.append('整包优先：'+data['priority']['profile'])
    parts.append(f'排队 {len(data.get("queue",[]))}')
    if data.get('launches'):parts.append(f'待启动 {len(data["launches"])}')
    age=seconds(data['observed_at'],now)
    if age>=5:parts.insert(0,f'状态 {age} 秒未更新，显示最后记录')
    return ' · '.join(parts)
