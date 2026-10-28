"""Worker-owned FIFO leases for short foreground UI phases; never block Qt."""
import re
import time

from .browser_options import snapshot
from .setup_support import project_identity
from .timing import request_timeout, value
from . import pack_mutex
from . import ui_lease

OPERATIONS = {'get_points', 'prepare', 'upload_images', 'submit', 'focus_window', 'repair_project', 'open_project', 'retry_download'}


def state(service):
    if not hasattr(service, 'ui_mutex'):
        service.ui_mutex = {'owner': None, 'queue': []}
    return service.ui_mutex


def client_for(service, entry):
    if entry.get('host'): return service
    return next((client for client in service.clients.values() if client.get('authorized') and
        not client.get('retired') and client.get('id') == entry['client'] and
        client.get('socket') is entry['socket']), None)


def notify(service, entry, result):
    if entry.get('host'):
        service.native_ui_result.emit({'token':entry['token'],'state':result,
            'expires_at':entry['at']+host_limit(service)-1 if result=='granted' else 0})
        return
    client = client_for(service, entry)
    if client:
        from .credit_probe import account_wait
        account_wait(service, client, entry, result)
        service.send(client, {'type': 'ui_mutex_result', 'token': entry['token'], 'state': result,
            'expires_at': ui_lease.deadline(service,entry) if result == 'granted' else 0,
            **({'renewal':True} if result=='granted' and entry.get('renewable') else {})})
        client['ui_mutex'] = {'state': 'active' if result == 'granted' else 'idle',
            'operation': entry['operation'], 'at': time.time()}


def advance(service):
    current = state(service)
    if service.closing:
        for entry in ([current['owner']] if current['owner'] else []) + current['queue']:
            notify(service, entry, 'expired')
        current.update(owner=None, queue=[])
        current.pop('burst', None)
        return
    pack_mutex.watch(service,current,snapshot(service.preferences)['ui_mutex'])
    if current['owner'] or not current['queue']:
        return
    while current['queue'] and not current['owner']:
        burst = current.get('burst')
        index = next((i for i,e in enumerate(current['queue']) if not e.get('host') and e['client']==burst['client']), None) if burst else 0
        if index is None:
            return
        entry = current['queue'].pop(index)
        if not client_for(service, entry):
            continue
        if not pack_mutex.packed_request(service,entry) and time.time()-entry['queued_at'] >= wait_limit(service,entry):
            notify(service,entry,'expired')
            continue
        if not valid_phase(service, entry):
            notify(service, entry, 'rejected')
            continue
        entry['at'] = time.time()
        if entry.get('renewable'):entry['expires_at'] = ui_lease.deadline(service,entry)
        for pending in pack_mutex.requests(service,entry):
            pending['at']=entry['at']
        current['owner'] = entry
        if not entry.get('host'):
            pack_mutex.granted(service,current,entry,client_for(service,entry))
        notify(service, entry, 'granted')
        return


