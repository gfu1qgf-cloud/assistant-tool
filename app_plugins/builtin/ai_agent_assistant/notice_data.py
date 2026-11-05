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
            'files': {}, 'versions': [], 'seen_versions': [], 'current': None})
        files = by_job[key]; valid, missing = 0, 0
        active_ids = files.get('@discovery', {}).get('file_ids')
        if not isinstance(active_ids, list) or any(not isinstance(item, str) for item in active_ids): active_ids = None
        active_ids = set(active_ids) if active_ids is not None else None
        for file_id, record in files.items():
            if record.get('state') != 'downloaded': continue
            try: exists = cached_file(record, Path(directory)/'result')
            except (OSError, ValueError): exists = False
            if not exists:
                if active_ids is None or file_id in active_ids: missing += 1
                continue
            checksum = str(record.get('sha256') or '')
            if not checksum: continue
            if active_ids is None or file_id in active_ids: valid += 1
            version = (key, file_id, checksum)
            group['files'][directory_key(record['path'])] = version
            if version not in seen:
                group['versions'].append(list(version)); available_versions.append((canonical, version))
            else: group['seen_versions'].append(list(version))
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
        elif status == '已下载·待回写':
            text, tone = '已收取·待回写', 'warning'
        elif status in {'收取待核对', '需核对质检', '任务类型不符', '任务编号不符', '需核对重复ID', '未找到云端记录'}:
            text, tone = ('已收取·待核对' if received and received >= total else f'收取待核对 {received}/{total or "?"}'), 'warning'
        elif status == '收取待重试':
            text, tone = '收取待重试', 'warning' if received else 'error'
        elif status in {'等待制作完成', '等待成品链接'}:
            text, tone = status, 'warning'
        elif status == '已下载' or received and received >= total and not status:
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


def unmark_versions_seen(store, versions):
    """Undo only explicitly selected, still-matching file versions; no file I/O."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for version in {tuple(value) for value in versions}:
            if len(version) != 3: raise ValueError('成品查看记录缺少精确版本。')
            row = db.execute('SELECT record FROM delivery_files WHERE job_id=? AND file_id=?', version[:2]).fetchone()
            if row:
                record = json.loads(row[0])
                if record.get('state') == 'downloaded' and record.get('sha256') == version[2]:
                    db.execute('DELETE FROM delivery_seen WHERE job_id=? AND file_id=? AND checksum=?', version)
                    # Restoring unread is not a newly downloaded video. Do not
                    # create another desktop notification on the next poll.
                    db.execute('INSERT OR IGNORE INTO delivery_announced VALUES (?,?,?)', version)


def run_notice_snapshot(payload, run):
    run.check_canceled(); store = Store(payload['root'])
    if payload.get('seen') and payload.get('unseen'): raise ValueError('不能同时标记已查看和未查看。')
    if payload.get('seen'): mark_versions_seen(store, payload['seen'])
    if payload.get('unseen'): unmark_versions_seen(store, payload['unseen'])
    run.check_canceled()
    return {**notice_snapshot(store, announce=not bool(payload.get('seen') or payload.get('unseen'))), 'ui_generation': payload.get('ui_generation', 0)}
