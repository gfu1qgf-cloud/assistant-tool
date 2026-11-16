"""Explicit, snapshot-checked completion of settled Batch image failures only."""


def completion_target(job):
    """No running/unknown/receipt-recovery jobs may turn into paid retries."""
    if job['spec'].get('mode', 'batch') != 'batch':
        return None
    if job['status'] not in ('partial', 'failed', 'cancelled'):
        return None
    if job.get('planning') and job['planning']['state'] != 'done':
        return None
    attempt = job['attempts'][-1] if job['attempts'] else None
    if not attempt or attempt['state'] != 'finished':
        return None
    # Settled batches contain only definite successes/failures. Inconsistent
    # records must be checked, not silently converted into new billable calls.
    if any(item['status'] not in ('done', 'failed') for item in job['items']):
        return None
    missing = [item['id'] for item in job['items'] if item['status'] == 'failed']
    if not missing:
        return None
    return {'id': job['id'], 'attempt': attempt['token'], 'item_ids': missing}


def completion_targets(jobs):
    return [target for job in jobs if (target := completion_target(job)) is not None]
