"""Release audited settings failures that occur before the Generate click."""
import time

# Exact errors thrown by applyDuration/waitForSettings inside the initial
# syncPageVideoSettings call, before reference attachment, prompt entry and
# generation submission. Never infer this from arbitrary "failed" messages.
PRE_SUBMIT_ERRORS = {
    '提交失败: 时长设置面板尚未稳定，自动等待后重新核对；未提交生成。',
    '提交失败: 设置面板尚未稳定，自动等待后重新核对；未提交生成。',
    '提交失败: 设置面板正在重建，自动等待后重新核对；未提交生成。',
}
EVIDENCE = ('bindings', 'upstream_task', 'generation_response', 'download',
            'unverified_download', 'page_recovery', 'cloud_finished')


def confirmed_unsubmitted(row, message=None):
    return (row['status'] in {'submitting','generating','uncertain'} and bool(row['attempt']) and
        str(row['error'] if message is None else message).strip() in PRE_SUBMIT_ERRORS and
        not any(row['remote'].get(key) for key in EVIDENCE))


def release(service, row, message=None):
    if not confirmed_unsubmitted(row, message): return False
    account = str(row['remote'].get('account') or '').strip().casefold()
    if account:
        balances = dict(service.preferences.get('credit_balances', {}))
        balances[account] = {**balances.get(account, {}), 'recheck_after': time.time()}
        service.commit_preferences({**service.preferences, 'credit_balances': balances})
    if not service.store.release_unsubmitted_failure(row['id'], row['attempt'], message): return False
    for client in service.clients.values():
        if (client.get('id') == row['browser'] or row['remote'].get('profile') and client.get('profile') == row['remote']['profile'] or
                account and str(client.get('account') or '').strip().casefold() == account):
            client.pop('prepare_snapshot', None)
            client.pop('reuse_recovery', None)
            client['runtime_note'] = '已确认提交前设置核对失败，已释放账号；重新读取实际积分后继续接单。'
    return True


def watch(service):
    changed = False
    for row in service.store.rows():
        if row['status'] == 'uncertain' and confirmed_unsubmitted(row): changed |= release(service, row)
    return changed
