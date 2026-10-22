"""Retry only a terminal failure tied to this exact generated media."""
import time

from .media_identity import generated_bindings
from .policy import block_for, capable, estimated_cost
from .timing import value, delay


def confirmed_failure(service, client, message):
    row = next((row for row in service.store.rows() if row['attempt'] == message.get('batch_id')
                and row['browser'] == client['id']), None)
    if (not row or row['status'] not in {'submitting', 'generating', 'uncertain', 'download_failed'}
            or message.get('failure_source') not in {'server_status', 'refund_card'}
            or message.get('download_path') != service.download_name(row)
            or row['remote'].get('download') or row['remote'].get('cloud_finished')):
        return False
    bound = [binding for binding in generated_bindings(row['remote']) if
        binding['taskId'] == message.get('taskId') == row['remote'].get('upstream_task') and
        binding['submissionId'] == message.get('submissionId') and
        binding['mediaIds'] == [message.get('mediaId')] and not binding['readyIds']]
    if len(bound) != 1: return False
    text = str(message.get('error') or 'Flow 已确认视频生成失败')[:500]
    count = row['payload'].get('generation_recovery', {}).get('count', 0)
    limit = value(service.preferences, 'generation_retry_attempts')
    permanent = any(word in text.casefold() for word in
        ('captcha', 'verify', 'policy', 'safety', 'prohibited', 'disabled', '违规', '違規', '政策', '验证', '驗證', '封禁', '安全限制'))
    retry = {'state': 'waiting' if count < limit and not permanent else 'stopped', 'failed_at': time.time(),
        'failed_attempt': row['attempt'], 'submission': message['submissionId'], 'media': message['mediaId'],
        'source': message['failure_source'], 'next_at': time.time() + delay(service.preferences, 'generation_retry_delays', count)}
    detail = f'；等待后自动重试（已重试 {count}/{limit}）' if retry['state'] == 'waiting' else '；自动重试已停止'
    service.store.update(row['id'], 'failed', remote={**row['remote'], 'generation_retry': retry}, error=text + detail)
    return True


def watch(service):
    if service.safety_paused: return False
    changed = False
    for row in service.store.rows():
        info = row['remote'].get('generation_retry', {})
        if (row['status'] != 'failed' or info.get('state') != 'waiting'
                or info.get('failed_attempt') != row['attempt'] or info.get('next_at', 0) > time.time()
                or service.paused and row['id'] not in service.released): continue
        previous = row['payload'].get('generation_recovery', {})
        count = previous.get('count', 0)
        if count >= value(service.preferences, 'generation_retry_attempts'): continue
        history = (previous.get('history', []) + [{key: info[key] for key in
                   ('failed_at', 'failed_attempt', 'submission', 'media', 'source')}])[-10:]
        payload = {**row['payload'], 'generation_recovery': {'count': count + 1, 'history': history}}
        remote = {'generation_retry': {**info, 'state': 'ready', 'credit_after': time.time(), 'credit_probes': {}}}
        changed |= service.store.requeue_failed_generation(row['id'], row['attempt'], payload, remote)
    return changed


def fresh_clients(service, row, clients):
    """A new paid retry needs an actual post-failure balance read, not a refund guess."""
    info = row['remote'].get('generation_retry', {})
    if info.get('state') != 'ready': return clients
    from .connection_recovery import occupied
    from .routing import dispatch_rank
    now = time.time(); probes = dict(info.get('credit_probes', {})); changed = False
    for client in clients:
        if (not client.get('page_ready') or occupied(service, client) or block_for(client, service.preferences)
                or dispatch_rank(client, row['payload'], service.preferences) is None
                or client.get('credits_at', 0) >= info['credit_after'] and
                    capable(client, row['payload'], service.preferences, estimated_cost(row['payload'], service.observed_costs))): continue
        if any(request['client'] == client['id'] and request['action'] == 'get_points' for request in service.pending.values()):
            continue
        previous = probes.get(client['id'], {})
        if (previous.get('count', 0) >= value(service.preferences, 'credit_read_attempts')
                or previous.get('next_at', 0) > now): continue
        if service.request(client, 'get_points'):
            probes[client['id']] = {'count': previous.get('count', 0) + 1,
                'next_at': now + value(service.preferences, 'generation_retry_credit_probe_seconds')}
            changed = True
    if changed:
        service.store.update(row['id'], row['status'], remote={**row['remote'], 'generation_retry': {**info, 'credit_probes': probes}},
            error='已确认原生成失败，重试前正在重新读取实际账号积分；不预先增加退款余额')
    return [client for client in clients if client.get('credits_at', 0) >= info['credit_after']]
