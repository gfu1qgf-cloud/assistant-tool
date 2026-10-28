"""A new program session rechecks browsers without erasing tasks or user settings."""
import math
import time


def reset_preferences(preferences, now=None):
    from .credit_refresh import deadline
    from .runtime_close import PAUSED
    now = time.time() if now is None else now
    result = dict(preferences)
    # These are observations/retry budgets for the previous process, not
    # user configuration, material identity, or a paid generation receipt.
    for key in ('browser_launch_failures','browser_probe_retries','environment_recoveries',
                'bridge_recoveries','browser_lifecycle','browser_slots','parameter_blocks'):
        result[key] = {}
    result['browser_blocks'] = {key:reason for key,reason in preferences.get('browser_blocks',{}).items()
        if reason == PAUSED or str(reason).startswith('用户手动标记账号不可用')}
    manual = {key:entry for key,entry in preferences.get('account_blocks',{}).items() if entry.get('kind') == 'manual'}
    result['account_blocks'] = manual
    result['account_block_links'] = {key:target for key,target in preferences.get('account_block_links',{}).items() if target in manual}
    future = lambda at: type(at) in (int,float) and math.isfinite(at) and at > now
    cooldowns = {}
    for account,entry in preferences.get('credit_cooldowns',{}).items():
        refresh = entry.get('refresh_at') or deadline({'account':account},preferences)
        if future(refresh):cooldowns[account] = {**entry,'until':refresh,'refresh_at':refresh}
    result['credit_cooldowns'] = cooldowns
    result['credit_cooldown_links'] = {key:target for key,target in preferences.get('credit_cooldown_links',{}).items() if target in cooldowns}
    exhausted = {}
    for profile,entry in preferences.get('exhausted_browser_disconnects',{}).items():
        refresh = entry.get('refresh_at') or deadline({'account':entry.get('account')},preferences)
        if future(refresh):exhausted[profile] = {**entry,'refresh_at':refresh}
    result['exhausted_browser_disconnects'] = exhausted
    # Keep balances for display, debit history and expected account matching.
    # They are not current-login/spending authority in the new session.
    result['credit_balances'] = {account:entry if account in cooldowns else {**entry,'recheck_after':now}
        for account,entry in preferences.get('credit_balances',{}).items()}
    return {key:value for key,value in result.items() if key in preferences or value != {}}


def reset_runtime(service):
    """Explicit reset rechecks idle pages, deferring active/paid work intact."""
    import uuid
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold
    from .runtime_close import PAUSED
    from .connection_status import browser_keys
    from .store import TRANSIENT
    from .timing import value
    service.commit_preferences(reset_preferences(service.preferences))
    service.reuse_recoveries = {}; service.connection_recoveries = {}; service.small_balance_probes = {}
    now=time.time(); rows=service.store.rows()
    protected={c.get('profile') for c in service.clients.values() if c.get('authorized')}
    protected.update(row['remote'].get('profile') for row in rows if row['status'] in TRANSIENT|{'uncertain','download_failed','archiving'})
    service.launching={profile:at for profile,at in service.launching.items() if now-at<value(service.preferences,'browser_startup_seconds')}
    protected.update(service.launching)
    service.launched.intersection_update(protected);service.launch_attempts.intersection_update(protected)
    reset=manual=waiting=0
    for client in service.clients.values():
        if not client.get('authorized'):continue
        if account_hold(client,service.preferences).get('kind')=='manual' or any(
                service.preferences.get('browser_blocks',{}).get(key) for key in browser_keys(client)):
            manual+=1;continue
        if credit_hold(client,service.preferences):waiting+=1;continue
        reset+=1
        closing=(client.get('runtime_close') or {}).get('state') in {'checking','closing'}
        for key in ('reuse_recovery','connection_recovery','prepare_wait','f5_recovery','setup_blocked',
                    'page_error','runtime_note','credit_refresh_probe','credit_close','runtime_close','preparation_block_recovered'):
            if closing and key=='runtime_close':continue
            client.pop(key,None)
        client.update(blocked='',credits_ready_failures=0,credits_retry_at=0,
            credits_waiting_account=False,credits_waiting_ready=False,browser_recheck_pending=True)
        if (client.get('credit_check') or {}).get('state') not in {'checking','waiting'}:
            client['credit_check']={'state':'deferred','at':now,'message':'异常状态已重置，空闲后重新核对账号和积分'}
        if any(p['client']==client['id'] and p['action']=='get_points' for p in service.pending.values()):
            client['reuse_recovery']={'since':now,'credit_count':0}
    from .ui_mutex import watch as watch_mutex
    watch_mutex(service)
    watch(service)
    service.browser_error_reset={'id':uuid.uuid4().hex,'reset':reset,'manual':manual,'waiting':waiting,
        'deferred':sum(bool(c.get('browser_recheck_pending')) for c in service.clients.values()),
        'message':f'所有浏览器异常标记已重置，{reset}个在线浏览器将重新检测；保留用户标记及未到期积分限制。'}


def watch(service):
    from .connection_recovery import occupied
    from .account_policy import account_hold
    from .credit_cooldown import credit_hold
    changed=False
    for client in service.clients.values():
        if not client.get('browser_recheck_pending') or not client.get('authorized'):continue
        if account_hold(client,service.preferences) or credit_hold(client,service.preferences):
            client.pop('browser_recheck_pending',None);continue
        if occupied(service,client) or client.get('native_busy'):continue
        if any(p['client']==client['id'] for p in service.pending.values()):continue
        service.request(client,'snapshot')
        if client.get('page_loaded') or client.get('page_ready'):
            if service.request(client,'get_points',manual_credit=True):
                client.pop('browser_recheck_pending',None);changed=True
    return changed
