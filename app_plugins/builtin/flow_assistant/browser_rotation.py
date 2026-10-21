"""A skipped rich account must yield a physical window slot to other accounts."""
import time

from .account_policy import account_hold
from .browser_slots import occupied_profiles, limit
from .connection_status import configured_profiles
from .high_credit import allowed, is_high, record
from .image_affinity import cached_profile, allows_image
from .balance_dispatch import can_fund
from .policy import block_for
from .routing import dispatch_rank
from .idle_close import request_close


def watch(service, queued):
    if not queued or service.safety_paused:
        return False
    slots = occupied_profiles(service)
    if len(slots) < limit(service):
        return False
    clients = [c for c in service.clients.values() if c.get('authorized')]
    if any((c.get('credit_close') or {}).get('state') in {'checking', 'closing'} for c in clients):
        return False  # One confirmed vacancy is enough; never close a whole pool at once.
    preferences, rows = service.preferences, service.store.rows()
    online = {c.get('profile') for c in clients}
    protected = [row for row in rows if row['status'] in {'preparing', 'uploading', 'submitting',
        'generating', 'downloading', 'archiving', 'uncertain', 'download_failed'}]
    holds = service.store.reset_holds()
    held_profiles = {row['remote'].get('profile') for row in protected} | {hold['profile'] for hold in holds}
    held_accounts = {str(row['remote'].get('account') or '').strip().casefold() for row in protected} | {
        str(hold['account'] or '').strip().casefold() for hold in holds}
    # Check alternatives without can_start(): the purpose here is to free
    # precisely the slot that currently makes can_start() return False.
    alternatives = []
    for profile in configured_profiles(preferences):
        if (profile in slots or profile in online or profile in held_profiles or profile in service.launched or
                profile in preferences.get('browser_launch_failures', {}) or
                profile in getattr(service, 'launch_attempts', set())):
            continue
        candidate = cached_profile(profile, preferences)
        if candidate.get('account') and str(candidate['account']).strip().casefold() in held_accounts:
            continue
        if not allowed(candidate, preferences) or block_for(candidate, preferences):
            continue
        if any(dispatch_rank(candidate, row['payload'], preferences) is not None and
               can_fund(candidate, row['payload'], preferences, service.observed_costs) and
               allows_image(candidate, row['payload'], rows, preferences) for row in queued):
            alternatives.append(candidate)
    if not alternatives:
        return False
    for client in clients:
        if (not is_high(client, preferences) or allowed(client, preferences) or
                record(client, preferences).get('decision') == 'asking' or
                not client.get('account') or not client.get('profile') or not client.get('credits_local') or
                client.get('identity_unconfirmed') or client.get('setup_pending') or
                client.get('reset_wait_until', 0) > time.time() or account_hold(client, preferences)):
            continue
        if 'close_idle_backup_v1' not in client.get('capabilities', []):
            client['backup_rotation_note'] = '此备用账号需要加载新版扩展后才能自动核对关闭，账号保持跳过'
            continue
        client.pop('backup_rotation_note', None)
        if request_close(service, client, {'reason': 'high_credit_backup', 'account': client['account']},
                '千分账号已跳过；正在核对任务和下载，关闭空闲窗口后自动换账号', grace=False):
            return True
    return False