def receive(service, client, message):
    token = message.get('token')
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
        return
    current = state(service)
    if service.closing:
        advance(service)
        return
    kind = message.get('type')
    matches = lambda entry: entry and not entry.get('host') and entry['token'] == token and entry['socket'] is client.get('socket')
    if kind in {'ui_mutex_release', 'ui_mutex_cancel'}:
        if matches(current['owner']):
            if current['owner'].get('renewable') and message.get('stopped') is not True:
                ui_lease.stop(service,current['owner'])
                return
            pack_mutex.released(service,current,current['owner'])
            if kind=='ui_mutex_release' and message.get('passive_wait') is True:
                pack_mutex.passive_wait(service,current,current['owner'])
            current['owner'] = None
        current['queue'] = [entry for entry in current['queue'] if not matches(entry)]
        if not any(entry['client'] == client['id'] for entry in current['queue']) and not (
                current['owner'] and current['owner']['client'] == client['id']):
            client['ui_mutex'] = {'state': 'idle'}
        advance(service)
        return
    if kind != 'ui_mutex_request':
        return
    if not snapshot(service.preferences)['ui_mutex']:
        service.send(client, {'type': 'ui_mutex_result', 'token': token, 'state': 'disabled'})
        return
    if (message.get('operation') not in OPERATIONS or 'ui_mutex_v1' not in client.get('capabilities', []) or
            not project_identity(client.get('saved_project')) or
            project_identity(message.get('url')) != project_identity(client['saved_project'])):
        service.send(client, {'type': 'ui_mutex_result', 'token': token, 'state': 'rejected'})
        return
    if matches(current['owner']):
        if current['owner'].get('renewable') and (current['owner'].get('stopping') or time.time()>=ui_lease.deadline(service,current['owner'])):
            ui_lease.stop(service,current['owner'])
            return
        notify(service, current['owner'], 'granted')
        return
    if any(matches(entry) for entry in current['queue']):
        return
    if any(entry['client'] == client['id'] for entry in current['queue']) or (
            current['owner'] and current['owner']['client'] == client['id']):
        service.send(client, {'type': 'ui_mutex_result', 'token': token, 'state': 'rejected'})
        return
    current['queue'].append({'token': token, 'client': client['id'], 'socket': client.get('socket'),
        'operation': message['operation'], 'action': 'focus_window', 'foreground_token': token,
        'renewable':ui_lease.CAPABILITY in client.get('capabilities',[]),
        'batch_id': str(message.get('batch_id') or ''),
        'at': time.time(), 'queued_at': time.time()})
    client['ui_mutex'] = {'state': 'waiting', 'operation': message['operation'], 'at': time.time()}
    if message['operation'] == 'get_points':
        client['credit_check'] = {**client.get('credit_check', {}), 'state':'waiting',
            'message':'积分检测正在排队等待UI锁，尚未开始读取'}
    advance(service)


def foreground_pending(service, client, request_id):
    owner = state(service)['owner']
    return owner if owner and not owner.get('stopping') and time.time()<ui_lease.deadline(service,owner) and owner['token'] == request_id and client_for(service, owner) is client else None


def disconnected(service, client):
    current = state(service)
    if current['owner'] and not current['owner'].get('host') and current['owner']['socket'] is client.get('socket'):
        current['owner'] = None
    current['queue'] = [entry for entry in current['queue'] if entry.get('host') or entry['socket'] is not client.get('socket')]
    if current.get('burst', {}).get('socket') is client.get('socket'):
        current.pop('burst', None)
    advance(service)


def phase_limit(service, entry=None):
    if entry and not entry.get('host'):
        action='run_tasks' if entry['operation']=='submit' else entry['operation']
        return request_timeout(service.preferences,action)+ (5 if action=='get_points' else 30)
    return max(request_timeout(service.preferences, action) for action in ('prepare', 'upload_images')) + 30


def wait_limit(service, entry):
    return max(value(service.preferences,'browser_startup_seconds'),
        0 if entry.get('host') else phase_limit(service,entry))


def cancel_operations(service, client, operations):
    """Cancel only this client's expired unpaid checks, preserving other work."""
    current=state(service)
    matches=lambda e: e and not e.get('host') and e['client']==client['id'] and e['operation'] in operations
    expired=[]
    if matches(current['owner']):
        if current['owner'].get('renewable'):
            ui_lease.stop(service,current['owner'])
        else:
            pack_mutex.released(service,current,current['owner'])
            expired.append(current['owner']);current['owner']=None
    remaining=[]
    for entry in current['queue']:
        if matches(entry):expired.append(entry)
        else:remaining.append(entry)
    current['queue']=remaining
    for entry in expired:notify(service,entry,'expired')
    advance(service)


def valid_phase(service, entry, *, active=False):
    if entry.get('host'): return not service.closing
    # Deleting a record does not interrupt an already granted page operation.
    # Keep its lease through the reply/release or the normal bounded deadline;
    # retired operations still waiting in the FIFO must never begin.
    if active and any(pending.get('retired') and pending.get('cancel_deadline',float('inf'))>time.time() and pending['client']==entry['client'] and
            (pending['action']==entry['operation'] or
             entry['operation']=='submit' and pending['action']=='run_tasks' and
             pending['attempt']==entry.get('batch_id')) for pending in service.pending.values()):
        return True
    if entry['operation'] == 'submit':
        return bool(entry.get('batch_id') and any(row['attempt'] == entry['batch_id'] and
            row['browser'] == entry['client'] and row['status'] in {'submitting', 'generating'}
            for row in service.store.rows()))
    if entry['operation'] == 'retry_download':
        return bool(entry.get('batch_id') and any(pending['client'] == entry['client'] and
            pending['action'] == 'retry_download' and pending['attempt'] == entry['batch_id'] and
            not pending.get('retired') for pending in service.pending.values()))
    return any(pending['client'] == entry['client'] and pending['action'] == entry['operation'] and
        not pending.get('retired') for pending in service.pending.values())


