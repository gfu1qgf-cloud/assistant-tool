"""Bounded repair of an idle bridge; generation ownership always takes priority."""
import time

from .account_policy import account_hold
from .credit_cooldown import credit_hold
from .setup_support import project_identity
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
    profile = client.get('profile')
    saved = project_identity(client.get('saved_project'))
    if (not client.get('authorized') or client.get('extension_version') not in {'1.1.18', '1.1.19', '1.1.20', '1.1.21', '1.1.22', '1.1.23', '1.1.24'}
            or not profile or not saved or saved != project_identity(service.preferences.get('profile_projects', {}).get(profile))
            or account_hold(client, service.preferences) or credit_hold(client, service.preferences) or occupied(service, client)):
        return False
    if any(p['client'] == client['id'] and p['action'] == 'repair_project' for p in service.pending.values()):
        return False
    attempts = service.connection_recoveries.get(profile, {})
    limit = timing_value(service.preferences, 'connection_repair_attempts')
    if attempts.get('count', 0) >= limit:
        client['connection_recovery'] = {**attempts, 'state': 'stopped',
            'message': f'连接自动修复已达{limit}次，请检查此浏览器的登录、扩展状态或项目访问权限'}
        return False
    if attempts.get('next_at', 0) > time.time():
        return False
    count = attempts.get('count', 0) + 1
    state = {'count': count, 'next_at': time.time() + delay(service.preferences, 'connection_repair_delays', count-1), 'state': 'checking',
        'message': f'页面消息通道未就绪，正在自动修复 {count}/{limit}；只恢复当前空闲项目'}
    service.connection_recoveries[profile] = state
    client['connection_recovery'] = state
    return service.request(client, 'repair_project', {'idle': True, 'url': client['saved_project']})


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
