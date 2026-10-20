"""Bounded pre-submit readiness recovery; never retry paid generation here."""
import re
import time

from .connection_status import browser_keys
from .setup_support import EXTENSION_VERSION, project_identity, compatible_extension
from .account_policy import account_hold


def missing_window(error):
    return bool(re.fullmatch(r'(?:error:\s*)?no current window\.?', str(error).strip(), re.I))


def defer_prepare(service, client, row, error, code=''):
    if code == 'FLOW_BATCH_BUSY':
        client['batch_running'] = True
        service.store.update(row['id'], 'queued', attempt='', browser='', remote={**row['remote'],
            'batch_wait': True}, error='上一批网页队列正在收尾，空闲后自动继续；尚未上传或提交本条生成。')
        service.request(client, 'snapshot')
        return True
    if not missing_window(error) and code not in {'FLOW_PAGE_NOT_READY', 'FLOW_SETTINGS_NOT_READY'}:
        return False
    previous = row['remote'].get('prepare_retry', {})
    count = int(previous.get('count', 0)) + 1
    if count > 3:
        service.quarantine(client, row, '页面或浏览器窗口连续未就绪，自动等待已达3次；未提交生成。')
        return True
    recovery = {'count': count, 'next_at': time.time() + (2, 5, 15)[count-1],
        'profile': client.get('profile', ''), 'browser': client['id'], 'reopen': missing_window(error)}
    remote = {**row['remote'], 'prepare_retry': recovery}
    service.store.update(row['id'], 'queued', attempt='', browser='', remote=remote,
        error=f'正在等待页面控件／窗口就绪（自动重试{count}/3）；尚未提交生成')
    client['page_ready'] = False
    client['page_error'] = '页面或窗口尚未就绪，程序正在自动等待，不需要手动重新配对'
    service.request(client, 'snapshot')
    return True


def recover_preparation_block(service, client):
    """Re-enter preflight once for two audited, pre-submit legacy failures.

    An orphan block can outlive its queue row. Require a current bridge and a
    fresh idle snapshot of the exact configured project, then verify again;
    clearing a block is never proof that the requested settings are selected.
    """
    if (not client.get('authorized') or not client.get('page_ready') or client.get('batch_running') is not False
            or client.get('extension_version') != EXTENSION_VERSION or client.get('setup_blocked')
            or client.get('preparation_block_recovered')):
        return False
    expected = project_identity(service.preferences.get('profile_projects', {}).get(client.get('profile')))
    if (not expected or project_identity(client.get('saved_project')) != expected
            or project_identity(client.get('project')) != expected):
        return False
    rows = service.store.rows()
    if any(row['status'] in {'preparing', 'uploading', 'submitting', 'generating', 'downloading',
                            'uncertain', 'download_failed'} and
           (row['browser'] == client['id'] or client.get('account') and
            row['remote'].get('account') == client['account']) for row in rows):
        return False
    legacy = {'无法确认页面已选中 Omni 1.1 Flash、比例、分辨率和 x1，未提交生成。',
              '浏览器响应超时，请检查页面后恢复；未发送新的生成请求。'}
    blocks = service.preferences.get('browser_blocks', {})
    keys = {key for key in browser_keys(client) if blocks.get(key) in legacy}
    if not keys:
        return False
    history = list(service.preferences.get('preparation_block_history', []))
    history.extend({'key': key, 'error': blocks[key], 'at': time.time()} for key in sorted(keys))
    service.commit_preferences({**service.preferences,
        'browser_blocks': {key: value for key, value in blocks.items() if key not in keys},
        'preparation_block_history': history[-32:]})
    client['preparation_block_recovered'] = True
    for row in rows:
        remote = row['remote']
        identities = browser_keys({'profile': remote.get('profile'), 'id': remote.get('environment_browser')})
        if (row['status'] not in {'queued', 'failed'} or row['attempt'] or not identities & keys
                or not remote.get('environment_error') or not any(message in row['error'] for message in legacy)
                or any(remote.get(key) for key in ('bindings', 'upstream_task', 'generation_response',
                    'download', 'unverified_download', 'page_recovery', 'cloud_finished'))):
            continue
        remote = {key: value for key, value in remote.items() if key not in {'environment_error', 'environment_browser'}}
        service.store.update(row['id'], 'queued', remote=remote,
            error='旧准备失败标记已转为重新核对参数；尚未提交生成。')
    return True


def watch_preparation(service):
    changed = False
    for row in service.store.rows():
        recovery = row['remote'].get('prepare_retry', {})
        if row['status'] != 'queued' or not recovery.get('reopen') or recovery.get('next_at', 0) > time.time():
            continue
        profile = recovery.get('profile')
        if not profile:
            continue
        recovery = {**recovery, 'reopen': False}
        service.store.update(row['id'], 'queued', remote={**row['remote'], 'prepare_retry': recovery})
        ready = any(c.get('authorized') and c.get('profile') == profile and c.get('page_ready')
                    for c in service.clients.values())
        if not ready:
            service.launch_requested.emit(profile)
        changed = True
    return changed


def retry_initial_credits(service, client):
    page_available = client.get('page_loaded') if client.get('credits_waiting_account') else client.get('page_ready')
    if (not client.get('authorized') or not page_available or account_hold(client, service.preferences)
            or not client.get('credits_waiting_ready') or client.get('credits_ready_failures', 0) >= 3
            or client.get('credits_retry_at', 0) > time.time()
            or any(r['client'] == client['id'] and r['action'] == 'get_points' for r in service.pending.values())):
        return False
    client['credits_waiting_ready'] = False
    service.request(client, 'get_points')
    return True


def recover_window_block(service, client):
    """Retire only an old pre-submit No-current-window block after real readiness."""
    if (not client.get('authorized') or not client.get('page_ready')
            or not compatible_extension(client.get('extension_version')) or client.get('setup_blocked')):
        return False
    expected = project_identity(service.preferences.get('profile_projects', {}).get(client.get('profile')))
    if not expected or project_identity(client.get('saved_project')) != expected or project_identity(client.get('project')) != expected:
        return False
    blocks = service.preferences.get('browser_blocks', {})
    keys = {key for key in browser_keys(client) if missing_window(blocks.get(key, ''))}
    if not keys:
        return False
    preferences = {**service.preferences, 'browser_blocks': {key: value for key, value in blocks.items() if key not in keys}}
    service.store.save_settings(preferences)
    service.preferences = preferences
    for row in service.store.rows():
        remote = row['remote']
        identities = browser_keys({'profile': remote.get('profile'), 'id': remote.get('environment_browser')})
        if (row['status'] not in {'queued', 'failed'} or not remote.get('environment_error')
                or not identities & keys or 'no current window' not in row['error'].casefold()
                or row['attempt'] or remote.get('bindings') or remote.get('upstream_task')):
            continue
        remote = {key: value for key, value in remote.items() if key not in {'environment_error', 'environment_browser'}}
        remote['window_block_recovered'] = True
        service.store.update(row['id'], 'queued', remote=remote, error='浏览器窗口已就绪，自动重新核对参数；未重新生成')
    return True
