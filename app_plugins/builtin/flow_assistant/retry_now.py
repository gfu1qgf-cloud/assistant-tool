"""Wake selected automatic recovery without resetting its limits or identity."""
import time

from .transfer_policy import retry_info


def retry_kind(row):
    remote = row.get('remote', {})
    if row['status'] == 'queued' and remote.get('prepare_retry', {}).get('next_at'):
        return 'prepare'
    for stage, status in (('upload', 'uploading'), ('download', 'download_failed')):
        info = retry_info(row, stage)
        if row['status'] == status and info.get('next_at') and not info.get('stopped') and not info.get('active'):
            return stage
    info = remote.get('generation_retry', {})
    if (row['status'] == 'failed' and info.get('state') == 'waiting' and
            info.get('failed_attempt') == row['attempt']): return 'generation'
    info = remote.get('page_recovery', {})
    if row['status'] == 'uncertain' and info and not info.get('done'): return 'probe'
    return ''


def request_now(service, command):
    if service.safety_paused:
        service.failed.emit('网站安全暂停仍有效；请处理原提示后继续，立即重试不会解除安全暂停。')
        return
    accepted = 0
    for identifier in dict.fromkeys(command.get('ids', [])):
        row = service.store.get(identifier)
        expected = command.get('versions', {}).get(identifier)
        if not row or expected != {key: row[key] for key in ('attempt', 'browser', 'status')}: continue
        kind = retry_kind(row)
        if not kind or any(p['job'] == identifier and p['attempt'] == row['attempt'] for p in service.pending.values()): continue
        now = time.time(); remote = dict(row['remote'])
        if kind in {'upload', 'download'}:
            info = {**retry_info(row, kind), 'next_at': now}
            if kind == 'download': info['probe_at'] = 0
            service._save_transfer(row, kind, info)
        else:
            key = {'prepare': 'prepare_retry', 'generation': 'generation_retry', 'probe': 'page_recovery'}[kind]
            info = {**remote[key], 'next_at': now}; remote[key] = info
            service.store.update(identifier, row['status'], remote=remote, error=row['error'])
            if kind == 'prepare':
                client = next((c for c in service.clients.values() if c.get('authorized') and c['id'] == info.get('browser')), None)
                if client:
                    if (client.get('prepare_wait') or {}).get('browser') == client['id']:
                        client['prepare_wait'] = {**client['prepare_wait'], 'next_at': now}
                    if not any(p['client'] == client['id'] and p['action'] == 'snapshot' for p in service.pending.values()):
                        service.request(client, 'snapshot')
        service.released.add(identifier)
        accepted += 1
    if accepted:
        service._tick()
    else:
        service.failed.emit('选中任务没有可提前的自动重试，或状态已变化／正在处理；未重复提交。')
