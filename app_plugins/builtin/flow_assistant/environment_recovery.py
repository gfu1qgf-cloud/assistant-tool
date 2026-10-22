"""Bounded F5 recovery of typed, unpaid preparation failures only."""
import time
import re

from .connection_status import browser_keys
from .connection_recovery import occupied
from .account_policy import account_hold
from .credit_cooldown import credit_hold
from .setup_support import project_identity
from .timing import value, delay

CODES = {'FLOW_SETTINGS_MISMATCH', 'FLOW_PAGE_NOT_READY'}


def legacy_code(message):
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
                credit_hold(client, service.preferences) or client.get('native_busy') or occupied(service, client) or
                any(p['client'] == client['id'] for p in service.pending.values())): continue
        project = project_identity(service.preferences.get('profile_projects', {}).get(client.get('profile')))
        if not project or project != project_identity(client.get('saved_project')): continue
        entries = dict(service.preferences.get('environment_recoveries', {}))
        keys = browser_keys(client)
        key = next((key for key in sorted(keys) if entries.get(key, {}).get('code') in CODES), None)
        if not key:
            # Upgrade only audited pre-submit faults with a matching durable
            # unpaid row; arbitrary/manual browser blocks are never guessed.
            blocks = service.preferences.get('browser_blocks', {})
            key = next((key for key in sorted(keys) if legacy_code(blocks.get(key, ''))), None)
            if not key: continue
            from .store import submission_risk
            if not any(row['status'] in {'queued','failed'} and not row['attempt'] and
                    row['remote'].get('environment_error') and not submission_risk(row) and
                    browser_keys({'profile':row['remote'].get('profile'), 'id':row['remote'].get('environment_browser')}) & keys
                    for row in service.store.rows()): continue
            service.commit_preferences(schedule(service.preferences,client,blocks[key],legacy_code(blocks[key])))
            entries = dict(service.preferences['environment_recoveries'])
            changed = True
        if not key: continue
        entry = entries[key]
        blocks = dict(service.preferences.get('browser_blocks', {}))
        if (entry.get('state') in {'stopped', 'ready'} or blocks.get(key) != entry.get('error') or
                any(blocks.get(other) and blocks[other] != entry['error'] for other in keys) or
                client.get('blocked') and client['blocked'] != entry['error']): continue
        now = time.time()
        if (entry.get('state') == 'refreshing' and client.get('page_ready') and
                client.get('batch_running') is False and client.get('page_instance') and
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
        if now < entry.get('next_at', 0): continue
        count = entry.get('count', 0)
        if count >= value(service.preferences, 'connection_repair_attempts'):
            entries[key] = {**entry, 'state': 'stopped', 'next_at': 0}
        else:
            from .runtime_controls import refresh
            if refresh(service, client, automatic=True):
                entries[key] = {**entry, 'state': 'refreshing', 'count': count + 1,
                    'page_instance': client.get('page_instance', ''),
                    'next_at': now + max(value(service.preferences, 'request_timeout_seconds'),
                        delay(service.preferences, 'connection_repair_delays', count))}
            elif client.get('f5_recovery', {}).get('count', 0) >= value(service.preferences, 'connection_repair_attempts'):
                entries[key] = {**entry, 'state': 'stopped', 'next_at': 0}
            else: continue
        service.commit_preferences({**service.preferences, 'environment_recoveries': entries})
        changed = True
    return changed
