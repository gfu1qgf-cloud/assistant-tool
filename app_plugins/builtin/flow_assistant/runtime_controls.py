"""Explicit remote controls and bounded F5 recovery of an unpaid idle page."""
import time
from .connection_status import browser_keys
from .connection_recovery import occupied
from .account_policy import account_hold
from .credit_cooldown import credit_hold
from .timing import value


def refresh(service, client, *, automatic=False):
    if 'runtime_controls_v1' not in client.get('capabilities',[]):
        client['runtime_note']='当前扩展尚未加载运行时 F5 支持；相关浏览器正常重启后可用。'
        return False
    if (not client.get('authorized') or occupied(service, client) or client.get('native_busy') or
            account_hold(client, service.preferences) or credit_hold(client, service.preferences) or
            service.safety_paused or any(p['client'] == client['id'] for p in service.pending.values())):
        client['runtime_note'] = 'F5 未执行：当前仍有操作、生成、下载或待核对结果，或账号被隔离／冷却。'
        return False
    info = client.get('f5_recovery', {})
    now = time.time()
    if automatic and (info.get('count', 0) >= value(service.preferences, 'connection_repair_attempts') or
                      now < info.get('next_at', 0)): return False
    if not service.request(client, 'repair_project', {'idle': True, 'url':client.get('saved_project'), 'force_reload':True}): return False
    if not automatic: client.pop('reuse_recovery',None)
    client['f5_recovery'] = {'count':info.get('count',0)+int(automatic),
        'next_at':now+value(service.preferences,'connection_repair_wait_seconds')}
    client['runtime_note'] = '已请求 F5 刷新原项目，等待扩展再次核对空闲及原页面。'
    client['page_ready'] = False
    client['page_unready_at'] = now
    return True


def execute(service, command):
    operation = command.get('operation')
    if operation not in {'focus','refresh','credits','snapshot'}: raise ValueError('未知运行时操作')
    keys = set(command.get('keys', [])); found = False
    for client in service.clients.values():
        if not client.get('authorized') or not browser_keys(client) & keys: continue
        found = True
        if operation == 'refresh': refresh(service, client)
        elif operation == 'focus':
            if 'runtime_controls_v1' not in client.get('capabilities',[]):
                client['runtime_note']='当前扩展尚未加载运行时激活支持；相关浏览器正常重启后可用。'
                continue
            service.request(client, 'focus_window')
            client['runtime_note'] = '正在请求激活此配置的准确项目窗口。'
        elif operation == 'credits': service.request(client,'get_points',manual_credit=True)
        else: service.request(client,'snapshot')
    if not found: service.failed.emit('所选浏览器未连接，运行时操作未执行。')


def recover_upload(service, client, row):
    info = row['remote'].get('transfer_retries',{}).get('upload',{})
    previous = client.get('f5_recovery',{}).get('attempt')
    if info.get('count',0) < 2 or previous == row['attempt']: return False
    if refresh(service,client,automatic=True):
        client['f5_recovery']['attempt'] = row['attempt']
        return True
    return False
