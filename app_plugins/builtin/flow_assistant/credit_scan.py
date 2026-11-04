"""Explicit, serial inventory of current and historical high-credit profiles."""
import copy,time,uuid
from .setup_support import project_identity
from .timing import value,request_timeout


def snapshot(service):
    return copy.deepcopy(getattr(service,'credit_scan',{}))


def current(service):
    scan=getattr(service,'credit_scan',{})
    if not scan.get('running'):return None
    return next((e for e in scan['entries'] if e['phase'] not in {'pending','finished'}),None)


def holds(service,profile):
    entry=current(service)
    return bool(entry and entry['profile']==profile)


def attach(service,client):
    if not holds(service,client.get('profile')):return
    client['credit_scan']=service.credit_scan['id']
    if current(service).get('opened'):client['credit_observation_only']=True


def start(service):
    if getattr(service,'credit_scan',{}).get('running'):return
    from model.OfflineMode import is_offline
    if is_offline():
        service.credit_scan={'running':False,'entries':[],'message':'当前为离线模式，无法刷新网页积分。'};return
    from .high_credit import account_key,is_high
    from .image_affinity import cached_profile
    online=[c for c in service.clients.values() if c.get('authorized')]
    profiles=list(dict.fromkeys([c.get('profile') for c in online if c.get('profile')]+
        list(service.preferences.get('profile_projects',{}))+
        [key[len('profile:'):] for key in service.preferences.get('high_credit_links',{}) if key.startswith('profile:')]))
    from .ordered_dispatch import browser_order
    connected={c.get('profile') for c in online}
    profiles.sort(key=lambda p:(p not in connected,browser_order({'profile':p,'id':''},service.preferences)))
    entries=[];seen=set();records=service.preferences.get('high_credit_accounts',{})
    for profile in profiles:
        client=next((c for c in online if c.get('profile')==profile),None) or cached_profile(profile,service.preferences)
        account=account_key(client,service.preferences)
        if not is_high(client,service.preferences) and account not in records:continue
        identity=account or 'profile:'+profile
        if identity in seen:continue
        seen.add(identity)
        entries.append({'profile':profile,'account':account,'credits':None,'phase':'pending','result':'待检测','note':'','at':0})
    service.credit_scan={'id':uuid.uuid4().hex,'running':bool(entries),'cancelled':False,'entries':entries,'started_at':time.time(),
        'message':'按顺序刷新千分账号积分' if entries else '没有已识别的千分账号；请先连接账号检测一次积分。'}


def cancel(service):
    scan=getattr(service,'credit_scan',{})
    if scan.get('running'):
        scan['cancelled']=True;scan['message']='正在停止；当前已发出的读取／关闭结束后退出'
        for entry in scan['entries']:
            if entry['phase']=='pending':entry.update(phase='finished',result='已停止')


def client_for(service,entry):
    return next((c for c in service.clients.values() if c.get('authorized') and c.get('profile')==entry['profile']),None)


def blocked(service,client):
    from .account_policy import hold_reason
    from .connection_status import browser_keys
    from .runtime_close import PAUSED
    reason=hold_reason(client,service.preferences)
    if reason:return reason
    marks=[service.preferences.get('browser_blocks',{}).get(k,'') for k in browser_keys(client)]
    return next((m for m in marks if m==PAUSED or str(m).startswith('用户手动')),'')


def finish(service,entry,client=None):
    entry['phase']='finished'
    if entry.get('existing_window'):
        if not entry.get('was_attempted'):service.launch_attempts.discard(entry['profile'])
        if not entry.get('was_launched'):service.launched.discard(entry['profile'])
    if client:
        if client.get('credit_scan')==service.credit_scan['id']:client.pop('credit_scan',None)
        client.update(credits_waiting_ready=False,credits_waiting_account=False,credits_retry_at=0)


def complete(service,entry,result,note='',client=None):
    entry.update(result=result,note=str(note)[:600],at=time.time())
    if entry.get('opened') and client and (client.get('runtime_close') or {}).get('state')!='closed':
        # Close only windows used by this scan, through the existing protected
        # idle/project/download checks. Personal tabs prevent the entire close.
        from .runtime_close import request_close
        if request_close(service,client,temporary=True):
            entry.update(phase='closing',deadline=time.time()+request_timeout(service.preferences,'close_browser'));return
        entry['note']+=('；' if entry['note'] else '')+'检测窗口保留：'+client.get('runtime_note','未确认可以安全关闭')
    finish(service,entry,client)


def launch_result(service,command):
    scan=getattr(service,'credit_scan',{})
    slot=service.preferences.get('browser_slots',{}).get(command.get('profile'),{})
    if (command.get('opened') is False and slot.get('scan_id') and slot.get('scan_id')==command.get('scan_id') and
            not any(c.get('authorized') and c.get('profile')==command.get('profile') for c in service.clients.values())):
        # An older cancelled launch may reply after a new report was started.
        # Release only the exact new reservation that never actually opened.
        from .browser_slots import closed
        closed(service,command['profile'])
    if command.get('scan_id')!=scan.get('id'):return
    entry=next((e for e in scan['entries'] if e['profile']==command.get('profile')),None)
    if not entry:return
    if command.get('opened') is False:
        # A failed GUI launch proves only its own reservation never opened.
        if entry['phase']=='starting':
            complete(service,entry,'已停止' if scan.get('cancelled') else '失败',command.get('error') or '浏览器未启动')


