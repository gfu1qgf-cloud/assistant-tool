"""Bounded repair of an idle bridge; generation ownership always takes priority."""
import time

from .account_policy import account_hold
from .credit_cooldown import credit_hold
from .setup_support import project_identity, compatible_extension
from .store import TRANSIENT
from .store import submission_risk
from .timing import value as timing_value, delay
from .transfer_policy import retry_info


def occupied(service, client):
    account = str(client.get('account') or '').strip().casefold()
    if client.get('batch_running') or client.get('reset_wait_until', 0) > time.time(): return True
    for row in service.store.rows():
        if row['status'] not in TRANSIENT | {'archiving', 'uncertain', 'download_failed'}: continue
        if not (row['browser'] == client.get('id') or row['remote'].get('profile') == client.get('profile') or
                account and str(row['remote'].get('account') or '').strip().casefold() == account): continue
        # An unpaid transfer waiting for this page must not block its own
        # recovery. Live native operations and all paid/unknown results do.
        waiting = (bool(row['attempt']) and (row['browser'] == client.get('id') or
            row['remote'].get('profile') and row['remote']['profile'] == client.get('profile')) and
            (row['status'] == 'uploading' and retry_info(row, 'upload').get('next_at') or
             row['status'] == 'preparing' and row['remote'].get('connection_wait_at')) and not submission_risk(row,
            service.store.attempt_charged(row['attempt'])) and not any(p['job'] == row['id'] and
                p['attempt'] == row['attempt'] for p in service.pending.values()))
        if not waiting: return True
    return False


def repair_idle(service, client):
    from .browser_reuse import stopped
    if stopped(service,client):return False
    if client.get('credit_observation_only'):
        from .passive_credits import flow_interest
        if not flow_interest(service,client):return False
        client['credit_observation_only']=False
    profile = client.get('profile')
    saved = project_identity(client.get('saved_project'))
    startup = service.preferences.get('browser_slots', {}).get(profile, {}).get('project_startup', {})
    if (startup.get('session') == service.project_startup_session and startup.get('state') in {'opening','waiting'} and
            time.time() - startup.get('at', 0) < timing_value(service.preferences, 'browser_startup_seconds')):
        return False  # Let the requested original browser/project finish opening.
    if (not client.get('authorized') or not compatible_extension(client.get('extension_version'), minimum_patch=18)
            or not profile or not saved or saved != project_identity(service.preferences.get('profile_projects', {}).get(profile))
            or account_hold(client, service.preferences) or credit_hold(client, service.preferences) or occupied(service, client)):
        return False
    if any(p['client'] == client['id'] and p['action'] == 'repair_project' for p in service.pending.values()):
        return False
    attempts = service.connection_recoveries.get(profile, {})
    limit = timing_value(service.preferences, 'connection_repair_attempts')
    if attempts.get('count', 0) >= limit:
        last=attempts.get('last_error') or client.get('ready_note') or client.get('page_error') or '已请求恢复原项目，但未收到控件就绪状态'
        client['connection_recovery'] = {**attempts, 'state': 'stopped',
            'message': f'已发送 {limit:g} 次原页面恢复请求，仍未就绪；最后原因：{last}'}
        return False
    if attempts.get('next_at', 0) > time.time():
        return False
    count = attempts.get('count', 0) + 1
    if not service.request(client, 'repair_project', {'idle': True, 'url': client['saved_project'], 'force_reload':count >= 2}):
        client['connection_recovery']={**attempts,'state':'not_sent','message':'页面恢复请求未能发出：'+str(
            client.get('runtime_note') or client.get('page_error') or '浏览器连接未接受指令')}
        return False
    state = {**attempts, 'count': count, 'next_at': time.time() + delay(service.preferences, 'connection_repair_delays', count-1), 'state': 'checking',
        'message': f'页面消息通道未就绪，正在自动修复 {count}/{limit}；只恢复当前空闲项目'}
    service.connection_recoveries[profile] = state
    client['connection_recovery'] = state
    for pending in service.pending.values():
        if pending['client']==client['id'] and pending['action']=='repair_project':
            pending['connection_repair_previous']=count-1
    return True


def repair_response(service, client, request, message):
    managed='connection_repair_previous' in request
    current=dict(client.get('connection_recovery') or {})
    busy=message.get('code')=='FLOW_PAGE_REPAIR_BUSY'
    if message.get('success'):
        reloaded=(message.get('data') or {}).get('reloaded') is True
        text=('已执行原项目 F5，等待页面及积分就绪' if reloaded else
            '原项目消息通道已回应；等待网页控件及积分就绪，尚未执行 F5')
        current.update(state='waiting',message=text,last_error=client.get('ready_note') or text)
    else:
        text=str(message.get('error') or '扩展未返回页面恢复执行结果')[:500]
        current.update(state='waiting' if busy else 'failed',message=text,last_error=text)
        if busy:
            if managed:current['count']=request['connection_repair_previous']
            if 'f5_previous_count' in request:
                client['f5_recovery']={**client.get('f5_recovery',{}),'count':request['f5_previous_count']}
            current['next_at']=time.time()+timing_value(service.preferences,'connection_repair_wait_seconds')
    client['connection_recovery']=current
    client['runtime_note']=current['message']
    if managed:service.connection_recoveries[client.get('profile')]=current


def watch_connections(service):
    changed = False
    for client in list(service.clients.values()):
        if not client.get('authorized'):
            continue
        if client.get('page_ready') and client.get('credits_local'):
            service.connection_recoveries.pop(client.get('profile'), None)
            if client.pop('connection_recovery', None): changed = True
            continue
        if (client.get('page_ready') or time.time() - client.get('connected', time.time()) < timing_value(service.preferences, 'connection_repair_wait_seconds')
                or any(p['client'] == client['id'] and p['action'] == 'get_points'
                    for p in service.pending.values())):
            continue
        changed |= bool(repair_idle(service, client))
    return changed
