"""Single-use, two-minute localhost setup handoff; no long-lived key in URLs."""
import hmac
import re
import time

from .setup_support import EXTENSION_ID, EXTENSION_VERSION, project_identity, project_block, compatible_extension
from .store import TRANSIENT


def confirm_saved_connection(service, client):
    """A valid saved session need not re-pair just to retire an unused ticket.

    Only the authenticated socket's saved identity AND its real page response
    may confirm the exact requested project. Never steal an in-flight claim or
    an explicit repair, and never rewrite credentials/reconnect a busy browser.
    """
    if (not client.get('authorized') or not client.get('setup_pending')
            or not compatible_extension(client.get('extension_version'))
            or not (client.get('page_loaded') or client.get('page_ready'))):
        return False
    profile = client.get('profile')
    setup = service.setup_requests.get(profile)
    if not setup or setup.get('mode') == 'reload_only' or not re.fullmatch(r'[a-f0-9]{32}', str(setup.get('id', ''))):
        return False
    live = setup.get('expires', 0) > time.time()
    if live and (setup.get('claimed') or setup.get('force_pair')):
        return False
    expected = project_identity(setup.get('project'))
    if (not expected or project_identity(client.get('saved_project')) != expected
            or project_identity(client.get('project')) != expected
            or project_identity(service.preferences.get('profile_projects', {}).get(profile)) != expected):
        return False
    service.setup_requests.pop(profile, None)
    client['setup_pending'] = False
    client['setup_blocked'] = project_block(client, service.preferences)
    return True


def claim_setup(service, client, message):
    ticket = str(message.get('ticket') or '')
    match = next(((profile, value) for profile, value in service.setup_requests.items()
        if re.fullmatch(r'[a-f0-9]{32}', ticket) and hmac.compare_digest(str(value.get('id', '')), ticket)), None)
    if not match or match[1].get('claimed') or match[1].get('expires', 0) <= time.time():
        service.send(client, {'type': 'setup_error', 'error': '连接步骤已过期或已使用，请回软件点一次“打开浏览器并连接”。'})
        return False
    profile, setup = match
    if setup.get('mode') == 'reload_only':
        service.send(client, {'type': 'setup_error', 'error': '这是原配置的扩展更新入口，无需重新配对；请等待后台重连。'})
        return False
    ids = {c.get('id') for c in service.clients.values() if c.get('profile') == profile}
    if any(row['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} and
           (row['browser'] in ids or row['remote'].get('profile') == profile) for row in service.store.rows()):
        service.send(client, {'type': 'setup_error', 'error': '此浏览器仍有未完成任务，暂不更换连接。'})
        return False
    setup['claimed'] = True
    service.send(client, {'type': 'setup_bundle', 'pairing': {
        'token': service.pair['token'], 'port': service.pair['port'], 'profile': profile,
        'project': setup['project'], 'extension_id': EXTENSION_ID, 'setup_id': ticket}})
    return True


def check_reload(service, client, message):
    """Only a recent explicit connection request may authorize an idle reload."""
    ticket = str(message.get('ticket') or '')
    match = next(((profile, setup) for profile, setup in service.setup_requests.items()
        if re.fullmatch(r'[a-f0-9]{32}', ticket) and hmac.compare_digest(str(setup.get('id', '')), ticket)), None)
    if not match or match[1].get('claimed') or match[1].get('expires', 0) <= time.time():
        service.send(client, {'type': 'setup_reload_denied', 'error': '自动更新入口已过期，请在主程序点击连接。'})
        return False
    profile, setup = match
    ids = {c.get('id') for c in service.clients.values() if c.get('profile') == profile}
    blocked_states = TRANSIENT | {'archiving', 'uncertain'}
    if any((row['status'] in blocked_states or row['status'] == 'download_failed'
            and (setup.get('mode') != 'reload_only' or not row['remote'].get('cloud_finished'))) and
           (row['browser'] in ids or row['remote'].get('profile') == profile) for row in service.store.rows()):
        service.send(client, {'type': 'setup_reload_denied', 'error': '原浏览器仍有未完成任务，本次不重载扩展。'})
        return False
    service.send(client, {'type': 'setup_reload_allowed', 'profile': profile, 'project': setup['project'], 'ticket': ticket,
                          'mode': setup.get('mode', 'pair')})
    return True


def lookup_setup(service, client, message):
    """A configured extension can recover its own entry, never another profile's."""
    profile = str(message.get('profile') or '')
    setup = service.setup_requests.get(profile, {})
    if (not hmac.compare_digest(str(message.get('token') or ''), service.pair['token']) or
            not setup or setup.get('claimed') or setup.get('expires', 0) <= time.time()):
        service.send(client, {'type': 'setup_waiting'})
        return False
    service.send(client, {'type': 'setup_ticket', 'ticket': setup['id'], 'port': service.pair['port'],
                         'profile': profile, 'project': setup['project']})
    return True
