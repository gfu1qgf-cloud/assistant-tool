"""Healthy browser pass first; only confirmed closed faults rejoin a new pass."""
import time
from .browser_options import snapshot
from .connection_status import configured_profiles
from .problem_details import temporary_code


def enabled(preferences,client=None):
    return snapshot(preferences)['auto_reopen_page_faults'] and (client is None or
        {'automatic_browser_close_v1','protected_browser_close_v1'}<=set(client.get('capabilities',[])))


def is_fault(profile,preferences):
    entry=preferences.get('browser_lifecycle',{}).get(profile,{})
    return bool(entry.get('rotation_reason') or entry.get('kind')=='page_fault' or
        temporary_code(preferences.get('browser_blocks',{}).get('profile:'+str(profile),'')) or
        preferences.get('environment_recoveries',{}).get('profile:'+str(profile),{}).get('code'))


def marked(preferences,client,reason):
    profile=client.get('profile')
    if not profile:return preferences
    entries=dict(preferences.get('browser_lifecycle',{}))
    entries[profile]={**entries.get(profile,{}),'rotation_reason':str(reason)[:1000],'fault_at':time.time()}
    return {**preferences,'browser_lifecycle':entries}


def healthy_remaining(service):
    from .policy import admitted_queue,block_for
    from .image_affinity import cached_profile,allows_image,affinity_context
    from .routing import dispatch_rank
    from .high_credit import allowed
    from .balance_dispatch import can_fund
    from .store import TRANSIENT
    from .browser_slots import live_starting_profiles
    rows=service.store.rows();prefs=service.preferences
    pending=admitted_queue(rows,service.store.batches(),service.observed_costs,
        paused=service.paused,released=service.released)
    if not pending:return True  # Do not reset/reopen faulted browsers while idle.
    job=pending[0]['payload'];clients=[c for c in service.clients.values() if c.get('authorized')]
    affinity=affinity_context(clients,rows,prefs,{r['id'] for r in pending})
    holds=service.store.reset_holds();starting=live_starting_profiles(service)
    for profile in configured_profiles(prefs):
        if is_fault(profile,prefs) or profile in prefs.get('browser_launch_failures',{}):continue
        client=next((c for c in clients if c.get('profile')==profile),None)
        # An expired launch with no authenticated client cannot be selected
        # again until native absence is proven. Do not let that stale reservation
        # keep closed faulted profiles waiting for a fictional healthy browser.
        if not client and profile not in starting and (profile in getattr(service,'launch_attempts',set()) or
                profile in service.launched or prefs.get('browser_slots',{}).get(profile,{}).get('state')=='opening'):continue
        client=client or cached_profile(profile,prefs)
        if (block_for(client,prefs) or client.get('blocked') or not allowed(client,prefs) or
            dispatch_rank(client,job,prefs) is None or not can_fund(client,job,prefs,service.observed_costs) or
            not allows_image(client,job,rows,prefs,affinity['images'])):continue
        account=str(client.get('account') or '').strip().casefold()
        if any(h.get('profile')==profile or account and h.get('account')==account for h in holds):continue
        if any(r['status'] in TRANSIENT|{'uncertain','download_failed','archiving'} and
            (r['remote'].get('profile')==profile or account and str(r['remote'].get('account') or '').casefold()==account)
            for r in rows):continue
        return True
    return False
