"""Bounded recovery of dispatch stalls; never replay a paid generation."""
import re
import time


def retryable_upload(row):
    info=row['remote'].get('transfer_retries',{}).get('upload',{})
    text=str(info.get('last_error') or row.get('error') or '')
    return (row['status']=='failed' and info.get('stopped') and not info.get('permanent') and
        not re.search(r'图片已变化|图片.*丢失|账号已变化|安全限制|用户取消|用户停止',text) and
        bool(re.search(r'上传.*(?:等待|无进展|素材|超时)|未重复拖放|拖放失败|首帧挂载|素材编号|'
                      r'前台.*(?:锁|互斥|权限)|timeout|No current window|插件.*失效|连接后台失败',text,re.I)))


def eligible(service,client):
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold
    from .connection_status import browser_keys
    from .problem_details import temporary_code
    from .runtime_close import PAUSED
    if (not client.get('authorized') or client.get('credit_observation_only') or
            account_hold(client,service.preferences) or credit_hold(client,service.preferences)):
        return False
    reasons=[service.preferences.get('browser_blocks',{}).get(k) for k in browser_keys(client)]
    return not any(r and (r==PAUSED or not temporary_code(r)) for r in reasons)


def recover(service):
    from .store import submission_risk
    from .connection_recovery import occupied
    from .connection_status import browser_keys
    from .problem_details import temporary_code
    from .browser_reuse import reset_recovery
    from .timing import value
    rows=service.store.rows(); reset=[]; affected=set()
    for row in rows:
        if (row['remote'].get('upload_cycle') and not row['remote']['upload_cycle'].get('confirmed') or
                row['remote'].get('upload_recovery',{}).get('count',0)>=value(service.preferences,'upload_reassign_attempts')):continue
        if not retryable_upload(row) or submission_risk(row,service.store.attempt_charged(row['attempt'])):continue
        client=next((c for c in service.clients.values() if c.get('id')==row['browser']),None)
        if client and not eligible(service,client):continue
        # A disconnected original can be re-assigned, but a manual profile or
        # credit restriction still protects it through a synthetic identity.
        if not client and not eligible(service,{'authorized':True,'id':row['browser'],
                'profile':row['remote'].get('profile'),'account':row['remote'].get('account')}):continue
        reset.append(row);affected.add(row['browser'])
    if reset:
        service.reset_tasks({'ids':[r['id'] for r in reset],'versions':{r['id']:{
            k:r[k] for k in ('attempt','browser','status')} for r in reset}},automatic=True)
    checked=0;blocks=dict(service.preferences.get('browser_blocks',{}))
    repairs=dict(service.preferences.get('environment_recoveries',{}))
    lifecycle=dict(service.preferences.get('browser_lifecycle',{}))
    for client in service.clients.values():
        if not eligible(service,client) or occupied(service,client) or client.get('native_busy'):continue
        if (client.get('runtime_close') or {}).get('state') in {'checking','closing','closed'}:continue
        if any(p['client']==client['id'] and not p.get('retired') for p in service.pending.values()):continue
        profile=client.get('profile');entry=lifecycle.get(profile,{})
        if entry.get('state') in {'closing','cooldown'}:continue
        was_stopped=(client.get('reuse_recovery') or {}).get('state')=='stopped'
        for key in browser_keys(client):
            if temporary_code(blocks.get(key,'')):blocks.pop(key,None);repairs.pop(key,None)
        if entry.get('state')=='close_failed':
            lifecycle[profile]={**entry,'next_at':0,'close_attempts':0}
        reset_recovery(service,client)
        service.connection_recoveries.pop(profile,None)
        service.small_balance_probes.pop(profile,None)
        if temporary_code(client.get('blocked','')):client['blocked']=''
        for key in ('prepare_wait','f5_recovery','connection_recovery','prepare_snapshot'):
            client.pop(key,None)
        client.update(credits_ready_failures=0,credits_retry_at=0,
            credits_waiting_account=False,credits_waiting_ready=False)
        client['browser_recheck_pending']=True
        client['runtime_note']='队列停滞，正在自动重新核对原页面和积分。'
        checked+=1
        # F5 remains idle-only and respects the existing UI mutex. No new tab.
        if was_stopped or client['id'] in affected or not client.get('page_ready'):
            from .runtime_controls import refresh
            refresh(service,client,automatic=True)
    service.commit_preferences({**service.preferences,'browser_blocks':blocks,
        'environment_recoveries':repairs,'browser_lifecycle':lifecycle})
    return len(reset),checked


