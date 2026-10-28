"""Retry pre-permit contention without a lifetime cap; bound real UI faults."""
import json
import math
import time
from .media_identity import generated_bindings
from .timing import value, delay

PREFIX = '前台 UI 操作互斥申请未完成'
STOP_PREFIX = '前台UI锁申请连续失败'
WAIT_STATES = {'rejected','expired','busy','waiting','timeout','timed_out'}


def permit_wait(message):
    if message.get('code')!='FLOW_UI_MUTEX_UNAVAILABLE':return False
    text=str(message.get('error') or '').strip()
    if not text:return True  # Older reviewed bridges only returned the code.
    if text.startswith(PREFIX):text=text[len(PREFIX):].lstrip('：: ').strip()
    return text.casefold() in WAIT_STATES


def recover(service, client, row, action, message):
    code = message.get('code')
    if action not in {'prepare','upload_images','run_tasks'}:
        return False
    if code != 'FLOW_UI_MUTEX_UNAVAILABLE' and not (action in {'prepare','upload_images'} and code=='FLOW_UI_MUTEX_EXPIRED'):
        return False
    # An expired permit after a Generate click is ambiguous and is excluded.
    if any(b['mediaIds'] for b in generated_bindings(row['remote'])) or any(row['remote'].get(key) for key in
            ('generation_response','download','unverified_download','page_recovery','cloud_finished')):
        return False
    previous=row['remote'].get('ui_permission_retry', {})
    count = previous.get('count', 0) + 1
    waiting=permit_wait(message)
    faults=previous.get('fault_count',0)+(0 if waiting else 1)
    waits=previous.get('wait_count',0)+(1 if waiting else 0)
    limit = value(service.preferences, 'ui_permission_retry_attempts')
    now = time.time()
    seconds=delay(service.preferences,'ui_permission_retry_delays',(waits if waiting else faults)-1)
    if waiting:seconds=max(1,seconds)
    retry = {'count':count,'wait_count':waits,'fault_count':faults,'kind':'waiting' if waiting else 'fault',
             'limit':limit,'at':now,'next_at':now+seconds,'operation':action,'no_generate':True}
    remote = {key:row['remote'][key] for key in ('account','profile','credit_pack','generation_retry','upload_recovery',
        'transfer_retries','upload_reference','upload_phase','upload_cycle') if key in row['remote']}
    if remote.get('transfer_retries',{}).get('upload'):
        remote['transfer_retries']={**remote['transfer_retries'],'upload':{
            **remote['transfer_retries']['upload'],'attempt':''}}
    cycle=remote.get('upload_cycle',{})
    if (action=='upload_images' and cycle.get('upload_attempt',{}).get('request') and
            cycle['upload_attempt']['request']==message.get('request_id')):
        cycle={**cycle};cycle.pop('upload_attempt',None);remote['upload_cycle']=cycle
        from .image_upload_limit import cached_cycle
        cache=cached_cycle(service,cycle)
        if cache and cache.get('upload_attempt',{}).get('request')==message.get('request_id'):cache.pop('upload_attempt',None)
    remote['ui_permission_retry'] = retry
    remote['prepare_retry'] = {'browser':client['id'],'profile':client.get('profile',''),'count':faults,
                              'ui_wait':waiting,'next_at':retry['next_at']}
    status = 'queued' if waiting or faults <= limit else 'failed'
    reason = (f'等待前台UI锁（已重新申请{waits}次，{seconds:g}秒后继续）；尚未点击生成，保留原分包，不计失败次数。' if waiting else
              f'前台UI窗口／权限异常，自动等待后重新核对（{faults}/{limit}）；尚未点击生成，保留原分包。' if status=='queued' else
              f'前台UI窗口／权限异常已达重试上限{limit}次，已停止本条并释放账号／并行名额。')
    if not waiting:reason+=' 原因：'+str(message.get('error') or message.get('code') or '未收到有效操作权限')[:400]
    with service.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT attempt,status FROM jobs WHERE id=?',(row['id'],)).fetchone()
        if not current or current[0] != row['attempt'] or current[1] not in {'queued','failed','preparing','uploading','submitting','generating','uncertain'}:
            return False
        refund = db.execute('SELECT COALESCE(SUM(cost),0) FROM charges WHERE attempt=? AND job=?',(row['attempt'],row['id'])).fetchone()[0]
        db.execute('UPDATE charges SET cost=0,source="rejected_before_submission" WHERE attempt=? AND job=?',(row['attempt'],row['id']))
        db.execute('UPDATE jobs SET status=?,attempt="",browser="",remote=?,error=?,updated=? WHERE id=?',
            (status,json.dumps(remote,ensure_ascii=False),reason,now,row['id']))
        db.execute('INSERT INTO history VALUES (?,?,?,?)',(row['id'],now,status,reason+' 原执行号：'+row['attempt']))
    client.pop('prepare_snapshot',None)
    client['prepare_wait'] = remote['prepare_retry']
    if status=='failed':client['prepare_wait']['next_at']=now+30
    client['runtime_note'] = reason
    account = str(row['remote'].get('account') or '').strip().casefold()
    if refund and account:
        debits = getattr(service,'credit_debits',{})
        debits[account] = max(0,debits.get(account,0)-refund)
        refunded = None
        for current in service.clients.values():
            points = current.get('credits')
            if str(current.get('account') or '').strip().casefold()==account and type(points) in (int,float) and math.isfinite(points):
                current.update(credits=points+refund,credits_estimated=True,credits_local=True)
                refunded=current
        if refunded:
            from .credit_cooldown import remember_balance
            remember_balance(service,refunded,manual=True,source='ui_denied_unused_reservation')
        else:
            balances=dict(service.preferences.get('credit_balances',{}))
            balances[account]={**balances.get(account,{}),'recheck_after':now}
            service.commit_preferences({**service.preferences,'credit_balances':balances})
    # Remove only leases whose exact pending request/attempt is now retired.
    # Other windows' live operations remain owned and retain their deadlines.
    from .ui_mutex import watch
    watch(service)
    return True


def recover_legacy(service):
    blocks = service.preferences.get('browser_blocks',{})
    removable = {key for key,reason in blocks.items() if str(reason).startswith(PREFIX)}
    if removable:
        service.commit_preferences({**service.preferences,'browser_blocks':{key:reason for key,reason in blocks.items() if key not in removable}})
    for row in service.store.rows():
        if row['status'] not in {'queued','failed','uncertain'}:continue
        legacy_stop=(row['status']=='failed' and row['error'].startswith(STOP_PREFIX) and
            row['remote'].get('ui_permission_retry') and not row['remote']['ui_permission_retry'].get('kind'))
        if not row['error'].startswith(PREFIX) and not legacy_stop:continue
        pseudo={'id':row['browser'] or row['remote'].get('prepare_retry',{}).get('browser',''),
            'profile':row['remote'].get('profile',''),'account':row['remote'].get('account','')}
        recover(service,pseudo,row,'run_tasks' if service.store.attempt_charged(row['attempt']) else 'prepare',
                {'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'rejected' if legacy_stop else row['error']})
