"""Bounded F5 recovery of typed, unpaid preparation failures only."""
import time
import re

from .connection_status import browser_keys
from .connection_recovery import occupied
from .account_policy import account_hold
from .credit_cooldown import credit_hold
from .setup_support import project_identity
from .timing import value, delay
from .problem_details import temporary_code, manual_action

CODES = {'FLOW_SETTINGS_MISMATCH', 'FLOW_PAGE_NOT_READY'}


def legacy_code(message):
    if temporary_code(message): return temporary_code(message)
    if (message == '页面未确认 Omni 参数，未提交生成。' or
            re.fullmatch(r'无法确认页面已选中的[^；\n]+；实际选中参数不一致，请检查 Flow 设置后恢复该浏览器。未提交生成。', str(message))):
        return 'FLOW_SETTINGS_MISMATCH'
    return ''


def schedule(preferences, client, message, code):
    if code not in CODES: return preferences
    key = 'profile:' + client['profile'] if client.get('profile') else 'browser:' + client['id']
    entries = dict(preferences.get('environment_recoveries', {}))
    previous = entries.get(key, {})
    count = previous.get('count', 0)
    entries[key] = {**previous, 'code': code, 'error': str(message)[:1000],
        'page_instance': previous.get('page_instance') or client.get('page_instance', ''),
        'state': 'waiting' if count < value(preferences, 'connection_repair_attempts') else 'stopped',
        'next_at': time.time() + delay(preferences, 'connection_repair_delays', count)}
    return {**preferences, 'environment_recoveries': entries}


