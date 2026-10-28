"""Bounded, idle-only bridge reloads; protected native closure is a last resort."""
import re
import time
import uuid

from .browser_options import snapshot
from .connection_status import browser_keys, configured_profiles
from .setup_support import EXTENSION_VERSION, project_identity, required_bridge_update
from .store import TRANSIENT
from .timing import value

PREFIX='bridge-update:'
ACTIVE={'requesting','opening','awaiting','restarting'}


def needed(client, preferences):
    match=re.fullmatch(r'1\.1\.(0|[1-9][0-9]*)',str(client.get('extension_version') or ''))
    return bool(match and int(match[1])<=int(EXTENSION_VERSION.rsplit('.',1)[1]) and
                (client['extension_version']!=EXTENSION_VERSION or required_bridge_update(client,preferences)))


def holding(client, preferences):
    if not snapshot(preferences)['auto_bridge_recovery']:return False
    entry=preferences.get('bridge_recoveries',{}).get(client.get('profile'),{})
    return entry.get('state') in ACTIVE and entry.get('deadline',0)>time.time()


def idle(service, client):
    profile=client.get('profile');account=str(client.get('account') or '').strip().casefold()
    related=[c for c in service.clients.values() if c.get('profile')==profile or account and
             str(c.get('account') or '').strip().casefold()==account]
    ids={c.get('id') for c in related}
    return not (any(c.get('native_busy') or c.get('batch_running') or
                c.get('reset_wait_until',0)>time.time() for c in related) or
        any(p.get('client') in ids for p in service.pending.values()) or
        any(r['status'] in TRANSIENT|{'archiving','uncertain','download_failed'} and
            (r['browser'] in ids or r['remote'].get('profile')==profile or account and
             str(r['remote'].get('account') or '').strip().casefold()==account) for r in service.store.rows()) or
        any(h.get('profile')==profile or account and str(h.get('account') or '').strip().casefold()==account
            for h in service.store.reset_holds()))


def save(service, profile, entry):
    service.commit_preferences({**service.preferences,'bridge_recoveries':{
        **service.preferences.get('bridge_recoveries',{}),profile:entry}})


def launch_result(service, command):
    profile=command.get('profile');entry=service.preferences.get('bridge_recoveries',{}).get(profile,{})
    if command.get('request_id')!=entry.get('request') or entry.get('state') not in {'requesting','opening'}:return
    now=time.time()
    if command.get('opened'):
        save(service,profile,{**entry,'state':'awaiting','deadline':now+value(service.preferences,'bridge_update_wait_seconds')})
    else:
        from .launch_retries import ui_wait_error
        deferred=ui_wait_error(command.get('error')) or command.get('cancelled')
        save(service,profile,{**entry,'state':'waiting','deadline':0,'next_at':now+5,
            'count':max(0,entry.get('count',1)-int(deferred)),
            'error':str(command.get('error') or '自动重载入口尚未打开')[:400]})


