"""Worker-side persistent unread/version summaries; no Qt objects."""
from collections import defaultdict
import json
import os
from pathlib import Path

from .store import Store
from .delivery_drive import cached_file
from .review import review_state, review_tooltip
from .sheet_profiles import needs_user_segments,task_kind


def directory_key(path): return os.path.normcase(os.path.abspath(str(path)))


def same_receipt_scope(files):
    proof=files.get('@received', {}); discovery=files.get('@discovery', {})
    versions=proof.get('versions')
    if (not isinstance(versions,dict) or not versions or proof.get('total')!=len(versions) or proof.get('total')!=discovery.get('total') or
            not proof.get('source') or proof['source']!=discovery.get('source')): return False
    active_ids=discovery.get('file_ids')
    if isinstance(active_ids,list) and set(active_ids)!=set(versions): return False
    return True


def verified_receipt(files, valid_versions):
    return same_receipt_scope(files) and all(valid_versions.get(key)==checksum and bool(checksum)
        for key,checksum in files['@received']['versions'].items())


def same_partial_scope(files):
    partial=files.get('@partial',{}); discovery=files.get('@discovery',{}); versions=partial.get('versions')
    return bool(isinstance(versions,dict) and versions and partial.get('source')==discovery.get('source') and
        partial.get('total')==discovery.get('total') and set(versions)==set(discovery.get('file_ids') or []))


def verified_partial_receipt(files, valid_versions):
    partial=files.get('@partial',{})
    return bool(partial.get('source') and same_partial_scope(files) and
        all(valid_versions.get(key)==checksum and bool(checksum)
            for key,checksum in partial['versions'].items()))


def receipt_badge(current, *, unread, seen_versions):
    """Keep lifecycle, local receipt and explicit human review separate."""
    status=current['status']; received,total=current['received'],current['total']
    pending=status in {'正在下载','等待继续下载','下载待重试'}
    full_local=(received>0 and total>0 and received==total and not current['missing'] and current['view_scope_ok'])
    complete=full_local and current.get('total_known',True)
    partial=current.get('verified_partial',False)
    normal='success' if unread else 'muted'
    if status in {'正在下载','等待继续下载'}:
        text,tone=('补段收取中' if current.get('needs_user_segments') else '收取中'),'warning'
    elif status=='下载待重试':
        text,tone=('补段下载待重试' if current.get('needs_user_segments') else '收取待重试'),'warning' if received else 'error'
    elif current['missing']:
        text,tone='成品缺失','warning'
    elif current.get('needs_user_segments'):
        if status=='收取待重试':text,tone='补段下载待重试','warning' if received else 'error'
        elif status=='等待成品链接':text,tone='等待补段成品链接','warning'
        else:text,tone='需用户补段','warning'
    elif current.get('cloud_complete') is False:
        text,tone=('待补齐' if received and total>received else '等待制作完成'),'warning'
    elif status=='收取待重试':
        if current['verified_received'] and complete:
            text,tone='已收取',normal if current['receipt_archived'] else 'warning'
        elif partial: text,tone='待补齐','warning'
        else: text,tone='收取待重试','warning' if received else 'error'
    elif status in {'已下载','已下载·待回写'}:
        if complete:
            text,tone='已收取',normal if status=='已下载' else 'warning'
        else: text,tone='收取待核对','warning'
    elif status=='收取待核对':
        text,tone='收取待核对','warning'
    elif status in {'已收取部分','已收取·任务未完成'}:
        # Having all currently available files is NOT having every expected
        # segment. A partial receipt proves those bytes, never completion.
        text,tone=('待补齐' if received<total else '等待制作完成' if status=='已收取·任务未完成' else '收取待核对'),'warning'
    elif status in {'需核对质检','任务类型不符','任务编号不符','需核对重复ID','未找到云端记录'}:
        text,tone='收取待核对','warning'
    elif status in {'等待制作完成','等待成品链接'}:
        text,tone=status,'warning'
    elif received:
        text,tone='收取中','warning'
    else: text,tone='收取需处理','error'
    manually_seen=received>0 and unread==0 and bool(seen_versions) and current['view_scope_ok']
    if manually_seen and not pending and not current.get('needs_user_segments'):
        text='已查看'
        if not current['missing'] and received>=total and status=='收取待重试': tone='muted'
    if text not in {'已收取','已查看'} and (pending or received or total):
        target=str(total) if total>0 and current.get('total_known',True) else '?'
        text+=f' {received}/{target}'
    return text,tone,manually_seen


