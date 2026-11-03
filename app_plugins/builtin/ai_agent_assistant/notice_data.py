"""Worker-side persistent unread/version summaries; no Qt objects."""
from collections import defaultdict
import json
import os
from pathlib import Path

from .store import Store
from .delivery_drive import cached_file


def directory_key(path): return os.path.normcase(os.path.abspath(str(path)))


def notice_snapshot(store, *, announce=True):
    if not store.file.exists(): return {'items': [], 'notify_files': 0, 'notify_tasks': 0}
    with store.connect() as db:
        jobs = db.execute('SELECT id,payload FROM jobs ORDER BY rowid DESC').fetchall()
        records = db.execute('SELECT job_id,file_id,record FROM delivery_files').fetchall()
        statuses = {key: (status, detail) for key, status, detail in db.execute('SELECT job_id,status,detail FROM delivery_jobs')}
        seen = set(db.execute('SELECT job_id,file_id,checksum FROM delivery_seen').fetchall())
    by_job = defaultdict(dict)
    for key, file_id, raw in records: by_job[key][file_id] = json.loads(raw)
    groups, available_versions = {}, []
    for key, raw in jobs:
        payload = json.loads(raw); directory = str(payload.get('target_dir') or '')
        if not os.path.isabs(directory): continue
        canonical = directory_key(directory)
        group = groups.setdefault(canonical, {'directory': directory, 'task_id': str(payload.get('task_id') or ''),
            'files': {}, 'versions': [], 'current': None})
        files = by_job[key]; valid, missing = 0, 0
        for file_id, record in files.items():
            if record.get('state') != 'downloaded': continue
            try: exists = cached_file(record, Path(directory)/'result')
            except (OSError, ValueError): exists = False
            if not exists: missing += 1; continue
            checksum = str(record.get('sha256') or '')
            if not checksum: continue
            valid += 1
            version = (key, file_id, checksum)
            group['files'][directory_key(record['path'])] = version
            if version not in seen:
                group['versions'].append(list(version)); available_versions.append((canonical, version))
        status, detail = statuses.get(key, ('', ''))
        expected = files.get('@discovery', {}).get('total')
        if not isinstance(expected, int) or expected < 0:
            expected = sum(record.get('state') in {'downloaded', 'retry'} for file_id, record in files.items() if file_id != '@discovery')
        meaningful = valid or missing or status and status not in {'等待制作完成', '等待成品链接'}
        if group['current'] is None and meaningful:
            group['current'] = {'status': status, 'detail': detail, 'received': valid, 'total': expected, 'missing': missing}
    items = []
    for group in groups.values():
        current = group['current']
        if not current: continue
        total_files = len(group.pop('files')); unread = len(group['versions'])
        status = current['status']; received, total = current['received'], current['total']
        if current['missing']:
            text, tone = f'成品缺失 {received}/{max(total, received + current["missing"])}', 'warning'
        elif status in {'已下载', '已下载·待回写'} or received and received >= total and not status:
            text, tone = (f'新成品 · {unread}', 'success') if unread else (f'已收取 · {total_files}', 'muted')
        elif received:
            text, tone = f'收取中 {received}/{max(total, received)}', 'warning'
        elif status in {'正在下载', '等待继续下载'}:
            text, tone = f'收取中 {received}/{total or "?"}', 'warning'
        else: text, tone = '收取需处理', 'error'
        group.update(text=text, tone=tone, unread=unread, received=total_files,
            tooltip=f'Agent成品：本地可用 {total_files} 个，未查看 {unread} 个。\n{status}\n{current["detail"]}')
        group.pop('current'); items.append(group)
    claimed, claimed_tasks = 0, set()
    if announce and available_versions:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key, version in available_versions:
                if db.execute('SELECT 1 FROM delivery_seen WHERE job_id=? AND file_id=? AND checksum=?', version).fetchone(): continue
                if db.execute('INSERT OR IGNORE INTO delivery_announced VALUES (?,?,?)', version).rowcount:
                    claimed += 1; claimed_tasks.add(key)
    return {'items': items, 'notify_files': claimed, 'notify_tasks': len(claimed_tasks)}


def mark_versions_seen(store, versions):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for version in {tuple(value) for value in versions}:
            if len(version) != 3: raise ValueError('成品查看记录缺少精确版本。')
            row = db.execute('SELECT record FROM delivery_files WHERE job_id=? AND file_id=?', version[:2]).fetchone()
            if row:
                record = json.loads(row[0])
                if record.get('state') == 'downloaded' and record.get('sha256') == version[2]:
                    db.execute('INSERT OR IGNORE INTO delivery_seen VALUES (?,?,?)', version)


def run_notice_snapshot(payload, run):
    run.check_canceled(); store = Store(payload['root'])
    if payload.get('seen'): mark_versions_seen(store, payload['seen'])
    run.check_canceled()
    return {**notice_snapshot(store, announce=not bool(payload.get('seen'))), 'ui_generation': payload.get('ui_generation', 0)}