def watch(service):
    scan=getattr(service,'credit_scan',{})
    if not scan.get('running'):return False
    from .runtime_close import occupied
    entry=current(service);now=time.time()
    if not entry:
        entry=next((e for e in scan['entries'] if e['phase']=='pending'),None)
        if not entry:
            scan['running']=False
            scan['finished_at']=now
            scan['message']='已停止全面刷新' if scan['cancelled'] else '千分账号积分刷新完成'
            return True
        client=client_for(service,entry)
        from .image_affinity import cached_profile
        identity=client or cached_profile(entry['profile'],service.preferences)
        reason=blocked(service,identity)
        if reason:complete(service,entry,'跳过',reason);return True
        if client and (occupied(service,client) or any(p['client']==client['id'] for p in service.pending.values())):
            complete(service,entry,'跳过','账号正在操作／生成／下载或核对原结果，未打断使用');return True
        project=service.preferences.get('profile_projects',{}).get(entry['profile'])
        if not project_identity(project):complete(service,entry,'跳过','未保存有效Flow项目地址');return True
        if not client:
            from .browser_budget import available
            from .browser_slots import live_starting_profiles
            slots=dict(service.preferences.get('browser_slots',{}))
            if entry['profile'] in live_starting_profiles(service) or slots.get(entry['profile'],{}).get('state')=='opening':
                complete(service,entry,'跳过','原配置仍有未确认的启动，未重复打开');return True
            if not available(service,entry['profile']):
                complete(service,entry,'跳过','已到浏览器窗口上限；释放名额后可重新刷新');return True
            existing=entry['profile'] in slots
            entry.update(phase='starting',result='等待连接',opened=not existing,existing_window=existing,
                was_attempted=entry['profile'] in service.launch_attempts,was_launched=entry['profile'] in service.launched,
                deadline=now+value(service.preferences,'browser_startup_seconds'))
            if not existing:
                slots[entry['profile']]={'state':'opening','at':now,'scan_id':scan['id'],'purpose':'credit_scan'}
                service.commit_preferences({**service.preferences,'browser_slots':slots})
            service.launch_attempts.add(entry['profile']);service.launched.add(entry['profile'])
            # The GUI guard must see the reservation before receiving launch.
            service.publish()
            service.credit_scan_launch_requested.emit({'scan_id':scan['id'],'profile':entry['profile'],'project':project})
            return True
        entry.update(phase='starting',result='等待页面',deadline=now+value(service.preferences,'browser_startup_seconds'))
    client=client_for(service,entry)
    if client:attach(service,client)
    if entry['phase']=='closing':
        state=(client or {}).get('runtime_close',{}).get('state')
        if not client:
            if entry['profile'] in service.preferences.get('browser_slots',{}):
                entry['note']+=('；' if entry['note'] else '')+'检测窗口已断开但未确认退出，保留窗口名额'
            finish(service,entry);return True
        if state=='closed':finish(service,entry,client);return True
        if state in {'failed','unconfirmed'} or now>=entry['deadline']:
            entry['note']+=('；' if entry['note'] else '')+'检测窗口保留：'+client.get('runtime_note','关闭未确认')
            finish(service,entry,client);return True
        return False
    if entry['phase']=='starting':
        if scan['cancelled']:
            if client:complete(service,entry,'已停止',client=client);return True
            if now>=entry['deadline']:complete(service,entry,'已停止','启动未连接，未确认窗口退出');return True
            return False
        if not client or not (client.get('page_loaded') or client.get('page_ready')):
            if now>=entry['deadline']:
                complete(service,entry,'失败','等待原项目连接／页面超过启动时限，已继续下个账号',client);return True
            return False
        from .connection_recovery import occupied as work_occupied
        if work_occupied(service,client) or client.get('native_busy') or any(p['client']==client['id'] and p['action']!='snapshot' for p in service.pending.values()):
            complete(service,entry,'跳过','账号出现其他操作，已让出检测',client);return True
        before=set(service.pending)
        if not service.request(client,'get_points',manual_credit=True):
            complete(service,entry,'失败',client.get('runtime_note') or '积分读取请求未发送',client);return True
        request=next(k for k in service.pending if k not in before)
        service.pending[request]['credit_scan_id']=scan['id']
        entry.update(phase='reading',result='读取中',request=request,sent_at=now,deadline=now+request_timeout(service.preferences,'get_points',client))
        return True
    if entry['phase']=='reading':
        request=service.pending.get(entry['request'])
        if request:
            # The normal UI mutex advances request.at only for verified waits.
            entry['deadline']=max(entry['deadline'],request['at']+request_timeout(service.preferences,'get_points',client))
            return False
        if not client:complete(service,entry,'失败','读取期间浏览器连接断开');return True
        check=client.get('credit_check',{})
        if check.get('state')=='success' and check.get('at',0)>=entry['sent_at'] and client.get('credits_local') and not client.get('credits_cached'):
            entry.update(account=client.get('account',''),credits=client['credits'])
            complete(service,entry,'已刷新',client=client);return True
        complete(service,entry,'已停止' if scan['cancelled'] else '失败',check.get('message') or client.get('page_error') or '本次未取得新积分，保留上次余额',client)
        return True
    return False