def notice_snapshot(store, *, announce=True):
    if not store.file.exists(): return {'items': [], 'notify_files': 0, 'notify_tasks': 0}
    with store.connect() as db:
        jobs = db.execute('SELECT id,payload FROM jobs ORDER BY rowid DESC').fetchall()
        records = db.execute('SELECT job_id,file_id,record FROM delivery_files').fetchall()
        statuses = {key: (status, detail) for key, status, detail in db.execute('SELECT job_id,status,detail FROM delivery_jobs')}
        seen = set(db.execute('SELECT job_id,file_id,checksum FROM delivery_seen').fetchall())
    by_job = defaultdict(dict)
    for key, file_id, raw in records: by_job[key][file_id] = json.loads(raw)
    groups, available_versions, summaries = {}, [], {}
    for key, raw in jobs:
        payload = json.loads(raw); directory = str(payload.get('target_dir') or '')
        if not os.path.isabs(directory): continue
        canonical = directory_key(directory)
        group = groups.setdefault(canonical, {'directory': directory, 'task_id': str(payload.get('task_id') or ''),
            'files': {}, 'versions': [], 'seen_versions': [], 'current': None})
        files = by_job[key]; valid, missing = 0, 0; valid_versions={}; job_seen=[]; job_unread=0
        cloud=files.get('@cloud',{});discovery=files.get('@discovery',{})
        active_ids = files.get('@discovery', {}).get('file_ids')
        if not isinstance(active_ids, list) or any(not isinstance(item, str) for item in active_ids): active_ids = None
        active_ids = set(active_ids) if active_ids is not None else None
        source=cloud.get('source') or discovery.get('source')
        if source and discovery.get('source')!=source:
            active_ids=set()  # Old-link files are historical, not new receipt progress.
        elif source and active_ids is None and any(proof.get('source') and proof['source']!=source
                for proof in (files.get('@received',{}),files.get('@partial',{}))):
            active_ids=set()
        for file_id, record in files.items():
            if record.get('state') != 'downloaded': continue
            try: exists = cached_file(record, Path(directory)/'result')
            except (OSError, ValueError): exists = False
            if not exists:
                if active_ids is None or file_id in active_ids: missing += 1
                continue
            checksum = str(record.get('sha256') or '')
            if not checksum: continue
            valid_versions[file_id]=checksum
            if active_ids is None or file_id in active_ids: valid += 1
            version = (key, file_id, checksum)
            group['files'][directory_key(record['path'])] = version
            if version not in seen:
                group['versions'].append(list(version)); available_versions.append((canonical, version))
            else: group['seen_versions'].append(list(version))
            if active_ids is None or file_id in active_ids:
                if version in seen:job_seen.append(list(version))
                else:job_unread+=1
        status, detail = statuses.get(key, ('', ''))
        expected = cloud.get('expected')
        total_known=isinstance(expected,int) and expected>0
        if not total_known:
            expected=discovery.get('total') if not source or discovery.get('source')==source else None
            total_known=isinstance(expected,int) and expected>0 and cloud.get('completed') is not False
        if not isinstance(expected, int) or expected < 0:
            expected = sum(record.get('state') in {'downloaded', 'retry'} for file_id, record in files.items()
                if active_ids is None or file_id in active_ids)
        current = {'status': status, 'detail': detail, 'received': valid, 'total': expected, 'missing': missing,
            'ai_review':files.get('@ai_review',{}).get('text',''), 'cloud_complete':cloud.get('completed'),
            'needs_user_segments':needs_user_segments(task_kind(payload),cloud.get('status')),
            'total_known':total_known or not cloud and expected>0,
            'verified_received':verified_receipt(files,valid_versions),
            'verified_partial':verified_partial_receipt(files,valid_versions),
            'receipt_archived':bool(files.get('@received',{}).get('archived')),
            'view_scope_ok':'@received' not in files or same_receipt_scope(files) or same_partial_scope(files)}
        label,tone,_manual=receipt_badge(current,unread=job_unread,seen_versions=job_seen)
        review=review_state(current['ai_review'])
        if review['problem'] and valid:tone='error'
        meaningful = valid or missing or bool(status)
        if meaningful:
            summaries[key]={'text':label,'tone':tone,'tooltip':review_tooltip(
                f'当前收取数量：{valid}/{expected if current["total_known"] else "?"}。\n'+cloud.get('status','')+'\n'+status+'\n'+detail,review['text'])}
        if group['current'] is None and meaningful:
            group['current'] = current
    items = []
    for group in groups.values():
        current = group['current']
        if not current: continue
        total_files = len(group.pop('files')); unread = len(group['versions'])
        status = current['status']; received, total = current['received'], current['total']
        text,tone,manually_seen=receipt_badge(current,unread=unread,seen_versions=group['seen_versions'])
        review=review_state(current['ai_review'])
        if review['problem'] and total_files:
            tone = 'error'
        human=('\n手动状态：已有片段已查看；补段待办仍需处理。' if current.get('needs_user_segments') else
            '\n手动状态：已有片段已查看；仍有下载待完成。' if text.startswith(('收取中','收取待重试')) else '\n手动状态：已查看。') if manually_seen else ''
        retained='\n本地已校验收取完成；后续云端检查异常未撤销收取事实。' if current['verified_received'] and status=='收取待重试' else ''
        target=str(total) if current['total_known'] and total>0 else '?'
        handoff='\nAI已完成，需用户补段。补完后将表格执行状态改为“已完成”；已查看不代表补段完成。' if current.get('needs_user_segments') else ''
        tooltip=review_tooltip(f'Agent成品：本地可用 {total_files} 个，未查看 {unread} 个。{human}{retained}{handoff}\n当前批次收取进度：{received}/{target}。\n{status}\n{current["detail"]}',review['text'])
        group.update(text=text, tone=tone, unread=unread, received=total_files,tooltip=tooltip)
        group.pop('current'); items.append(group)
    claimed, claimed_tasks = 0, set()
    if announce and available_versions:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key, version in available_versions:
                if db.execute('SELECT 1 FROM delivery_seen WHERE job_id=? AND file_id=? AND checksum=?', version).fetchone(): continue
                if db.execute('INSERT OR IGNORE INTO delivery_announced VALUES (?,?,?)', version).rowcount:
                    claimed += 1; claimed_tasks.add(key)
    return {'items': items, 'notify_files': claimed, 'notify_tasks': len(claimed_tasks),'job_summaries':summaries}


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
    return {**notice_snapshot(store, announce=bool(payload.get('announce',True)) and not bool(payload.get('seen') or payload.get('unseen'))),
        'ui_generation': payload.get('ui_generation', 0), 'transient':bool(payload.get('transient')) and not bool(payload.get('seen') or payload.get('unseen'))}
