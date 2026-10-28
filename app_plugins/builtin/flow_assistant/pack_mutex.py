"""Cooperative UI time slices preserve pack identity and active page phases."""
import time

from .credit_packs import ui_burst
from .timing import value


def requests(service, entry):
    action = 'run_tasks' if entry.get('operation') == 'submit' else entry.get('operation')
    return [p for p in service.pending.values() if p['client'] == entry['client'] and p['action'] == action and
            not p.get('retired') and (action not in {'run_tasks', 'retry_download'} or p['attempt'] == entry.get('batch_id'))]


def packed_request(service, entry):
    if entry.get('host') or entry.get('operation') == 'retry_download':
        return False
    for pending in requests(service, entry):
        row = service.store.get(pending.get('job')) if pending.get('job') else None
        if (row and row['attempt'] == pending['attempt'] and row['browser'] == pending['client'] and
                row['remote'].get('credit_pack')):
            return True
    return False


def waiting_submission(service, row):
    state = getattr(service, 'ui_mutex', {})
    entries = list(state.get('queue', []))
    if state.get('owner'):
        entries.append(state['owner'])
    return any(e.get('operation') == 'submit' and e['client'] == row['browser'] and e.get('batch_id') == row['attempt']
               for e in entries)


def settle(burst, now):
    if burst.get('clock_at') is not None:
        burst['elapsed'] += max(0, now - burst['clock_at'])
        burst['clock_at'] = now


def granted(service, current, entry, client):
    if entry.get('host'):
        return
    pack = ui_burst(service, client)
    if not pack or entry['operation'] not in {'prepare', 'upload_images', 'submit'}:
        return
    now = time.time()
    burst = current.get('burst')
    if not burst:
        burst = current['burst'] = {'id':pack, 'client':entry['client'], 'socket':entry['socket'],
            'elapsed':0, 'clock_at':None, 'started_at':None, 'last_phase_at':now}
    if burst['id'] != pack or burst['client'] != entry['client']:
        return
    settle(burst, now)
    if entry['operation'] == 'submit':
        if burst['started_at'] is None:
            burst['started_at'] = now
        burst['clock_at'] = now
        # Waiting for a foreground permit is not waiting for a website receipt.
        for pending in requests(service, entry):
            row = service.store.get(pending.get('job')) if pending.get('job') else None
            if row and row['remote'].get('credit_pack', {}).get('id') == pack:
                service.store.update(row['id'], row['status'],
                    remote={**row['remote'], 'pack_sent_at':now}, error=row['error'])
    else:
        burst['clock_at'] = None  # Preparation/upload never spend the submit slice.
    burst['last_phase_at'] = now
    client['pack_ui'] = {'id':pack,'state':'active'}


def released(service, current, entry):
    burst = current.get('burst')
    if not burst or entry.get('host') or burst['client'] != entry['client']:
        return
    now = time.time()
    settle(burst, now)
    burst['clock_at'] = now if burst['started_at'] is not None else None
    burst['last_phase_at'] = now
    if entry['operation'] == 'submit':
        for pending in requests(service, entry):
            row = service.store.get(pending.get('job')) if pending.get('job') else None
            if row and row['remote'].get('credit_pack', {}).get('id') == burst['id']:
                service.store.update(row['id'], row['status'],
                    remote={**row['remote'], 'pack_sent_at':now}, error=row['error'])


def passive_wait(service,current,entry):
    """Background observation yields priority, never pack members or receipts."""
    burst=current.get('burst')
    if (entry.get('host') or entry.get('operation') not in {'prepare','upload_images','submit'} or
            not burst or burst['client']!=entry['client']):return
    current.pop('burst',None)
    client=next((c for c in service.clients.values() if c.get('id')==entry['client'] and
        c.get('socket') is entry['socket']),None)
    if client:client['pack_ui']={'id':burst['id'],'state':'waiting',
        'message':'后台等待上传／页面／提交回执，已让出UI；整包保持原账号、图片和顺序，交互前重新申请锁'}


def watch(service, current, enabled):
    burst = current.get('burst')
    if not burst:
        return
    now = time.time()
    client = next((c for c in service.clients.values() if c.get('authorized') and c['id'] == burst['client'] and
                   c.get('socket') is burst['socket']), None)
    if not enabled or not client or ui_burst(service, client) != burst['id']:
        current.pop('burst', None)
        if client:
            client.pop('pack_ui', None)
        return
    if not current.get('owner') and client.get('page_ready') is False:
        # run_tasks has not entered its submit lease while initial controls
        # are loading. That read-only wait must not reserve the desktop.
        passive_wait(service,current,{'client':burst['client'],'socket':burst['socket'],'operation':'prepare'})
        return
    settle(burst, now)
    pending = any(p['client'] == burst['client'] and p.get('job') and not p.get('retired') and
                  p['action'] in {'prepare','upload_images','run_tasks'} for p in service.pending.values())
    grace = max(10, value(service.preferences, 'pack_submit_interval_seconds') +
                value(service.preferences, 'pack_submit_jitter_seconds') + 5)
    due = burst['elapsed'] >= value(service.preferences, 'pack_ui_slice_seconds')
    idle = not pending and now - burst['last_phase_at'] >= grace
    unclaimed=(now-burst['last_phase_at']>=grace and not any(
        not e.get('host') and e['client']==burst['client'] for e in current.get('queue',[])))
    if (due or idle or unclaimed) and not current.get('owner'):
        current.pop('burst', None)
        client['pack_ui'] = {'id':burst['id'], 'state':'waiting',
            'message':'整包保持原账号、图片和顺序；让出本轮UI锁，排队取得下一轮锁后继续'}
