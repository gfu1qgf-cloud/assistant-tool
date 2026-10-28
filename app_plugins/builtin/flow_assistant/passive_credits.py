"""Observe manual browsers without UI requests, recovery, closure or paid work."""
import math
import re
import time

from .setup_support import project_identity

CAPABILITY='passive_credit_observation_v1'


def flow_interest(service,client):
    """Existing explicit Flow work may still use its normal active checks."""
    if client.get('profile') in service.launching:return True
    rows=service.store.rows()
    if any(row['status'] in {'preparing','uploading','submitting','generating','downloading'} and
           (row['browser']==client.get('id') or row['remote'].get('profile')==client.get('profile')) for row in rows):
        return True
    return not service.safety_paused and any(row['status']=='queued' and
        (not service.paused or row['id'] in service.released) for row in rows)


def defer_active_probe(service,client):
    if not client.get('credit_observation_only'):return False
    if flow_interest(service,client):
        client['credit_observation_only']=False
        return False
    if (client.get('credit_check') or {}).get('state')!='observing':
        client['credit_check']={'state':'observing','at':time.time(),
            'message':'手动使用中：仅后台只读观察；等待页面自然显示账号和积分，不打开菜单或激活窗口'}
    client.update(credits_waiting_ready=False,credits_waiting_account=False,credits_retry_at=0)
    return True


def receive(service,client,message):
    if not client.get('authorized') or CAPABILITY not in client.get('capabilities',[]):return False
    project=project_identity(message.get('url'))
    if (not project or project!=project_identity(client.get('saved_project')) or
            not message.get('page_instance') or message['page_instance']!=client.get('page_instance')):
        return False
    expected=service.preferences.get('profile_projects',{}).get(client.get('profile'))
    if expected and project_identity(expected)!=project:return False
    account=str(message.get('account') or '').strip().casefold()
    points,observed=message.get('balance'),message.get('observed_at')
    if (not re.fullmatch(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}',account) or
            type(points) is not int or not 0<=points<1000000 or
            type(observed) not in (int,float) or not math.isfinite(observed) or abs(time.time()-observed)>30 or
            observed<=client.get('passive_credit_at',0) or observed<client.get('credits_at',0)):
        return False
    # No observation can race a debit, active probe, upload or unresolved paid
    # result. Let the next naturally visible observation update it later.
    if client.get('native_busy') or client.get('batch_running') or any(
            pending['client']==client.get('id') and pending['action']!='snapshot' for pending in service.pending.values()):
        return False
    if any(row['status'] in {'preparing','uploading','submitting','generating','downloading','uncertain','download_failed','archiving'} and
           (row['browser']==client.get('id') or row['remote'].get('profile')==client.get('profile') or
            str(row['remote'].get('account') or '').casefold()==account) for row in service.store.rows()):
        return False
    if client.get('account')!=account:client.pop('prepare_snapshot',None)
    client.update(account=account,credits=points,credits_local=True,credits_at=observed,credit_observation_only=True,
        credits_cached=False,credits_estimated=False,identity_unconfirmed=False,passive_credit_at=observed,
        credit_check={'state':'success','at':observed,'message':'后台只读观察到当前账号和积分；未操作网页'},
        credits_waiting_ready=False,credits_waiting_account=False,credits_retry_at=0)
    # Saving a balance does not authorize closing or repairing a manual window.
    from .credit_refresh import observe
    observe(service,client,{'credit_refresh_checked':message.get('credit_refresh_checked') is True,
        'credit_refresh':message.get('credit_refresh')},started_at=observed)
    from .credit_cooldown import remember_balance
    remember_balance(service,client,manual=True,source='passive')
    from .browser_reuse import reset_recovery
    reset_recovery(service,client)
    return True
