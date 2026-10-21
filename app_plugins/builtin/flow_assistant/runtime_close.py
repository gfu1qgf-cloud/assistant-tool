"""Explicit user closure; never a scheduler action or a physical-window gate."""
import time
import uuid

from .setup_support import project_identity
from .store import TRANSIENT

CAPABILITY = 'manual_browser_close_v1'
PAUSED = '手动关闭浏览器：已暂停此配置自动调度；需要使用时请点“修正后恢复选中浏览器”。'


def request_close(service, client):
    if not client.get('authorized') or CAPABILITY not in client.get('capabilities', []):
        client['runtime_note'] = '关闭未执行：此操作需要已连接的桥接扩展 1.1.34。'
        return False
    profile, project = client.get('profile'), client.get('saved_project')
    if (not profile or not project_identity(project) or project_identity(project) !=
            project_identity(service.preferences.get('profile_projects', {}).get(profile))):
        client['runtime_note'] = '关闭未执行：无法确认所选配置的原项目。'
        return False
    account = str(client.get('account') or '').strip().casefold()
    related = {c['id'] for c in service.clients.values() if c.get('id') and c.get('profile') == profile}
    busy = any(row['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} and
        (row['browser'] in related or row['remote'].get('profile') == profile or account and
         str(row['remote'].get('account') or '').strip().casefold() == account)
        for row in service.store.rows())
    if (busy or client.get('native_busy') or client.get('batch_running') or
            client.get('reset_wait_until', 0) > time.time() or
            any(p['client'] in related for p in service.pending.values()) or
            client.get('runtime_close', {}).get('state') in {'checking', 'closing', 'closed'}):
        client['runtime_note'] = '关闭未执行：仍有网页操作、生成、下载或待核对结果，请结束后再关闭。'
        return False
    # Reserve this profile before sending, so a tick cannot assign fresh work
    # while the extension is checking downloads and other Flow tabs.
    blocks = dict(service.preferences.get('browser_blocks', {}))
    # Profile-only pause remains fully resettable after its socket disappears;
    # an offline table row cannot know the old stable browser ID.
    added = ['profile:' + profile] if not blocks.get('profile:' + profile) else []
    blocks.update({key: PAUSED for key in added})
    service.commit_preferences({**service.preferences, 'browser_blocks': blocks})
    client['runtime_close'] = {'state': 'checking', 'added_blocks': added}
    client['runtime_note'] = '正在核对空闲及下载；将关闭此 Chrome 配置的所有窗口。'
    sent = service.request(client, 'close_browser', {'idle': True, 'profile': profile,
        'url': project, 'reason': 'manual_runtime',
        'released_batches': service.store.released_attempts(client['id'])}, manual_close=True)
    if not sent:
        response(service, client, {'success': False, 'error': '连接已断开，未发送关闭命令'})
    return bool(sent)


def response(service, client, message):
    data = message.get('data') or {}
    state = client.setdefault('runtime_close', {})
    if message.get('success') and data.get('closed'):
        state['state'] = 'closed'
        client['runtime_note'] = PAUSED
        client['credit_close'] = {'state': 'closed', 'message': PAUSED}
        from .browser_slots import closed
        closed(service, client.get('profile'))
    elif message.get('success') and data.get('closing'):
        state['state'] = 'closing'
        client['runtime_note'] = '空闲及下载已核对，正在关闭此配置的浏览器窗口。'
        update_batch(service, client)
        return False  # Keep the request until an actual close acknowledgement.
    else:
        state['state'] = 'failed'
        blocks = dict(service.preferences.get('browser_blocks', {}))
        for key in state.get('added_blocks', []):
            if blocks.get(key) == PAUSED: blocks.pop(key)
        service.commit_preferences({**service.preferences, 'browser_blocks': blocks})
        client['runtime_note'] = '关闭未完成：' + str(message.get('error') or '扩展未确认关闭')[:500]
    update_batch(service, client)
    return True


def timeout(client, service=None):
    client.setdefault('runtime_close', {})['state'] = 'unconfirmed'
    client['runtime_note'] = '关闭响应超时，未确认窗口退出；此配置保留暂停，可重新关闭或手动恢复。'
    if service is not None: update_batch(service, client)


def low_balance(client, preferences):
    """Unknown/expired observations never count as exhausted accounts."""
    from .credit_policy import balance_ready
    from .credit_cooldown import credit_hold, MIN_CREDITS
    hold = credit_hold(client, preferences)
    points = hold.get('balance') if hold else client.get('credits')
    return (type(points) in (int, float) and 0 <= points < MIN_CREDITS and
            (bool(hold) or balance_ready(client, preferences=preferences)))


def close_all_low(service):
    previous = getattr(service, 'runtime_close_batch', {})
    if any(p['action'] == 'close_browser' and p['client'] in previous.get('entries', {})
           for p in service.pending.values()): return
    batch = {'id': uuid.uuid4().hex, 'entries': {}}
    service.runtime_close_batch = batch
    profiles = set()
    for client in list(service.clients.values()):
        if (not client.get('authorized') or not low_balance(client, service.preferences) or
                client.get('profile') in profiles or client.get('runtime_close', {}).get('state') == 'closed'): continue
        profiles.add(client.get('profile'))
        sent = request_close(service, client)
        batch['entries'][client['id']] = {'profile': str(client.get('profile') or client['id']),
            'state': 'checking' if sent else 'skipped', 'message': client.get('runtime_note', '')}


def update_batch(service, client):
    entry = getattr(service, 'runtime_close_batch', {}).get('entries', {}).get(client.get('id'))
    if entry is not None:
        entry.update(state=client.get('runtime_close', {}).get('state', 'unconfirmed'),
                     message=client.get('runtime_note', ''))


def batch_summary(service):
    batch = getattr(service, 'runtime_close_batch', {})
    if not batch: return {}
    entries = list(batch.get('entries', {}).values())
    count = lambda states: sum(e['state'] in states for e in entries)
    closed = count({'closed'}); waiting = count({'checking','closing'})
    skipped = count({'skipped'}); failed = count({'failed','unconfirmed'})
    message = (f'批量关闭低于10分浏览器：已关闭 {closed}；核对／关闭中 {waiting}；跳过 {skipped}；失败／未确认 {failed}。'
               if entries else '没有发现已连接且积分有效、低于10分的浏览器；未知或过期余额不会被关闭。')
    detail = '\n'.join(e['profile']+'：'+e['message'] for e in entries)
    return {'id': batch['id'], 'message': message, 'detail': detail, 'closed':closed,
            'waiting':waiting,'skipped':skipped,'failed':failed}
