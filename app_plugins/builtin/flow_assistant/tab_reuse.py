"""Online Flow recovery stays in its existing tab; never fall back to CLI URLs."""
import time
from urllib.parse import urlsplit, parse_qs

from qt_compat import QtCore
from .setup_support import project_identity, compatible_extension, OPTIONS_URL
from .browser_options import snapshot

NO_SAFE_PROJECT_TAB = '未找到可复用的Flow、空白或本扩展设置页，已保留其他标签；未新建项目分页。'


def route(plugin, entry):
    if entry.get('native_project_start_used'):return None
    targets=entry['targets']
    if len(targets)!=1:return None
    url=targets[0];identity=project_identity(url)
    client=next((c for c in plugin.snapshot.get('clients',[]) if c.get('profile')==entry['profile'] and
        project_identity(c.get('saved_project') or c.get('project'))==identity),None) if identity else None
    args=parse_qs(urlsplit(url).fragment)
    settings_requested=args.get('lzx_action',[''])[0] in {'options','update'}
    if identity and args.get('lzx_setup') and (settings_requested or
            client and not compatible_extension(client.get('extension_version'),minimum_patch=54)):
        # The on-disk options script can upgrade an old running worker. Opening
        # this privileged settings entrance does not duplicate its Flow page.
        # A missing connection alone is NOT an update request: ordinary cold
        # pairing must still open its HTTPS project, never an internal-only tab.
        ticket=args['lzx_setup'][0];port=args.get('lzx_port',[''])[0]
        return [OPTIONS_URL+'#setup='+ticket+'&port='+port]
    if not client:return None
    pending=getattr(plugin,'tab_reuse_entries',{})
    existing=next((e for e in pending.values() if e['profile']==entry['profile'] and e['targets']==targets),None)
    if existing:
        existing['callbacks'].extend(entry['callbacks'])
        return True,None
    plugin.tab_reuse_entries=pending;pending[entry['ticket']]=entry
    entry['opened']=True;entry['tab_reuse_pending']=True
    plugin.command.emit({'action':'reuse_project_tab','profile':entry['profile'],'browser':client['id'],
        'url':url,'request_id':entry['ticket'],'host_token':entry['ticket'] if entry.get('lease') else ''})
    def timeout():
        if entry['ticket'] in pending:
            result(plugin,{'request_id':entry['ticket'],'success':False,'error':'原标签页恢复响应超时；未额外打开Flow分页，可稍后重试。'})
    QtCore.QTimer.singleShot(22000,timeout)
    return True,None


def result(plugin, value):
    entry=getattr(plugin,'tab_reuse_entries',{}).pop(value.get('request_id'),None)
    if not entry:return
    entry.pop('tab_reuse_pending',None)
    if value.get('native_project_start_required') and not entry.get('native_project_start_used'):
        # The authenticated background just proved there is no saved-project
        # tab or safe landing surface. One normal HTTPS open can now recover
        # a profile left on chrome://extensions, without guessing other tabs.
        entry['native_project_start_used']=True
        plugin._launch_now(entry)
        return
    plugin._launch_report(entry,value.get('success') is True,value.get('error'))
    if plugin.launch_ui.active is entry:plugin.launch_ui.finish()
    elif not plugin.stopping and plugin.pending_browser_launches and not plugin.launch_timer.isActive():plugin.launch_timer.start(0)


def request(service, command):
    def fail(message):
        service.tab_reused.emit({'request_id':command.get('request_id'),'success':False,'error':message})
    client=next((c for c in service.clients.values() if c.get('authorized') and c.get('id')==command.get('browser') and
        c.get('profile')==command.get('profile')),None)
    identity=project_identity(command.get('url'))
    if not client or not identity or identity!=project_identity(client.get('saved_project')):
        fail('原浏览器连接或项目已变化，未额外打开分页。');return
    from .connection_recovery import occupied
    if occupied(service,client) or client.get('native_busy'):
        fail('原浏览器仍有操作／生成或结果待核对，暂不改变原标签页。');return
    from .ui_mutex import state
    owner=state(service).get('owner') or {}
    held=bool(owner.get('host') and owner.get('token')==command.get('host_token') and
        owner.get('profile')==client['profile'] and time.time()-owner.get('at',0)<35)
    if snapshot(service.preferences)['ui_mutex'] and not held:
        fail('原标签页恢复尚未取得UI操作权限，请稍后重试；未打开新分页。');return
    if not compatible_extension(client.get('extension_version'),minimum_patch=54):
        fail('当前扩展需更新到1.1.54后才能在原标签页恢复；未启动新的Flow分页。');return
    action='reuse_project_tab'
    before=set(service.pending)
    deadline=min(time.time()+15,owner.get('at',time.time())+30) if held else time.time()+15
    if not service.request(client,action,{'url':command['url'],'idle':True,'host_ui_lease':held,'expires_at':deadline}):
        fail('原浏览器连接未就绪，未额外打开分页。');return
    pending=next(p for k,p in service.pending.items() if k not in before)
    pending['tab_reuse_request']=command['request_id']
    pending['project_missing_at_request']=client.get('project_missing') is True
    pending['project_target']=command['url']


def response(service, pending, message):
    client=next((c for c in service.clients.values() if c.get('authorized') and c.get('id')==pending['client']),None)
    from .connection_recovery import occupied
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold
    native=bool(message.get('success') is not True and message.get('error')==NO_SAFE_PROJECT_TAB and
        pending.get('project_missing_at_request') and client and client.get('project_missing') and
        not client.get('page_ready') and not client.get('native_busy') and not occupied(service,client) and
        not account_hold(client,service.preferences) and not credit_hold(client,service.preferences) and
        project_identity(pending.get('project_target'))==project_identity(client.get('saved_project')))
    service.tab_reused.emit({'request_id':pending['tab_reuse_request'],'success':message.get('success') is True,
        'native_project_start_required':native,'error':str(message.get('error') or '')[:600]})