def details(service):
    from .policy import block_for
    from .credit_policy import balance_ready
    from .runtime_close import occupied
    from .browser_budget import maximum,profiles
    lines=[f'已自动强重试2轮，仍未恢复派发。当前窗口 {len(profiles(service))}/{maximum(service)}。']
    for client in service.clients.values():
        if not client.get('authorized') or client.get('credit_observation_only'):continue
        reason=(block_for(client,service.preferences) or client.get('blocked') or
            (client.get('runtime_close') or {}).get('message') or
            service.preferences.get('browser_lifecycle',{}).get(client.get('profile'),{}).get('last_error') or
            (client.get('reuse_recovery') or {}).get('reason') or client.get('page_error'))
        if not reason:
            reason=('有未完成操作或原结果待核对' if occupied(service,client) else
                '原项目未就绪' if not client.get('page_ready') else
                '未取得当前登录账号的有效积分' if not balance_ready(client,preferences=service.preferences) else
                '请在浏览器与时长分档查看此账号的排队原因')
        lines.append(str(client.get('profile') or '未识别配置')+'：'+str(reason)[:400])
    lines.append('请打开“浏览器与时长分档”查看上述配置：确认原项目能打开、账号已登录及积分可用；个人标签导致无法安全关闭时，请自行收起不需要的窗口。已提交视频保留原绑定，其他账号仍继续调度。')
    return '\n'.join(lines)


def watch(service):
    from model.OfflineMode import is_offline
    from .policy import admitted_queue
    from .credit_packs import active_count
    from .browser_slots import limit
    from .browser_budget import profiles,maximum
    rows=service.store.rows();now=time.time()
    queued=admitted_queue(rows,service.store.batches(),service.observed_costs,
        paused=service.paused,released=service.released)
    state=getattr(service,'scheduler_recovery',{})
    # Only actual submission/result progress resets the incident. Repeated
    # probes, heartbeats and failed preparation attempts cannot hide a stall.
    progress={(r['id'],r['attempt'],r['status']) for r in rows if
        r['status'] in {'submitting','generating','downloading','completed','archiving','download_parked'}}
    previous=getattr(service,'_scheduler_progress',None)
    service._scheduler_progress=progress
    pending_unpaid=any(r['status'] in {'preparing','uploading'} for r in rows)
    if (service.closing or is_offline() or service.safety_paused or not queued and not pending_unpaid or
            service.paused and not service.released or previous is None or progress-previous):
        service._scheduler_wait_since=now
        if state:service.scheduler_recovery={};return True
        return False
    if active_count(rows)>=limit(service):return False
    if not state:
        since=getattr(service,'_scheduler_wait_since',now)
        service._scheduler_wait_since=since
        blocked=any((c.get('reuse_recovery') or {}).get('state')=='stopped' for c in service.clients.values())
        hard=len(profiles(service))>=maximum(service) and blocked
        if not hard and now-since<30:return False
        state=service.scheduler_recovery={'round':0,'next_at':now,'message':''}
    if now<state.get('next_at',0) or state.get('attention'):return False
    if state['round']<2:
        reset,checked=recover(service)
        state.update(round=state['round']+1,next_at=now+45,
            message=f'队列停滞，自动强重试 {state["round"]+1}/2：重新核对 {checked} 个空闲账号，恢复 {reset} 条未提交任务。')
    else:
        state.update(attention=True,message='自动强重试后仍未恢复，请查看阻塞原因。',details=details(service))
        service.scheduler_attention.emit(dict(state))
    return True