def watch(service):
    current = state(service)
    enabled = snapshot(service.preferences)['ui_mutex']
    now = time.time()
    pack_mutex.watch(service,current,enabled)
    # A page phase is bounded by the existing upload/prepare deadlines. Waiting
    # for another browser is separate from that browser's operation timeout.
    waiting = [];expired=[]
    for entry in current['queue']:
        if not client_for(service, entry):
            continue
        if not enabled or not valid_phase(service, entry) or (not pack_mutex.packed_request(service,entry) and now - entry['queued_at'] >= wait_limit(service,entry)):
            expired.append(entry)
        else:
            waiting.append(entry)
    current['queue'] = waiting
    owner = current['owner']
    if owner and (not client_for(service, owner) or not enabled or owner.get('cancel_deadline',float('inf'))<=now or not valid_phase(service, owner, active=True) or
                  owner.get('stopping') or now >= ui_lease.deadline(service,owner)):
        if owner.get('renewable') and client_for(service,owner):
            ui_lease.stop(service,owner,'disabled' if not enabled else 'expired')
        else:
            pack_mutex.released(service,current,owner)
            current['owner'] = None
            expired.append(owner)
    # A GUI receipt may synchronously release a host lease and advance the
    # queue. Remove expired owners/waiters before delivering those callbacks.
    for entry in expired:notify(service,entry,'disabled' if not enabled else 'expired')
    for entry in current['queue']:
        for pending in pack_mutex.requests(service,entry):
            pending['at'] = now
    waits=getattr(service,'ui_launch_waits',{})
    for token,entry in list(waits.items()):
        if service.closing or not enabled or now-entry['at']>=value(service.preferences,'browser_startup_seconds'):
            waits.pop(token,None)
            if now-entry['at']>=value(service.preferences,'browser_startup_seconds'):
                service.launching.pop(entry['profile'],None)
        elif entry['profile'] in service.launching:
            service.launching[entry['profile']]=now
    advance(service)


def host_limit(service):
    from .timing import value
    return min(30,max(5,value(service.preferences,'browser_startup_seconds')))+5


def host_receive(service, command):
    """Trusted main-process commands; browser sockets never enter this path."""
    token=command.get('token')
    if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',token): return
    current=state(service);kind=command['action']
    if not hasattr(service,'ui_launch_waits'):service.ui_launch_waits={}
    if kind=='ui_launch_wait':
        if command.get('waiting'):
            service.ui_launch_waits.setdefault(token,{'profile':command['profile'],'at':time.time()})
        else: service.ui_launch_waits.pop(token,None)
        return
    matches=lambda entry:entry and entry.get('host') and entry['token']==token
    if kind=='ui_native_release':
        if matches(current['owner']):current['owner']=None
        current['queue']=[entry for entry in current['queue'] if not matches(entry)]
        advance(service);return
    if kind!='ui_native_request':return
    if service.closing or not isinstance(getattr(service,'preferences',None),dict):
        service.native_ui_result.emit({'token':token,'state':'rejected'});return
    if not snapshot(service.preferences)['ui_mutex']:
        service.native_ui_result.emit({'token':token,'state':'disabled'});return
    if matches(current['owner']) or any(matches(entry) for entry in current['queue']):return
    current['queue'].append({'host':True,'token':token,'profile':command['profile'],'client':'@host',
        'socket':None,'operation':'browser_launch','at':time.time(),'queued_at':time.time()})
    advance(service)


def description(client):
    info = client.get('ui_mutex') or {}
    if info.get('state') == 'waiting':
        return '前台 UI 互斥：等待其他浏览器完成当前页面操作，生成与下载继续并行'
    if info.get('state') == 'active':
        return '前台 UI 互斥：正在操作此窗口，操作结束立即交给下一账号'
    if info.get('state') == 'stopping':
        return '前台 UI 互斥：锁已到期，正在停止旧操作；确认停止或断开连接后释放'
    return ''
