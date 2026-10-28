"""Opening a new account is the last resort, after existing account recovery."""
import time

from .account_policy import account_hold
from .credit_cooldown import credit_hold, MIN_CREDITS
from .credit_policy import balance_ready
from .high_credit import allowed
from .image_affinity import allows_image, affinity_context
from .ordered_dispatch import browser_order
from .policy import admitted_queue, block_for, estimated_cost
from .routing import dispatch_rank
from .setup_support import compatible_extension, project_identity
from .credit_packs import active_count
from .store import TRANSIENT
from .timing import value


def recovery_key(client):
    return (client.get('profile') or client.get('id'), project_identity(client.get('saved_project')))


def recovery_state(service, client):
    states = getattr(service, 'reuse_recoveries', None)
    if states is None: service.reuse_recoveries = states = {}
    state = client.get('reuse_recovery') or states.get(recovery_key(client))
    if state is None:
        starts = [p.get('created_at', p['at']) for p in service.pending.values()
                  if p['client'] == client['id'] and not p.get('job') and p['action'] in {'get_points','repair_project'}]
        state = {'since': min([time.time()] + starts), 'credit_count': 0}
    states[recovery_key(client)] = client['reuse_recovery'] = state
    return state


def stopped(service, client):
    state = client.get('reuse_recovery') or getattr(service, 'reuse_recoveries', {}).get(recovery_key(client), {})
    if state:client['reuse_recovery']=state
    return state.get('state') == 'stopped'


def reset_recovery(service, client):
    client.pop('reuse_recovery', None)
    getattr(service, 'reuse_recoveries', {}).pop(recovery_key(client), None)


def abandon(service, client, reason):
    """Retire only unpaid automatic probes; never cancel an assigned job."""
    state = recovery_state(service, client)
    state.update(state='stopped', reason=reason, stopped_at=time.time())
    client.update(runtime_note=reason+'；已释放准备占位，继续其他账号。可在运行时手动检测积分或恢复此账号。',
        credits_waiting_ready=False, credits_waiting_account=False, credits_retry_at=0,
        credit_check={'state':'stopped', 'at':time.time(), 'message':reason})
    for key, pending in list(service.pending.items()):
        if (pending['client'] == client['id'] and not pending.get('job') and not pending.get('manual_credit') and
                pending['action'] in {'get_points','repair_project','snapshot'}):
            service.pending.pop(key, None)
    from .ui_mutex import cancel_operations
    cancel_operations(service, client, {'get_points','repair_project'})
    service.launching.pop(client.get('profile'), None)
    service.small_balance_probes[client.get('profile')] = time.time()


