"""Pin each account to one image and drain the selected parallel account pool."""
import time

from .account_policy import account_hold
from .credit_cache import restore_balance
from .credit_cooldown import credit_hold, MIN_CREDITS
from .credit_policy import balance_ready, credit_order
from .policy import block_for, estimated_cost
from .timing import value as timing_value
from .routing import dispatch_rank


def image_key(job):
    return job.get('image_identity', {}).get('sha256', '')


PENDING = {'queued', 'preparing', 'uploading', 'submitting', 'generating', 'downloading',
           'uncertain', 'download_failed', 'archiving'}


def account_key(value):
    return str(value or '').strip().casefold()


def account_images(preferences):
    if 'account_images' in preferences:
        return {account_key(key): value for key, value in preferences['account_images'].items()
                if key and isinstance(value, dict)}
    # The first normal assignment persists old single-image owners as accounts.
    entries = {}
    for image, entry in preferences.get('image_affinities', {}).items():
        if not isinstance(entry, dict) or not entry.get('account'): continue
        key = account_key(entry['account'])
        if key not in entries or entry.get('at', 0) >= entries[key].get('at', 0):
            entries[key] = {**entry, 'image': image, 'active': True}
    return entries


def account_entry(client, preferences):
    entries = account_images(preferences)
    account = account_key(client.get('account'))
    if account: return entries.get(account, {})
    return next((entry for entry in entries.values() if client.get('profile') and
                 entry.get('profile') == client['profile']), {})


def retained(client, preferences):
    entry = account_entry(client, preferences)
    return bool(entry and entry.get('active', True) and not account_hold(client, preferences)
        and not credit_hold(client, preferences) and not (
            balance_ready(client, preferences=preferences) and client['credits'] < MIN_CREDITS))


def dispatch_order(client, preferences):
    from .high_credit import is_high
    tier, credits = credit_order(client)
    return (1 if is_high(client, preferences) else tier, 0 if retained(client, preferences) else 1, credits)


def allows_image(client, job, rows, preferences, pending=None):
    entry = account_entry(client, preferences)
    if not entry or not entry.get('active', True) or not entry.get('image') or entry['image'] == image_key(job):
        return True
    remaining = pending.get(entry['image'], []) if pending is not None else [
        row for row in rows if row['status'] in PENDING and image_key(row.get('payload', {})) == entry['image']]
    if not remaining: return True
    # Image ownership protects this account's in-flight work, not every task
    # using that image on other accounts or unassigned future FIFO rows.
    account=account_key(client.get('account') or entry.get('account'))
    owned=[row for row in remaining if (client.get('id') and row.get('browser')==client['id'] or
        client.get('profile') and row.get('remote',{}).get('profile')==client['profile'] or
        account and account_key(row.get('remote',{}).get('account'))==account)]
    protected=[row for row in owned if row['status']!='queued' or client.get('id') and
        row.get('remote',{}).get('credit_pack',{}).get('browser')==client['id']]
    if not protected:return True
    # Wait for the image's last concurrent result; release only when this
    # account cannot perform any remaining queued variant's cost/parameters.
    queued = [row for row in owned if row['status'] == 'queued']
    if queued and balance_ready(client, preferences=preferences) and not any(
            dispatch_rank(client, row['payload'], preferences) is not None and (
                estimated_cost(row['payload'], {}) is None or
                client['credits'] >= estimated_cost(row['payload'], {})) for row in queued):
        return True
    return False


def retained_pool(clients, rows, preferences):
    """Healthy selected accounts reserve slots while their image finishes."""
    from .high_credit import allowed
    pool = set()
    jobs = [row.get('payload', {}) for row in rows if row['status'] in PENDING]
    held_rows = {}
    for row in rows:
        held_rows.setdefault(account_key(row['remote'].get('account')), []).append(row)
    for client in clients:
        account = account_key(client.get('account'))
        if (not account or not allowed(client, preferences) or not retained(client, preferences) or block_for(client, preferences) or client.get('blocked')
                or client.get('setup_blocked') or client.get('reset_wait_until', 0) > time.time()):
            continue
        held = held_rows.get(account, [])
        if any(row['status'] == 'uncertain' or row['status'] == 'download_failed' and
               row['remote'].get('transfer_retries', {}).get('download', {}).get('stopped') for row in held):
            continue
        fresh = balance_ready(client, preferences=preferences)
        if not client.get('page_ready') or not fresh:
            check = client.get('credit_check') or {}
            reading = client.get('page_ready') and check.get('state') == 'checking' and (
                time.time() - check.get('at', 0) < timing_value(preferences, 'request_timeout_seconds'))
            waiting_at = client.get('page_unready_at') or client.get('connected', 0)
            if not reading and time.time() - waiting_at >= timing_value(preferences, 'browser_startup_seconds'):
                continue
        if any(dispatch_rank(client, job, preferences) is not None and (not fresh or
                estimated_cost(job, {}) is None or client['credits'] >= estimated_cost(job, {})) for job in jobs):
            pool.add(account)
    return pool


def affinity_context(clients, rows, preferences, queue_ids=None):
    images = {}
    if queue_ids is not None:
        rows = [row for row in rows if row['status'] != 'queued' or row['id'] in queue_ids]
    for row in rows:
        if row['status'] in PENDING:
            images.setdefault(image_key(row.get('payload', {})), []).append(row)
    return {'images': images, 'pool': retained_pool(clients, rows, preferences)}


def cached_profile(profile, preferences):
    client = {'id': '', 'profile': profile, 'saved_project': preferences.get('profile_projects', {}).get(profile)}
    restore_balance(client, preferences)
    return client


def retire_exhausted(service):
    entries = account_images(service.preferences)
    changed = False
    for client in service.clients.values():
        key = account_key(client.get('account'))
        entry = entries.get(key)
        if entry and entry.get('active', True) and (credit_hold(client, service.preferences) or
                balance_ready(client, preferences=service.preferences) and client['credits'] < MIN_CREDITS):
            entries[key] = {**entry, 'active': False}
            changed = True
    if changed:
        service.commit_preferences({**service.preferences, 'account_images': entries, 'image_affinities': {}})


def remember_owner(service, row, client):
    image = image_key(row['payload'])
    if not image: return
    entries = account_images(service.preferences)
    account = account_key(client['account'])
    entries[account] = {'account': account, 'image': image, 'active': True,
        'profile': client.get('profile', ''), 'browser': client['id'], 'at': time.time()}
    service.commit_preferences({**service.preferences, 'account_images': entries, 'image_affinities': {}})
