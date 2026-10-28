"""An authenticated background worker is not proof of an open project tab."""
import time
import uuid

from .account_policy import account_hold
from .connection_recovery import occupied
from .credit_cooldown import credit_hold
from .policy import admitted_queue
from .routing import dispatch_rank
from .setup_support import project_identity
from .page_readiness import missing_window

MISSING_PROJECT = '等待主程序打开本配置的 Flow 项目；扩展不会自行新建标签页。'
OPEN_CONFIRM_SECONDS = 8
OPEN_ATTEMPTS = 3


def note_missing(service, client, error, code=''):
    if str(error).strip() != MISSING_PROJECT and not missing_window(error):
        return False
    client['project_missing'] = True
    client['page_ready'] = False
    client['connection_recovery'] = {**client.get('connection_recovery', {}), 'state': 'missing_project',
        'message': '扩展后台已连接，但原配置的项目页不存在，等待恢复原项目'}
    return request_open(service, client)


def request_open(service, client):
    from .browser_reuse import stopped
    if stopped(service,client):return False
    from .high_credit import allowed
    profile = client.get('profile')
    project = service.preferences.get('profile_projects', {}).get(profile)
    if (not allowed(client, service.preferences) or not client.get('authorized') or not client.get('project_missing') or not profile
            or not project_identity(project) or project_identity(project) != project_identity(client.get('saved_project'))
            or client.get('page_ready') or service.safety_paused or client.get('native_busy')
            or account_hold(client, service.preferences) or credit_hold(client, service.preferences)
            or occupied(service, client)):
        return False
    admitted = admitted_queue(service.store.rows(), service.store.batches(), service.observed_costs,
        paused=service.paused, released=service.released)
    if not any(dispatch_rank(client, row['payload'], service.preferences) is not None for row in admitted):
        return False
    slots = dict(service.preferences.get('browser_slots', {}))
    entry = dict(slots.get(profile, {}))
    previous = entry.get('project_startup', {})
    same_session=previous.get('session') == service.project_startup_session
    count=previous.get('count',0) if same_session else 0
    if same_session and previous.get('state')=='opening':return False
    if same_session and count:
        if time.time()-previous.get('at',time.time()) < OPEN_CONFIRM_SECONDS:return False
        if count>=OPEN_ATTEMPTS:
            client['connection_recovery'] = {**client.get('connection_recovery', {}), 'state':'stopped',
                'message':f'原项目打开已核对{OPEN_ATTEMPTS}次，仍未收到原项目页面就绪；已跳过本账号，请检查页面登录或扩展状态。'}
            return False
    command = {'action': 'project_open_result', 'request_id': uuid.uuid4().hex,
        'profile': profile, 'browser': client['id'], 'project': project}
    entry['project_startup'] = {**command, 'count':count+1, 'state':'opening', 'at':time.time(),
        'session':service.project_startup_session}
    slots[profile] = entry
    service.commit_preferences({**service.preferences, 'browser_slots': slots})
    client['connection_recovery'] = {**client.get('connection_recovery', {}), 'state':'opening_project',
        'message':'项目页不存在，正在重新打开原配置保存的 Flow 项目；不另换账号'}
    service.publish()  # GUI must receive the exact lease before its launch signal.
    service.project_open_requested.emit(command)
    return True


def watch(service):
    changed = False
    for client in list(service.clients.values()):
        if client.get('project_missing'): changed |= request_open(service, client)
    return changed


def ready(service, client):
    if (not client.get('page_ready') or not project_identity(client.get('project'))
            or project_identity(client['project']) != project_identity(client.get('saved_project'))
            or project_identity(client['project']) != project_identity(
                service.preferences.get('profile_projects', {}).get(client.get('profile')))):
        return
    client.pop('project_missing', None)
    profile = client.get('profile')
    slots = dict(service.preferences.get('browser_slots', {}))
    entry = dict(slots.get(profile, {}))
    if entry.pop('project_startup', None) is not None:
        slots[profile] = entry
        service.commit_preferences({**service.preferences, 'browser_slots': slots})


def result(service, command):
    profile = command.get('profile')
    slots = dict(service.preferences.get('browser_slots', {}))
    entry = dict(slots.get(profile, {})); lease = entry.get('project_startup', {})
    if not lease or any(lease.get(key) != command.get(key) for key in ('request_id', 'browser', 'project')):
        return
    cancelled = bool(command.get('cancelled'))
    message = ('已发出原项目打开请求，等待原项目网页实际响应；尚未确认页面就绪' if command.get('opened') else
        str(command.get('error') or '原项目恢复启动未完成')[:500])
    entry['project_startup'] = {**lease, 'state':'cancelled' if cancelled else
        'waiting' if command.get('opened') else 'failed', 'at':time.time(),
        'count':max(0,lease.get('count',1)-1) if cancelled else lease.get('count',1), 'message':message}
    slots[profile] = entry
    service.commit_preferences({**service.preferences, 'browser_slots': slots})
    # This was a same-profile navigation attempt, never proof that Chrome exited.
    for client in service.clients.values():
        if client.get('id') == command.get('browser'):
            client['connection_recovery'] = {**client.get('connection_recovery', {}),
                'state':'waiting' if command.get('opened') else 'stopped', 'message':message}


def launch_allowed(snapshot, command):
    from .high_credit import allowed
    preferences = snapshot.get('preferences', {})
    lease = preferences.get('browser_slots', {}).get(command.get('profile'), {}).get('project_startup', {})
    if (snapshot.get('safety_paused') or snapshot.get('paused', True) and not snapshot.get('released')
            or not lease or lease.get('state') != 'opening'
            or any(lease.get(key) != command.get(key) for key in ('request_id','browser','project'))
            or project_identity(command.get('project')) != project_identity(
                preferences.get('profile_projects', {}).get(command.get('profile')))):
        return False
    client = next((c for c in snapshot.get('clients', []) if c.get('id') == command.get('browser')
        and c.get('profile') == command.get('profile')), None)
    if (not client or not allowed(client, preferences) or not client.get('project_missing') or client.get('page_ready')
            or (client.get('reuse_recovery') or {}).get('state')=='stopped'
            or project_identity(client.get('saved_project')) != project_identity(command.get('project'))
            or client.get('native_busy') or client.get('batch_running')
            or client.get('reset_wait_until', 0) > time.time() or account_hold(client, preferences)
            or credit_hold(client, preferences)):
        return False
    account = str(client.get('account') or '').strip().casefold()
    return not any(row.get('status') in {'preparing','uploading','submitting','generating','downloading',
        'archiving','uncertain','download_failed'} and (row.get('browser') == command.get('browser')
        or row.get('remote', {}).get('profile') == command.get('profile') or account and
        str(row.get('remote', {}).get('account') or '').strip().casefold() == account)
        for row in snapshot.get('jobs', []))
