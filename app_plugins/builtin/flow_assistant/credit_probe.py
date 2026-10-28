"""Credit preflight distinguishes a verified UI waiter from an unresponsive page."""
import time


def waiting(service, client):
    from .ui_mutex import state, valid_phase, wait_limit
    return any(not entry.get('host') and entry['client'] == client['id'] and
        entry['operation'] == 'get_points' and valid_phase(service, entry) and
        time.time() - entry['queued_at'] < wait_limit(service, entry)
        for entry in state(service)['queue'])


def account_wait(service, client, entry, result):
    if entry.get('operation') != 'get_points' or entry.get('credit_wait_accounted') or result not in {'granted','expired','rejected','disabled'}:
        return
    from .browser_reuse import recovery_state
    recovery = recovery_state(service, client)
    now = time.time()
    recovery['since'] += max(0, now - max(entry['queued_at'], recovery['since']))
    entry['credit_wait_accounted'] = True
    if result == 'granted':
        client['credit_check'] = {**client.get('credit_check',{}),'state':'checking', 'at':now,
            'message':'已取得UI锁，正在读取当前账号和积分'}


def recover_wait(service, client, request, message):
    from .ui_permission_recovery import permit_wait
    if message.get('success', True) or not permit_wait(message): return False
    from .browser_reuse import recovery_state
    from .timing import value
    now = time.time(); recovery = recovery_state(service, client)
    if recovery.get('state') == 'stopped': return False
    recovery.update(since=now, credit_count=max(0,recovery.get('credit_count',0)-1))
    client.update(credits_retry_manual=bool(request.get('manual_credit')),
        credits_waiting_ready=True, credits_waiting_account=False,
        credits_retry_at=now+value(service.preferences,'credit_retry_seconds'),
        credit_check={'state':'waiting','at':now,
            'message':'积分检测正在等待前台UI锁，将自动重试；尚未读取账号或积分',
            **({'hold_dispatch':True} if request.get('manual_credit') else {})})
    # Retire only this request's now-invalid waiter, preserving other owners.
    from .ui_mutex import watch
    watch(service)
    return True