def watch(service):
    if service.closing or not snapshot(service.preferences)['auto_bridge_recovery']:return False
    now=time.time();changed=False
    runnable=not service.paused or bool(service.released)
    queued=runnable and any(r['status']=='queued' and (not service.paused or r['id'] in service.released)
                           for r in service.store.rows())
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold
    from .high_credit import allowed
    profiles=set(configured_profiles(service.preferences))
    for client in list(service.clients.values()):
        profile=client.get('profile')
        if (not client.get('authorized') or profile not in profiles or client.get('credit_observation_only') or
                client.get('runtime_close',{}).get('state') in {'checking','closing','closed','unconfirmed'}):continue
        entry=service.preferences.get('bridge_recoveries',{}).get(profile,{})
        if entry.get('target')!=EXTENSION_VERSION:entry={}
        if not needed(client,service.preferences):
            if entry and entry.get('state')!='done':
                save(service,profile,{**entry,'state':'done','deadline':0,'error':''});changed=True
            continue
        if (not queued or now-client.get('connected',now)<3 or not idle(service,client) or
                account_hold(client,service.preferences) or credit_hold(client,service.preferences) or
                not allowed(client,service.preferences) or
                any(service.preferences.get('browser_blocks',{}).get(key) for key in browser_keys(client)) or
                entry.get('state') in {'stopped','restarting'} or entry.get('next_at',0)>now):continue
        if entry.get('state') in ACTIVE and entry.get('deadline',0)>now:continue
        if entry.get('state') in {'requesting','opening'}:
            # UI queue contention is not a failed reload. Its late GUI guard
            # must fail before another ticket can be used.
            save(service,profile,{**entry,'state':'waiting','deadline':0,'next_at':now+5,
                'count':max(0,entry.get('count',1)-1),'error':'自动重载入口等待UI超时，稍后重新排队。'})
            changed=True;continue
        if entry.get('count',0)>=2:
            from .runtime_close import request_close
            note='扩展自动重载未恢复；正在尝试一次安全重开。'
            save(service,profile,{**entry,'state':'restarting','deadline':now+value(service.preferences,'credit_close_timeout_seconds'),
                                  'restart_attempted':True})
            if not entry.get('restart_attempted') and request_close(service,client,automatic_reason='bridge_update'):
                client['runtime_note']=note
            else:
                save(service,profile,{**entry,'state':'stopped','deadline':0,'restart_attempted':True,
                    'error':client.get('runtime_note') or '未能确认安全关闭，已保留窗口；其他账号继续。'})
            changed=True;continue
        request=PREFIX+uuid.uuid4().hex
        save(service,profile,{'target':EXTENSION_VERSION,'state':'requesting','request':request,
            'count':entry.get('count',0)+1,'deadline':now+value(service.preferences,'browser_startup_seconds'),
            'restart_attempted':entry.get('restart_attempted',False),'error':''})
        client['runtime_note']=f'扩展 {client.get("extension_version")} → {EXTENSION_VERSION}：空闲自动重载，等待版本确认。'
        service.publish()  # Deliver the reservation before its GUI setup result.
        service.execute({'action':'setup_profile','request_id':request,'profile':profile,
            'project':service.preferences['profile_projects'][profile],'automatic':True,'automatic_update':True})
        return True  # One new update entrance per tick, using the normal Flow UI queue.
    return changed


def prepared(plugin, result):
    request=str(result.get('request_id') or '')
    if not request.startswith(PREFIX):return False
    entry=next(((p,e) for p,e in plugin.snapshot.get('preferences',{}).get('bridge_recoveries',{}).items()
                if e.get('request')==request),None)
    if not entry:return True
    profile,record=entry
    def report(ok,error):
        plugin.command.emit({'action':'bridge_update_launch_result','request_id':request,'profile':profile,
                             'opened':ok,'error':error,'cancelled':error=='已取消待启动浏览器'})
    if not result.get('success'):
        report(False,result.get('error'));return True
    def guard():
        state=plugin.snapshot;preferences=state.get('preferences',{})
        current=preferences.get('bridge_recoveries',{}).get(profile,{})
        clients=[c for c in state.get('clients',[]) if c.get('profile')==profile]
        accounts={str(c.get('account') or '').strip().casefold() for c in clients}-{''}
        clients=[c for c in state.get('clients',[]) if c.get('profile')==profile or
                 str(c.get('account') or '').strip().casefold() in accounts]
        ids={c.get('id') for c in clients}
        return (not plugin.stopping and snapshot(preferences)['auto_bridge_recovery'] and
            current.get('request')==request and current.get('state') in {'requesting','opening'} and
            current.get('deadline',0)>time.time() and profile in configured_profiles(preferences) and
            project_identity(preferences.get('profile_projects',{}).get(profile))==project_identity(result.get('project')) and
            (not state.get('paused') or state.get('released')) and not state.get('safety_paused') and
            any(r['status']=='queued' for r in state.get('jobs',[])) and
            not any(c.get('native_busy') or c.get('batch_running') for c in clients) and
            not any(r['status'] in TRANSIENT|{'archiving','uncertain','download_failed'} and
                (r.get('browser') in ids or r.get('remote',{}).get('profile')==profile or
                 str(r.get('remote',{}).get('account') or '').strip().casefold() in accounts) for r in state.get('jobs',[])))
    url=result.get('project_claim_url')
    if not url or project_identity(url)!=project_identity(result.get('project')):
        report(False,'自动重载没有获得有效原项目入口');return True
    plugin.launch_browser(profile,[url],guard=guard,completed=report)
    return True
