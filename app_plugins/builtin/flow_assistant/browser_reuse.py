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
from .store import TRANSIENT
from .timing import value


def before_new_browser(service, job):
    rows = service.store.rows(); now = time.time(); preferences = service.preferences
    admitted = admitted_queue(rows,service.store.batches(),service.observed_costs,
                              paused=service.paused,released=service.released)
    clients = [c for c in service.clients.values() if c.get('authorized')]
    limit = max(1,min(8,int(preferences.get('concurrency',2))))
    free = min(max(0,limit-sum(r['status'] in TRANSIENT for r in rows)),len(admitted))
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
        state = client.setdefault('reuse_recovery',{'since':now,'credit_count':0})
        if state.get('state') == 'stopped': continue
        identity = ('account',account) if account else ('profile',profile)
        if identity in waiting: continue
        detail = '现有账号优先：'+profile+'；'
        if confirming:
            if now-client.get('connected',now) < value(preferences,'browser_startup_seconds'):
                waiting[identity] = detail+'正在确认原项目连接'
                continue
            state.update(state='stopped',reason='原项目连接确认已超过启动占位时间')
            client['runtime_note']=state['reason']+'；允许检查其他账号'
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
            state.update(state='stopped',reason='原网页操作持续未结束，恢复等待达到启动占位时限')
            client['runtime_note']=detail+state['reason']+'；允许检查其他账号，不刷新在途操作'
            continue
        if not client.get('page_ready'):
            from .connection_recovery import repair_idle
            recovery = service.connection_recoveries.get(profile,{})
            if (recovery.get('count',0) >= value(preferences,'connection_repair_attempts') or
                    not compatible_extension(client.get('extension_version'),minimum_patch=18) and
                    now-state['since'] >= value(preferences,'browser_startup_seconds')):
                state.update(state='stopped',reason='原页面恢复已达设置上限')
                client['runtime_note']=detail+state['reason']+'；允许检查其他账号'
                continue
            repair_idle(service,client)
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
                state.update(state='stopped',reason='原账号积分核对及可用的页面恢复已达上限')
                client['runtime_note']=detail+state['reason']+'；允许检查其他账号'
                continue
            if now >= state.get('credit_next_at',0):
                if service.request(client,'get_points'):
                    key,p=next((key,p) for key,p in service.pending.items() if p['client']==client['id'] and p['action']=='get_points')
                    state.update(credit_request=key,credit_count=state['credit_count']+1,
                                 credit_next_at=now+value(preferences,'credit_retry_seconds'))
            waiting[identity] = detail+'正在重新确认此账号积分'
            continue
        if (client.get('prepare_wait') or {}).get('next_at',0)>now:
            waiting[identity] = detail+'等待此浏览器的参数重试'
            continue
        waiting[identity] = detail+'积分足够，等待原浏览器恢复接单'
    # Prepare existing accounts first, reserving one slot per account. Only
    # genuinely uncovered lanes can request a new profile (with GUI spacing).
    return waiting_reason() if full() else ''


def defer_launch(service, job):
    reason = before_new_browser(service,job)
    waiting = {'job':job.get('id'),'message':reason} if reason else {}
    if getattr(service,'browser_reuse_wait',{}) != waiting:
        service.browser_reuse_wait = waiting
        service.publish()
    return bool(reason)
