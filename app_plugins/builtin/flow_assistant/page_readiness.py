"""Bounded pre-submit readiness recovery; never retry paid generation here."""
import re
import time

from .connection_status import browser_keys
from .setup_support import EXTENSION_VERSION, project_identity, compatible_extension
from .account_policy import account_hold
from .credit_cooldown import credit_hold
from .routing import lane_key
from .timing import value as timing_value, delay


def missing_window(error):
    return bool(re.fullmatch(r'(?:error:\s*)?no current window\.?', str(error).strip(), re.I))


def note_readiness(client):
    if client.get('page_ready'):
        client.pop('page_unready_at', None)
    else:
        client.setdefault('page_unready_at', time.time())


def defer_prepare(service, client, row, error, code=''):
    if code == 'FLOW_BATCH_BUSY':
        client['batch_running'] = True
        service.store.update(row['id'], 'queued', attempt='', browser='', remote={**row['remote'],
            'batch_wait': True}, error='上一批网页队列正在收尾，空闲后自动继续；尚未上传或提交本条生成。')
        service.request(client, 'snapshot')
        return True
    if not missing_window(error) and code not in {'FLOW_PAGE_NOT_READY', 'FLOW_SETTINGS_NOT_READY'}:
        return False
    previous = client.get('prepare_wait') or row['remote'].get('prepare_retry', {})
    if previous.get('browser') != client['id']:
        previous = {}
    count = int(previous.get('count', 0)) + 1
    limit = timing_value(service.preferences, 'prepare_attempts')
    if count > limit:
        service.quarantine(client, row, f'页面或浏览器窗口连续未就绪，自动等待已达{limit}次；未提交生成。')
        return True
    recovery = {'count': count, 'next_at': time.time() + delay(service.preferences, 'prepare_retry_delays', count-1),
        'profile': client.get('profile', ''), 'browser': client['id'], 'reopen': missing_window(error)}
    # A fresh snapshot may already show controls again. Keep the backoff on
    # this browser so the next queue row cannot bypass it or reset its counter.
    client['prepare_wait'] = recovery
    remote = {**row['remote'], 'prepare_retry': recovery}
    service.store.update(row['id'], 'queued', attempt='', browser='', remote=remote,
        error=f'正在等待页面控件／窗口就绪（自动重试{count}/{limit}）；尚未提交生成')
    client['page_ready'] = False
    client['page_error'] = '页面或窗口尚未就绪，程序正在自动等待，不需要手动重新配对'
    service.request(client, 'snapshot')
    return True


def record_duration_limit(service, client, row, error, data):
    duration = row['payload'].get('duration')
    options = data.get('available_durations')
    if (type(duration) is not int or data.get('duration') != duration or not isinstance(options, list)
            or not options or any(type(item) is not int or not 1 <= item <= 60 for item in options)
            or duration in options):
        return False
    scope, key = lane_key(client)
    key = scope + ':' + key
    fields = ('model', 'duration', 'aspect', 'resolution')
    signature = {field: row['payload'].get(field) for field in fields}
    blocks = dict(service.preferences.get('parameter_blocks', {}))
    entries = [entry for entry in blocks.get(key, [])
               if any(entry.get(field) != signature[field] for field in fields)]
    entries.append({**signature, 'reason': str(error)[:500], 'available_durations': sorted(set(options)),
                    'at': time.time()})
    blocks[key] = entries[-16:]
    service.commit_preferences({**service.preferences, 'parameter_blocks': blocks})
    client.pop('prepare_wait', None)
    remote = {key: value for key, value in row['remote'].items() if key != 'prepare_retry'}
    service.store.update(row['id'], 'queued', attempt='', browser='', remote=remote,
        error=f'此浏览器的 {duration} 秒参数不可用，等待其他可用账号；此浏览器仍可执行其他时长。')
    return True