def watch(service):
    if service.safety_paused: return False
    if not any(r['status'] == 'queued' and (not service.paused or r['id'] in service.released)
               for r in service.store.rows()): return False
    changed = False
    for client in list(service.clients.values()):
        if (not client.get('authorized') or account_hold(client, service.preferences) or
                credit_hold(client, service.preferences)): continue
        project = project_identity(service.preferences.get('profile_projects', {}).get(client.get('profile')))
        if not project or project != project_identity(client.get('saved_project')): continue
        entries = dict(service.preferences.get('environment_recoveries', {}))
        keys = browser_keys(client)
        key = next((key for key in sorted(keys) if entries.get(key, {}).get('code') in CODES), None)
        if not key:
            # Exact legacy transport faults may outlive their old queue row.
            # Other audited settings faults still require an unpaid row.
            blocks = service.preferences.get('browser_blocks', {})
            key = next((key for key in sorted(keys) if legacy_code(blocks.get(key, ''))), None)
            if not key: continue
            from .store import submission_risk
            if not temporary_code(blocks[key]) and not any(row['status'] in {'queued','failed'} and not row['attempt'] and
                    row['remote'].get('environment_error') and not submission_risk(row) and
                    browser_keys({'profile':row['remote'].get('profile'), 'id':row['remote'].get('environment_browser')}) & keys
                    for row in service.store.rows()): continue
            if occupied(service, client): continue
            service.commit_preferences(schedule(service.preferences,client,blocks[key],legacy_code(blocks[key])))
            entries = dict(service.preferences['environment_recoveries'])
            changed = True
        if not key: continue
        entry = entries[key]
        blocks = dict(service.preferences.get('browser_blocks', {}))
        if (entry.get('state') == 'ready' or blocks.get(key) != entry.get('error') or
                any(blocks.get(other) and blocks[other] != entry['error'] for other in keys) or
                client.get('blocked') and client['blocked'] != entry['error']): continue
        now = time.time()
        # Live paid work is protected. Pending read-only operations or a stale
        # native/batch flag get a finite wait instead of an endless "0/3".
        related = [row for row in service.store.rows() if row['browser'] == client['id'] or
            row['remote'].get('profile') == client.get('profile') or client.get('account') and
            row['remote'].get('account') == client['account']]
        from .store import TRANSIENT
        live_work = (any(row['status'] in TRANSIENT | {'archiving','uncertain','download_failed'} for row in related)
                     and occupied(service, client))
        if live_work:
            client['environment_wait'] = '原任务仍在生成、传输或核对；结束后自动恢复，期间不刷新'
            continue
        if (entry.get('state') in {'refreshing','stopped'} and client.get('page_ready') and
                client.get('batch_running') is False and client.get('page_instance') and
                entry.get('page_instance') and
                client['page_instance'] != entry.get('page_instance') and
                project_identity(client.get('project')) == project):
            entries[key] = {**entry, 'state': 'ready', 'next_at': 0}
            service.commit_preferences({**service.preferences, 'environment_recoveries': entries,
                'browser_blocks': {k:v for k,v in blocks.items() if k not in keys or v != entry['error']}})
            client.pop('prepare_wait', None)
            if client.get('blocked') == entry['error']: client['blocked'] = ''
            client['runtime_note'] = '原页面已自动刷新并恢复，下一条仍完整核对参数后才提交。'
            for row in service.store.rows():
                remote = row['remote']
                if (row['status'] not in {'queued','failed'} or row['attempt'] or not remote.get('environment_error') or
                        not browser_keys({'profile':remote.get('profile'), 'id':remote.get('environment_browser')}) & keys):
                    continue
                from .store import submission_risk
                if submission_risk(row): continue
                remote = {k:v for k,v in remote.items() if k not in {'environment_error','environment_browser','prepare_retry'}}
                service.store.update(row['id'], 'queued', remote=remote, error='原浏览器已自动恢复，等待重新核对参数。')
            changed = True
            continue
        if entry.get('state') == 'stopped':
            condition_cleared = (
                entry.get('wait_on') == 'precondition' and not client.get('batch_running') and
                not client.get('native_busy') and client.get('reset_wait_until', 0) <= now and
                not any(p['client'] == client['id'] for p in service.pending.values()) or
                entry.get('wait_on') == 'capability' and 'runtime_controls_v1' in client.get('capabilities', []))
            if not condition_cleared or entry.get('count', 0) >= value(service.preferences, 'connection_repair_attempts'): continue
            entry = {**entry, 'state': 'waiting', 'next_at': 0, 'wait_started_at': now}
        if now < entry.get('next_at', 0): continue
        count = entry.get('count', 0)
        pending = [p for p in service.pending.values() if p['client'] == client['id']]
        wait_reason = ('原网页仍报告队列未结束' if client.get('batch_running') else
            '等待浏览器原生操作结束' if client.get('native_busy') else
            '等待当前浏览器请求返回' if pending else
            '等待旧操作保护期结束' if client.get('reset_wait_until', 0) > now else '')
        if wait_reason:
            since = entry.get('wait_started_at', now)
            timeout = max(value(service.preferences, 'browser_startup_seconds'),
                          value(service.preferences, 'request_timeout_seconds'))
            update = {**entry, 'wait_started_at': since, 'wait_reason': wait_reason, 'wait_on': 'precondition'}
            if now - since >= timeout:
                update.update(state='stopped', next_at=0,
                    stop_reason=f'{wait_reason}持续超过 {timeout:g} 秒，未刷新在途操作',
                    manual_action='检查原项目是否还有未结束的生成、上传或下载；若网页已空闲，刷新原页并恢复此账号')
            if update != entry:
                entries[key] = update
                service.commit_preferences({**service.preferences, 'environment_recoveries': entries})
                changed = True
            continue
        client.pop('environment_wait', None)
        if count >= value(service.preferences, 'connection_repair_attempts'):
            entries[key] = {**entry, 'state': 'stopped', 'next_at': 0,
                'stop_reason': '原项目刷新后仍未恢复就绪', 'manual_action': manual_action(entry['error'])}
        else:
            from .runtime_controls import refresh
            if refresh(service, client, automatic=True):
                entries[key] = {**entry, 'state': 'refreshing', 'count': count + 1,
                    'page_instance': client.get('page_instance', ''),
                    'wait_reason': '', 'wait_on': '', 'wait_started_at': now,
                    'next_at': now + max(value(service.preferences, 'request_timeout_seconds'),
                        delay(service.preferences, 'connection_repair_delays', count))}
            elif client.get('f5_recovery', {}).get('count', 0) >= value(service.preferences, 'connection_repair_attempts'):
                entries[key] = {**entry, 'state': 'stopped', 'next_at': 0,
                    'stop_reason': '此浏览器的自动刷新次数已达上限', 'manual_action': manual_action(entry['error'])}
            elif 'runtime_controls_v1' not in client.get('capabilities', []):
                note = client.get('runtime_note') or '当前扩展尚未加载运行时 F5 支持'
                entries[key] = {**entry, 'state': 'stopped', 'next_at': 0,
                    'stop_reason': note, 'manual_action': manual_action(note), 'wait_on': 'capability'}
            elif client.get('f5_recovery', {}).get('next_at', 0) > now:
                continue
            else:
                since = entry.get('wait_started_at', now)
                note = client.get('runtime_note') or '刷新指令尚未发送成功，等待连接恢复'
                entries[key] = {**entry, 'wait_started_at': since, 'wait_reason': note}
                if now - since >= max(value(service.preferences, 'browser_startup_seconds'),
                                      value(service.preferences, 'request_timeout_seconds')):
                    entries[key].update(state='stopped', next_at=0,
                        stop_reason=note, manual_action=manual_action(note))
                if entries[key] == entry: continue
        service.commit_preferences({**service.preferences, 'environment_recoveries': entries})
        changed = True
    return changed
