"""Persistent account isolation. Only explicit user reset removes these holds."""
import re
import time

LABELS = {'disabled': '账号已停用／封禁', 'login': '登录已失效',
          'verification': '账号需要验证', 'manual': '用户已停用此账号'}


def identities(client):
    return {prefix + str(client[key]) for prefix, key in
            (('profile:', 'profile'), ('browser:', 'id')) if client.get(key)}


def classify_issue(error, code=''):
    text = str(error or '')[:2000]
    code = str(code or '').upper()
    if code in {'FLOW_ACCOUNT_DISABLED', 'ACCOUNT_DISABLED', 'ACCOUNT_SUSPENDED'} or re.search(
            r'account\s+(?:(?:has\s+been|is|was)\s+)?(?:disabled|suspended|banned)\b|'
            r'账号(?:已被|已经|已|被)?(?:停用|封禁|封停)|帳號(?:已遭|已被|已|被)?(?:停用|封鎖)', text, re.I):
        return 'disabled'
    if code in {'FLOW_ACCOUNT_SIGNED_OUT', 'UNAUTHENTICATED'} or re.search(
            r'please\s+(?:sign|log)\s*in|sign\s*in\s+to\s+continue|authentication\s+required|'
            r'登录(?:已过期|已失效|失效)|需要登录|请先登录|請先登入', text, re.I):
        return 'login'
    if code == 'FLOW_ACCOUNT_VERIFICATION' or re.search(
            r'captcha|unusual\s+activity|verify\s+your\s+identity|需要验证|验证码|異常活動|异常活动', text, re.I):
        return 'verification'
    return ''


def account_hold(client, preferences):
    blocks = preferences.get('account_blocks', {})
    account = str(client.get('account') or '').strip().casefold()
    if account and isinstance(blocks.get(account), dict):
        return blocks[account]
    links = preferences.get('account_block_links', {})
    for identity in sorted(identities(client)):
        key = links.get(identity)
        if key and (not account or key.startswith(('profile:', 'browser:')) or key == account):
            if isinstance(blocks.get(key), dict): return blocks[key]
    return {}


def hold_reason(client, preferences):
    hold = account_hold(client, preferences)
    return (LABELS.get(hold.get('kind'), '账号不可用') + '：' + str(hold.get('reason') or '') +
            '；处理后请手动重置账号状态并重新检测') if hold else ''


def mark_account(service, client, error, code='', *, manual=False):
    kind = 'manual' if manual else classify_issue(error, code)
    keys = identities(client)
    if not kind or not keys: return False
    account = str(client.get('account') or '').strip().casefold()
    key = account or ('profile:' + client['profile'] if client.get('profile') else 'browser:' + client['id'])
    blocks = dict(service.preferences.get('account_blocks', {}))
    links = dict(service.preferences.get('account_block_links', {}))
    reason = str(error or LABELS[kind])[:700]
    old = blocks.get(key, {})
    # A login redirect must not downgrade an already confirmed disabled account.
    severity = {'login': 1, 'verification': 2, 'disabled': 3, 'manual': 4}
    if severity.get(old.get('kind'), 0) > severity.get(kind, 0):
        kind, reason = old['kind'], old['reason']
    entry = {'kind': kind, 'reason': reason, 'at': old.get('at', time.time())}
    blocks[key] = entry
    related = [c for c in service.clients.values() if account and
               str(c.get('account') or '').casefold() == account] + [client]
    for current in related:
        links.update({identity: key for identity in identities(current)})
        current.update(credits_waiting_ready=False, credits_waiting_account=False, credits_retry_at=0)
    if blocks != service.preferences.get('account_blocks', {}) or links != service.preferences.get('account_block_links', {}):
        service.commit_preferences({**service.preferences, 'account_blocks': blocks, 'account_block_links': links})
    return True


def park_unsubmitted_job(service, row):
    with service.store.connection() as db:
        charged = db.execute('SELECT 1 FROM charges WHERE attempt=?', (row['attempt'],)).fetchone()
    evidence = any(row['remote'].get(key) for key in ('bindings', 'upstream_task', 'generation_response',
        'download', 'unverified_download', 'page_recovery', 'cloud_finished'))
    if charged or evidence:
        service.store.update(row['id'], 'uncertain', error='账号不可用；本次存在提交记录，保留原任务核对，不重新生成。')
    else:
        service.store.update(row['id'], 'queued', attempt='', browser='', remote={},
            error='原账号已隔离，等待其他可用账号；未提交生成。')


def reset_accounts(service, keys):
    keys = set(keys)
    if not keys: raise ValueError('请先选择需要手动重置的账号。')
    links = service.preferences.get('account_block_links', {})
    targets = {links[key] for key in keys if key in links}
    selected = [c for c in service.clients.values() if identities(c) & keys]
    for client in selected:
        account = str(client.get('account') or '').strip().casefold()
        if account: targets.add(account)
    affected = [c for c in service.clients.values() if identities(c) & keys or any(
        links.get(identity) in targets for identity in identities(c))]
    related_keys = keys | set().union(*(identities(client) for client in affected)) if affected else keys
    service.commit_preferences({**service.preferences,
        'account_blocks': {key: value for key, value in service.preferences.get('account_blocks', {}).items() if key not in targets},
        'account_block_links': {key: value for key, value in links.items() if value not in targets},
        'browser_blocks': {key: value for key, value in service.preferences.get('browser_blocks', {}).items()
                           if key not in related_keys or not classify_issue(value)}})
    rows = service.store.rows()
    for client in affected:
        if not client.get('authorized'): continue
        client.update(account_reset_at=time.time(), credits_ready_failures=0,
                      credits_waiting_ready=False, credits_waiting_account=False, credits_retry_at=0)
        if client.get('blocked') in {'需要网站验证', 'Flow 网站提示安全限制，后续提交已暂停；保留当前生成页面，不自动刷新。'}:
            client['blocked'] = ''
        # Resetting availability never resets an unresolved paid task.
        busy = client.get('batch_running') or any(row['status'] in
            {'preparing', 'uploading', 'submitting', 'generating', 'downloading', 'uncertain'} and
            (row['browser'] == client['id'] or client.get('account') and
             str(row['remote'].get('account') or '').casefold() == client['account'].casefold()) for row in rows)
        if busy:
            client['credit_check'] = {'state': 'deferred', 'at': time.time(),
                'message': '账号标记已重置；原任务仍待核对，空闲后再检测，不重新生成'}
            continue
        client.update(account='', credits=None, credits_local=False)
        service.request(client, 'get_points')
        client['credit_check']['hold_dispatch'] = True
        service.request(client, 'snapshot')
