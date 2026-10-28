"""Durable download-only work never reserves a generation account or lane."""
import time
from .media_identity import generated_bindings
from .transfer_policy import retry_info, retry_limit
from .timing import delay, value


def finished(row):
    from .video_catalog import manual_binding
    if manual_binding(row):return True
    bindings=[b for b in generated_bindings(row['remote']) if b['taskId']==row['remote'].get('upstream_task')]
    return bool(bindings and any(set(b['mediaIds']) & (set(b.get('readyIds',[]))-set(b.get('failedIds',[]))) for b in bindings) and
        all(set(b['mediaIds']) <= set(b.get('readyIds',[])) | set(b.get('failedIds',[])) for b in bindings))


def detached(row):
    return bool(row['remote'].get('download_backlog')) and finished(row)


def detach(service,row):
    if row['status'] not in {'downloading','download_failed'} or not finished(row):return False
    info=retry_info(row,'download');old=row['remote'].get('download_backlog') or {}
    if not info:
        info={'attempt':row['attempt'],'count':0,'stopped':retry_limit(service.preferences,'download')==0,
            'active':row['status']=='downloading','sent_at':time.time(),
            'next_at':time.time()+delay(service.preferences,'download_retry_delays',0)}
    backlog={**old,'since':old.get('since') or time.time(),'auto':old.get('auto',not info.get('stopped'))}
    remote={**row['remote'],'cloud_finished':True,'download_skipped':True,'download_backlog':backlog,
        'transfer_retries':{**row['remote'].get('transfer_retries',{}),'download':info}}
    service.store.update(row['id'],'download_parked',remote=remote,
        error=row['error'] or '原视频已生成，下载独立处理；账号和生成名额已释放。')
    service._release_skipped_wait(service.store.get(row['id']))
    return True


def reconnect(service,client):
    """One fresh, bounded download cycle per authenticated browser connection."""
    changed=False
    for row in service.store.rows():
        if (row['browser']!=client['id'] or row['status']!='download_parked' or not finished(row)):continue
        previous=row['remote'].get('download_backlog') or {}
        connection=client.get('connected',0)
        if previous.get('connection')==connection:continue
        # This is the original durable browser identity. Never substitute an
        # arbitrary account/profile after extension storage has been erased.
        remote={**row['remote'],'cloud_finished':True,'download_skipped':True,'download_backlog':{
            **previous,'since':time.time(),'connection':connection,'auto':True,'cancel_sent':False,'cancel_attempts':0,'cancel_next_at':0}}
        remote['transfer_retries']={**remote.get('transfer_retries',{}),'download':{
            'attempt':row['attempt'],'count':0,'next_at':time.time()+5,'stopped':False,'active':False}}
        service.store.update(row['id'],'download_parked',remote=remote,error='原浏览器已重连，等待空闲时续下原视频；不重新生成。')
        changed=True
    return changed


def watch(service):
    changed=False;now=time.time()
    for row in service.store.rows():changed |= detach(service,row)
    clients={c['id']:c for c in service.clients.values() if c.get('authorized')}
    rows=service.store.rows()
    generation=[r for r in rows if r['status'] in {'preparing','uploading','submitting','generating'}]
    for row in rows:
        if row['status']!='download_parked' or not detached(row):continue
        client=clients.get(row['browser']);info=retry_info(row,'download');backlog=row['remote']['download_backlog']
        if not client or service._transfer_pending(row,'download'):continue
        if client.get('runtime_close',{}).get('state') in {'checking','closing','closed'}:continue
        if any(p['client']==client['id'] for p in service.pending.values()):continue
        # Native transfers that never finish must not retain all eight windows.
        # Cancel only the exact managed attempt, retaining its media receipts.
        deadline=max(10,min(120,value(service.preferences,'download_stall_seconds')))
        if info.get('active') and now-(info.get('sent_at') or backlog['since'])>=deadline:
            if ('download_backlog_v1' in client.get('capabilities',[]) and not backlog.get('cancel_sent') and
                    backlog.get('cancel_attempts',0)<3 and backlog.get('cancel_next_at',0)<=now):
                service.request(client,'skip_download',{'batch_id':row['attempt'],'park_native_downloads':True})
                remote={**row['remote'],'download_backlog':{**backlog,'cancel_sent':True,'auto':False},
                    'transfer_retries':{**row['remote'].get('transfer_retries',{}),'download':{**info,'stopped':True}}}
                service.store.update(row['id'],'download_parked',remote=remote,error='原视频下载等待超时，已暂存；释放账号继续生成，原浏览器下次重连时续下。')
                changed=True
            continue
        if not backlog.get('auto') or info.get('stopped') or info.get('active') or info.get('next_at',0)>now:continue
        # Generation UI takes precedence. Download recovery may use an idle
        # original browser; it never opens another browser or replays run_tasks.
        if client.get('native_busy') or client.get('batch_running') or any(r['status'] in {'preparing','uploading','submitting','generating'} and
            (r['browser']==client['id'] or r['remote'].get('profile')==client.get('profile')) for r in generation):continue
        if row['remote'].get('account') and row['remote']['account']!=client.get('account'):continue
        if info.get('count',0)>=retry_limit(service.preferences,'download'):
            service.transfer_failed(row,'download','下载自动重试次数已用尽，等待原浏览器下次重连或手动重试')
            changed=True;continue
        changed |= service.retry_download(row,automatic=True)
    return changed


def cancelled(service,client,attempt,message):
    for row in service.store.rows():
        if row['browser']!=client['id'] or row['attempt']!=attempt or not detached(row):continue
        entry=row['remote']['download_backlog'];info=retry_info(row,'download')
        progressing=message.get('success') and (message.get('data') or {}).get('progressing',0)>0
        success=message.get('success') and (message.get('data') or {}).get('parked') is True
        remote={**row['remote'],'download_backlog':{**entry,'cancel_sent':bool(success),
            'cancel_attempts':0 if progressing else entry.get('cancel_attempts',0)+1,'cancel_next_at':time.time()+30},
            'transfer_retries':{**row['remote'].get('transfer_retries',{}),'download':{**info,'active':not success,'stopped':True}}}
        service.store.update(row['id'],'download_parked',remote=remote,
            error='原下载已暂存，原浏览器重连后续下；不影响后续生成。' if success else '原下载仍有字节进展，继续后台传输；不占生成名额。' if progressing else '暂存下载未确认：'+str(message.get('error') or '扩展未返回确认'))
        if success:
            entries=dict(service.preferences.get('browser_lifecycle',{}));old=entries.get(client.get('profile'),{})
            if old.get('state')=='close_failed' and '下载' in str(old.get('last_error') or ''):
                entries[client['profile']]={**old,'next_at':0,'close_attempts':0}
                service.commit_preferences({**service.preferences,'browser_lifecycle':entries})
        return
