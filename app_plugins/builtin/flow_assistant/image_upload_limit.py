"""Five failed upload rounds retire one image, never its healthy accounts."""
import json
import re
import time

from .store import submission_risk

LIMIT=5
SETTING='image_upload_failures'
NOTE='图片实际上传失败已达5次，已放弃该图片；所有同图未提交任务已跳过，其他图片继续。'


def image_key(row):return row['payload'].get('image_identity',{}).get('sha256','')


def cached_cycle(service,cycle):
    # Project identity is a tuple in memory and a list after SQLite JSON.
    key=tuple(tuple(v) if isinstance(v,list) else v for v in cycle.get('scope',[]))
    return getattr(service,'upload_cycles',{}).get(key)


def blocked(service,row):
    sha=image_key(row);entry=service.preferences.get(SETTING,{}).get(sha,{})
    return bool(sha and len(entry.get('failed',[]))>=LIMIT and entry.get('base')==service.store.reference_revision(sha))


def remember_failure(service,row,error='',*,expired=False):
    cycle=row['remote'].get('upload_cycle',{});attempt=cycle.get('upload_attempt',{})
    token=attempt.get('request','')
    if cycle.get('confirmed') or not re.fullmatch('[a-f0-9]{32}',token):return 0
    if any(s in str(error) for s in ('拖放失败','未拖放图片','尚未执行拖放')):
        attempt['state']='not_sent'
        cached=cached_cycle(service,cycle)
        if cached and cached.get('upload_attempt',{}).get('request')==token:cached['upload_attempt']=dict(attempt)
        return 0
    # A check-only failure, a rejected UI permit or a definite no-drag error
    # cannot increment this counter. A round must have entered upload first.
    observed=('本条参考图上传未确认' in str(error) and '拖放失败' not in str(error) and
        '未重复拖放' not in str(error) and any(s in str(error) for s in (
            '图片上传达到最长等待时间','图片上传持续无进展','网页明确提示')))
    if not observed and not expired:return 0
    if attempt.get('state') not in {'pending','failed'}:return 0
    sha=image_key(row);base=cycle.get('base','')
    entries=dict(service.preferences.get(SETTING,{}));entry=entries.get(sha,{})
    failed=list(entry.get('failed',[])) if entry.get('base')==base else []
    if token not in failed:
        failed=(failed+[token])[-LIMIT:]
        entries[sha]={'base':base,'failed':failed,'at':time.time()}
        service.commit_preferences({**service.preferences,SETTING:entries})
    attempt['state']='failed'
    cycle['upload_attempt']=attempt
    cached=cached_cycle(service,cycle)
    if cached and cached.get('upload_attempt',{}).get('request')==token:cached['upload_attempt']=dict(attempt)
    return len(failed)


def confirmed(service,row,cycle):
    attempt=cycle.get('upload_attempt',{})
    if attempt.get('state') not in {'pending','failed'}:return
    attempt['state']='confirmed'
    sha=image_key(row);entries=dict(service.preferences.get(SETTING,{}))
    if sha in entries and cycle.get('base','')==service.store.reference_revision(sha):
        entries.pop(sha,None);service.commit_preferences({**service.preferences,SETTING:entries})


def abandon(service,sha):
    """Atomically mark all unpaid clips; retain original paid/download rows."""
    base=service.store.reference_revision(sha);now=time.time();changed=set()
    with service.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        charged={r[0] for r in db.execute('SELECT attempt FROM charges')}
        rows=[service.store._row(r) for r in db.execute('SELECT id,payload,status,attempt,browser,remote,error,updated FROM jobs')]
        for row in rows:
            if (image_key(row)!=sha or row['status'] not in {'collected','queued','preparing','uploading','failed'} or
                    submission_risk(row,row['attempt'] in charged) or row['id'] in service.archiving):continue
            if row['remote'].get('image_upload_abandoned',{}).get('base')==base and row['status']=='failed':continue
            remote={**row['remote'],'image_upload_abandoned':{'base':base,'failures':LIMIT},
                'transfer_retries':{**row['remote'].get('transfer_retries',{}),'upload':{
                    **row['remote'].get('transfer_retries',{}).get('upload',{}),'attempt':row['attempt'],
                    'stopped':True,'permanent':True,'active':False,'next_at':0,'last_error':NOTE}}}
            db.execute('UPDATE jobs SET status="failed",remote=?,error=?,updated=? WHERE id=?',
                (json.dumps(remote,ensure_ascii=False),NOTE,now,row['id']))
            db.execute('INSERT INTO history VALUES (?,? ,"failed",?)',(row['id'],now,NOTE))
            changed.add(row['id'])
            if row['attempt'] and row['browser'] and any(p.get('job')==row['id'] for p in service.pending.values()):
                db.execute('INSERT OR REPLACE INTO reset_holds VALUES (?,?,?,?,?)',
                    (row['attempt'],row['browser'],remote.get('profile',''),str(remote.get('account') or '').casefold(),now+5))
    for key,pending in list(service.pending.items()):
        if pending.get('job') in changed and pending.get('action') in {'prepare','upload_images'} and not pending.get('retired'):
            pending.update(retired=True,cancel_deadline=now+5)
            client=next((c for c in service.clients.values() if c.get('id')==pending['client']),None)
            if client:service.send(client,{'type':'cancel_operation','action':'cancel_operation','target_request_id':key})
    if changed:service.refresh_reset_holds()
    return bool(changed)


def watch(service):
    changed=False
    entries=service.preferences.get(SETTING,{})
    limited={sha:entry for sha,entry in entries.items() if len(entry.get('failed',[]))>=LIMIT}
    if not limited:return False
    pending={image_key(r) for r in service.store.rows() if r['status'] in {'collected','queued','preparing','uploading'}}
    for sha,entry in limited.items():
        if sha in pending and entry.get('base')==service.store.reference_revision(sha):
            changed=abandon(service,sha) or changed
    return changed
