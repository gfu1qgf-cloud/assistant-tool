"""Close only after both host records and the extension prove the profile idle."""
import time

from .timing import value as timing_value


def request_close(service, client, params, message, *, grace=True):
    now = time.time()
    account = str(client.get('account') or '').strip().casefold()
    related = [row for row in service.store.rows() if row['browser'] == client['id'] or
               row['remote'].get('profile') == client['profile'] or account and
               str(row['remote'].get('account') or '').strip().casefold() == account]
    if any(row['id'] in service.archiving or row['status'] not in
           {'completed', 'needs_review', 'queued', 'cancelled', 'failed'} for row in related):
        return False
    unconfirmed = [row for row in related if row['status'] in {'queued', 'cancelled', 'failed'}]
    if any(any(row['remote'].get(key) for key in ('bindings', 'upstream_task', 'generation_response',
               'download', 'unverified_download', 'page_recovery')) for row in unconfirmed):
        return False
    with service.store.connection() as db:
        if any(row['attempt'] and db.execute('SELECT 1 FROM charges WHERE attempt=?',
               (row['attempt'],)).fetchone() for row in unconfirmed):
            return False
    if any(hold['profile'] == client['profile'] or hold['browser'] == client['id'] or
           account and str(hold['account']).strip().casefold() == account for hold in service.store.reset_holds()):
        return False
    if any(p['client'] == client['id'] for p in service.pending.values()):
        return False
    state = client.get('credit_close') or {}
    if (state.get('state') in {'checking', 'closing', 'closed'} or
            state.get('count', 0) >= timing_value(service.preferences, 'credit_close_attempts') or
            state.get('next_at', 0) > now):
        return False
    check = client.get('credit_check') or {}
    if grace and now - check.get('at', 0) < timing_value(service.preferences, 'credit_close_grace_seconds'):
        return False
    released = service.store.released_attempts(client['id'])
    if released:
        if 'close_released_batches_v1' not in client.get('capabilities', []):
            from .setup_support import EXTENSION_VERSION
            client['credit_close'] = {**state, 'state': 'needs_update',
                'message': f'旧任务已删除／重置；需要加载扩展 {EXTENSION_VERSION} 后自动释放其窗口占位'}
            return False
        params = {**params, 'released_batches': released}
    client['credit_close'] = {'state': 'checking', 'count': state.get('count', 0) + 1,
        'next_at': now + timing_value(service.preferences, 'credit_close_timeout_seconds'),
        'reason': params.get('reason', 'low_credit'), 'message': message}
    if service.request(client, 'close_exhausted', {'idle': True, 'profile': client['profile'],
            'url': client.get('saved_project'), **params}):
        return True
    client['credit_close'].update(state='failed', message='关闭请求未送达，保留当前窗口')
    return False