def recoverable_prepare_reason(text, version):
    if version not in {'1.1.16', '1.1.17', '1.1.18', '1.1.19', '1.1.20', '1.1.21', '1.1.22', '1.1.23', '1.1.24', '1.1.25', '1.1.26', '1.1.27'}: return False
    legacy = {'无法确认页面已选中 Omni 1.1 Flash、比例、分辨率和 x1，未提交生成。',
              '浏览器响应超时，请检查页面后恢复；未发送新的生成请求。'}
    return (text in legacy or bool(re.fullmatch(r'页面或浏览器窗口连续未就绪，自动等待已达\d+次；未提交生成。', str(text))) or
        version not in {'1.1.16','1.1.17'} and text in {
            f'找不到视频时长选项，无法确认 {duration} 秒；请检查当前模型的设置面板' for duration in (4,6,8,10)})


def recover_preparation_block(service, client):
    """Re-enter preflight once for two audited, pre-submit legacy failures.

    An orphan block can outlive its queue row. Require a current bridge and a
    fresh idle snapshot of the exact configured project, then verify again;
    clearing a block is never proof that the requested settings are selected.
    """
    if (not client.get('authorized') or not client.get('page_ready') or client.get('batch_running') is not False
            or client.get('extension_version') not in {'1.1.16', '1.1.17', '1.1.18', '1.1.19', '1.1.20', '1.1.21', '1.1.22', '1.1.23', '1.1.24', '1.1.25', '1.1.26', '1.1.27'} or client.get('setup_blocked')
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
    blocks = service.preferences.get('browser_blocks', {})
    keys = {key for key in browser_keys(client) if recoverable_prepare_reason(blocks.get(key), client.get('extension_version'))}
    if not keys:
        return False
    history = list(service.preferences.get('preparation_block_history', []))
    history.extend({'key': key, 'error': blocks[key], 'at': time.time()} for key in sorted(keys))
    service.commit_preferences({**service.preferences,
        'browser_blocks': {key: value for key, value in blocks.items() if key not in keys},
        'preparation_block_history': history[-32:]})
    client['preparation_block_recovered'] = True
    client.pop('prepare_wait', None)
    if client.get('blocked') in {blocks[key] for key in keys}:
        client['blocked'] = ''
    for row in rows:
        remote = row['remote']
        identities = browser_keys({'profile': remote.get('profile'), 'id': remote.get('environment_browser')})
        if (row['status'] not in {'queued', 'failed'} or row['attempt'] or not identities & keys
                or not remote.get('environment_error') or not any(message in row['error'] for message in (blocks[key] for key in keys))
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
        if service.safety_paused or service.paused and row['id'] not in service.released:
            continue
        profile = recovery.get('profile')
        if not profile or credit_hold({'profile': profile}, service.preferences):
            continue
        recovery = {**recovery, 'reopen': False}
        service.store.update(row['id'], 'queued', remote={**row['remote'], 'prepare_retry': recovery})
        ready = any(c.get('authorized') and c.get('profile') == profile and c.get('page_ready')
                    for c in service.clients.values())
        if not ready:
            from .browser_slots import request_start
            request_start(service, profile)
        changed = True
    return changed


def retry_initial_credits(service, client):
    page_available = client.get('page_loaded') if client.get('credits_waiting_account') else client.get('page_ready')
    if (not client.get('authorized') or not page_available or account_hold(client, service.preferences)
            or credit_hold(client, service.preferences) and not client.get('credits_retry_manual')
            or not client.get('credits_waiting_ready') or client.get('credits_ready_failures', 0) >= timing_value(service.preferences, 'credit_read_attempts')
            or client.get('credits_retry_at', 0) > time.time()
            or any(r['client'] == client['id'] and r['action'] == 'get_points' for r in service.pending.values())):
        return False
    client['credits_waiting_ready'] = False
    return bool(service.request(client, 'get_points', manual_credit=bool(client.get('credits_retry_manual'))))


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
