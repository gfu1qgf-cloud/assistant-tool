"""Keep one source image on one account until it can no longer continue."""
import time

from .account_policy import account_hold
from .credit_cache import restore_balance
from .credit_cooldown import credit_hold, MIN_CREDITS
from .credit_policy import balance_ready
from .policy import block_for
from .timing import value as timing_value
from .routing import dispatch_rank


def image_key(job):
    return job.get('image_identity', {}).get('sha256', '')


def active_image(job, rows):
    key = image_key(job)
    return bool(key and any(row['status'] in {'preparing','uploading','submitting','generating','downloading',
        'uncertain','download_failed','archiving'} and image_key(row.get('payload', {})) == key for row in rows))


def preferred_owner(job, clients, preferences, cost=None):
    entry = preferences.get('image_affinities', {}).get(image_key(job), {})
    if not entry.get('account'): return None
    account = str(entry['account']).strip().casefold()
    owner = next((c for c in clients if str(c.get('account') or '').strip().casefold() == account and
        (not entry.get('profile') or c.get('profile') == entry['profile'])), None)
    if owner is None:
        owner = {'profile':entry.get('profile'), 'account':account,
            'saved_project':preferences.get('profile_projects', {}).get(entry.get('profile'))}
        restore_balance(owner, preferences)
    if account_hold(owner, preferences) or credit_hold(owner, preferences): return None
    if dispatch_rank(owner, job, preferences) is None: return None
    if balance_ready(owner, preferences=preferences) and owner['credits'] < max(MIN_CREDITS, cost or 0): return None
    online = any(c is owner for c in clients)
    if (online and balance_ready(owner, preferences=preferences) and owner.get('page_ready') is not False and
            not block_for(owner, preferences) and not owner.get('blocked')):
        return entry
    # An inaccessible owner gets one bounded recovery window, never an
    # indefinite lock. A healthy funded owner remains preferred after it.
    return entry if time.time() - entry.get('at', 0) < timing_value(preferences, 'browser_startup_seconds') else None


def remember_owner(service, row, client):
    key = image_key(row['payload'])
    if not key: return
    entries = dict(service.preferences.get('image_affinities', {}))
    entries[key] = {'account':str(client['account']).strip().casefold(), 'profile':client.get('profile', ''),
        'browser':client['id'], 'at':time.time()}
    service.commit_preferences({**service.preferences, 'image_affinities':entries})
