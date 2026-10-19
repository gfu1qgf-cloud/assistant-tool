"""Read-only post-submit recovery; never reload or resubmit generation."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing

from .media_identity import generated_bindings
from .transfer_policy import ready_bindings

BINDING_MISSING = 'FLOW_MEDIA_BINDING_MISSING'


def record_response(service, client, message):
    row = next((r for r in service.store.rows() if r['attempt'] == message.get('batch_id') and r['browser'] == client['id']), None)
    if not row or row['status'] not in {'submitting', 'generating', 'uncertain', 'download_failed'}: return
    if message.get('download_path') != service.download_name(row) or not message.get('submissionId') or not message.get('taskId'): return
    if generated_bindings(row['remote']): return
    recorded = row['remote'].get('generation_response') or {}
    if recorded and not recorded.get('empty'): return
    if row['remote'].get('upstream_task') not in (None, '', message['taskId']): return
    submissions = {b.get('submissionId') for b in row['remote'].get('bindings', []) if isinstance(b, dict) and b.get('submissionId')}
    if submissions and message['submissionId'] not in submissions: return
    response = message.get('response')
    if response is not None and not isinstance(response, (dict, list, str)): return
    encoded = json.dumps(response, ensure_ascii=False, separators=(',', ':'))
    evidence = {'taskId': message['taskId'], 'submissionId': message['submissionId'],
        'sha256': hashlib.sha256(encoded.encode('utf-8')).hexdigest(), 'truncated': len(encoded) > 196608,
        'empty': response is None or response == '',
        'response': response if len(encoded) <= 196608 else encoded[:196608]}
    cache = getattr(service, '_generation_responses', None)
    if cache is None: cache = service._generation_responses = {}
    if recorded.get('sha256') == evidence['sha256']: return
    previous = cache.get(row['attempt'])
    if previous and not previous.get('empty'): return
    cache[row['attempt']] = evidence
    while len(cache) > 16: cache.pop(next(iter(cache)))
    # The receipt can arrive after binding_missing or after the three snapshot
    # probes. Persist it then as well; never leave it stranded only in RAM.
    if row['remote'].get('page_recovery'):
        metadata = persist_response(service, row)
        if metadata:
            error = row['error']
            if '未取得原生成响应' in error:
                error = '已收到原生成响应，尚待确认其中的媒体编号；保留原任务，不刷新或重新生成。'
            service.store.update(row['id'], row['status'], remote={**row['remote'], **metadata}, error=error)


def persist_response(service, row):
    evidence = getattr(service, '_generation_responses', {}).pop(row['attempt'], None)
    if not evidence: return {}
    # Only failed binding responses are retained. Keep them outside the hot
    # queue rows, and bound total storage; never fill the encrypted volume.
    try:
        with closing(service.namespace.file('generation-diagnostics.sqlite3').connect()) as db, db:
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('CREATE TABLE IF NOT EXISTS responses (attempt TEXT PRIMARY KEY, evidence TEXT NOT NULL)')
            exists = db.execute('SELECT 1 FROM responses WHERE attempt=?', (row['attempt'],)).fetchone()
            if not exists and db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] >= 50:
                return {'diagnostic_limit': True}
            db.execute('INSERT INTO responses VALUES (?,?) ON CONFLICT(attempt) DO UPDATE SET evidence=excluded.evidence',
                (row['attempt'], json.dumps(evidence, ensure_ascii=False)))
    except (OSError, sqlite3.Error) as error:
        # A diagnostic write must not stop the generation/download state machine.
        return {'diagnostic_error': type(error).__name__}
    return {'generation_response': {k:v for k,v in evidence.items() if k != 'response'}}


def schedule(service, client, message):
    row = next((r for r in service.store.rows() if r['attempt'] == message.get('batch_id') and r['browser'] == client['id']), None)
    if not row or row['status'] not in {'submitting', 'generating', 'uncertain', 'download_failed'}: return
    if generated_bindings(row['remote']): return
    metadata = persist_response(service, row)
    if row['remote'].get('page_recovery'):
        if metadata: service.store.update(row['id'], row['status'], remote={**row['remote'], **metadata})
        return
    service.store.update(row['id'], 'uncertain', remote={**row['remote'], **metadata,
        'recovery_code': BINDING_MISSING,
        'page_recovery': {'count': 0, 'next_at': time.time()+15, 'mode': 'snapshot_only'}},
        error='尚未解析到本次媒体编号，正在等待并只读核对原任务；不会刷新页面、重复生成或下载其他视频。')


def watch(service):
    changed = False
    clients = {c.get('id'): c for c in service.clients.values() if c.get('authorized')}
    for row in service.store.rows():
        if row['status'] != 'uncertain': continue
        recovery = row['remote'].get('page_recovery')
        missing = row['remote'].get('recovery_code') == BINDING_MISSING
        # Compatibility for old durable records only; new recovery is driven
        # by the typed binding_missing event and a stable stored code.
        legacy_missing = not row['remote'].get('recovery_code') and any(
            text in row['error'] for text in ('未解析到可验证的媒体', '未解析到本次媒体'))
        if not recovery and not generated_bindings(row['remote']) and (missing or legacy_missing) and row['browser'] in clients:
            schedule(service, clients[row['browser']], {'batch_id': row['attempt']})
            changed = True
            continue
        if not recovery or recovery.get('done') or recovery.get('next_at', 0) > time.time(): continue
        client = clients.get(row['browser'])
        if not client or client.get('active_batch') not in (None, '', row['attempt']): continue
        remote = dict(row['remote']); changed = True
        if generated_bindings(remote):
            remote['page_recovery'] = {**recovery, 'done': True, 'next_at': 0}
            service.store.update(row['id'], 'download_failed' if ready_bindings(remote) else 'generating', remote=remote, error='')
            continue
        if recovery.get('count', 0) >= 3:
            remote['page_recovery'] = {**recovery, 'done': True, 'next_at': 0}
            detail = '已收到原生成响应，尚待解析出本次媒体编号。' if remote.get('generation_response') else '暂未收到可用的原生成响应，仍会接收原任务的后续结果。'
            service.store.update(row['id'], 'uncertain', remote=remote,
                error='已完成三次只读核对，保留原任务继续等待结果。'+detail+' 没有刷新页面、重复生成或下载其他视频。')
            continue
        remote['page_recovery'] = {'count': recovery.get('count', 0)+1,
            'next_at': time.time()+15, 'mode': 'snapshot_only'}
        service.store.update(row['id'], 'uncertain', remote=remote,
            error='正在只读核对原任务；保持当前页面，不刷新、不重新生成。')
        service.request(client, 'snapshot')
    return changed