def before_new_browser(service, job):
    rows = service.store.rows(); now = time.time(); preferences = service.preferences
    admitted = admitted_queue(rows,service.store.batches(),service.observed_costs,
                              paused=service.paused,released=service.released)
    clients = [c for c in service.clients.values() if c.get('authorized')]
    limit = max(1,min(8,int(preferences.get('concurrency',2))))
    free = min(max(0,limit-active_count(rows)),len(admitted))
    if free <= 0: return '当前任务名额已满或没有等待派发的任务'
    online = {c.get('profile') for c in clients}
    opening = [(at,profile) for profile,at in service.launching.items() if profile not in online and
               now-at < value(preferences,'browser_startup_seconds')]
    waiting = {('profile',profile):'等待 '+profile+' 完成启动、连接与积分核对' for _,profile in sorted(opening)}
    def full():
        return len(waiting) >= free
    def waiting_reason():
        return f'正在并行准备 {len(waiting)} 个账号，已覆盖 {free} 个空闲任务名额；'+next(iter(waiting.values()))
    if full(): return waiting_reason()
    affinity = affinity_context(clients,rows,preferences,{row['id'] for row in admitted})
    cost = max(MIN_CREDITS,estimated_cost(job,service.observed_costs) or 0)
    upgrades = []
    for client in sorted(clients,key=lambda c:browser_order(c,preferences)):
        if full(): break
        profile = client.get('profile'); account = str(client.get('account') or '').strip().casefold()
        block = block_for(client,preferences)
        confirming = client.get('setup_pending') and block and block == client.get('setup_blocked')
        stale_low_flag = client.get('blocked') == '积分不足' and not balance_ready(client,preferences=preferences)
        if (not profile or not project_identity(client.get('saved_project')) or
                project_identity(client['saved_project']) != project_identity(preferences.get('profile_projects',{}).get(profile)) or
                not allowed(client,preferences) or account_hold(client,preferences) or credit_hold(client,preferences) or
                block and not confirming or client.get('blocked') and not stale_low_flag or
                (client.get('credit_close') or {}).get('state') == 'closed' or
                dispatch_rank(client,job,preferences) is None or
                not allows_image(client,job,rows,preferences,affinity['images'])): continue
        if balance_ready(client,preferences=preferences) and client['credits'] < cost: continue
        related = [r for r in rows if r['browser'] == client['id'] or r['remote'].get('profile') == profile or
                   account and str(r['remote'].get('account') or '').strip().casefold() == account]
        # Unknown paid results stay protected; they cannot accept another job.
        if any(r['status'] in {'uncertain','download_failed'} for r in related): continue
        # Its own paid/unpaid task already consumes its execution slot. It
        # cannot reserve all of the other free slots as an "existing account".
        if any(r['status'] in TRANSIENT | {'archiving'} for r in related): continue
        from .setup_support import required_bridge_update
        upgrade = required_bridge_update(client, preferences)
        if upgrade:
            upgrades.append((profile, upgrade))
            continue  # Not a failed credit read and cannot be repaired by opening more old bridges.
        state = recovery_state(service,client)
        if state.get('state') == 'stopped': continue
        from .credit_probe import waiting as credit_ui_waiting
        if credit_ui_waiting(service, client):
            waiting[('account',account) if account else ('profile',profile)] = '现有账号优先：'+profile+'；积分检测正在排队等待UI锁，尚未开始读取'
            continue
        maximum = value(preferences,'browser_startup_seconds')
        if now-state['since'] >= maximum:
            last = client.get('page_error') or (client.get('credit_check') or {}).get('message') or client.get('ready_note') or '未收到可用账号和积分'
            abandon(service,client,f'账号准备超过 {maximum:g} 秒，已跳过；最后状态：{last}')
            continue
        identity = ('account',account) if account else ('profile',profile)
        if identity in waiting: continue
        detail = '现有账号优先：'+profile+'；'
        if confirming:
            if now-client.get('connected',now) < value(preferences,'browser_startup_seconds'):
                waiting[identity] = detail+'正在确认原项目连接'
                continue
            abandon(service,client,'原项目连接确认已超过启动占位时间')
            continue
        pending = [(key,p) for key,p in service.pending.items() if p['client'] == client['id']]
        for key,p in pending:
            if p['action'] == 'get_points' and key != state.get('credit_request'):
                state.update(credit_request=key,credit_count=state['credit_count']+1,
                             credit_next_at=p['at']+value(preferences,'credit_retry_seconds'))
        if pending:
            waiting[identity] = detail+'正在核对积分或恢复原页面'
            continue
        if (client.get('native_busy') or client.get('batch_running') or client.get('reset_wait_until',0)>now):
            if now-state['since'] < value(preferences,'browser_startup_seconds'):
                waiting[identity] = detail+'等待原网页操作结束'
                continue
            abandon(service,client,'原网页操作持续未结束，恢复等待达到启动占位时限；不刷新在途操作')
            continue
        if not client.get('page_ready'):
            from .connection_recovery import repair_idle
            recovery = service.connection_recoveries.get(profile,{})
            if (recovery.get('count',0) >= value(preferences,'connection_repair_attempts') or
                    not compatible_extension(client.get('extension_version'),minimum_patch=18) and
                    now-state['since'] >= value(preferences,'browser_startup_seconds')):
                abandon(service,client,'原页面恢复已达设置上限；'+str((client.get('connection_recovery') or {}).get('message') or client.get('page_error') or client.get('ready_note') or '页面控件未就绪'))
                continue
            repair_idle(service,client)
            if (client.get('connection_recovery') or {}).get('state')=='not_sent':
                abandon(service,client,client['connection_recovery']['message'])
                continue
            waiting[identity] = detail+'正在恢复原项目页面'
            continue
        if not balance_ready(client,preferences=preferences):
            limit = value(preferences,'credit_read_attempts')
            attempts = max(state['credit_count'],client.get('credits_ready_failures',0))
            if attempts >= limit:
                from .runtime_controls import refresh
                if not state.get('refreshed') and refresh(service,client,automatic=True):
                    state.update(refreshed=True,credit_count=0)
                    waiting[identity] = detail+'积分读取连续失败，正在对原页执行安全 F5'
                    continue
                abandon(service,client,'原账号积分核对及可用的页面恢复已达上限；'+str((client.get('credit_check') or {}).get('message') or client.get('page_error') or '未读到有效积分'))
                continue
            if now >= state.get('credit_next_at',0):
                if service.request(client,'get_points'):
                    key,p=next((key,p) for key,p in service.pending.items() if p['client']==client['id'] and p['action']=='get_points')
                    state.update(credit_request=key,credit_count=state['credit_count']+1,
                                 credit_next_at=now+value(preferences,'credit_retry_seconds'))
                elif not any(p['client']==client['id'] and p['action']=='get_points' for p in service.pending.values()):
                    abandon(service,client,'积分检测请求未能启动：'+str(client.get('runtime_note') or client.get('page_error') or '浏览器未接受请求'))
                    continue
            waiting[identity] = detail+'正在重新确认此账号积分'
            continue
        if (client.get('prepare_wait') or {}).get('next_at',0)>now:
            waiting[identity] = detail+'等待此浏览器的参数重试'
            continue
        waiting[identity] = detail+'积分足够，等待原浏览器恢复接单'
    # Prepare existing accounts first, reserving one slot per account. Only
    # genuinely uncovered lanes can request a new profile (with GUI spacing).
    if full(): return waiting_reason()
    if upgrades:
        profile, reason = upgrades[0]
        return f'{len(upgrades)} 个已连接配置需更新扩展；{profile}：{reason} 已保留队列，停止为同类故障继续打开备用窗口。'
    from .browser_budget import profiles as physical_profiles, maximum, message
    physical=physical_profiles(service)
    if len(physical) >= maximum(service):
        from .browser_slots import can_start
        from .connection_status import configured_profiles
        # Reuse a reserved, disconnected profile without increasing the
        # window budget. The cap only excludes opening ANOTHER profile.
        reserved=any(p in physical and p not in online and p not in service.launched and
            not block_for({'profile':p},preferences) and can_start(service,p) for p in configured_profiles(preferences))
        if not reserved:return message(service)
    return ''


def defer_launch(service, job):
    reason = before_new_browser(service,job)
    waiting = {'job':job.get('id'),'message':reason} if reason else {}
    if getattr(service,'browser_reuse_wait',{}) != waiting:
        service.browser_reuse_wait = waiting
        service.publish()
    return bool(reason)
