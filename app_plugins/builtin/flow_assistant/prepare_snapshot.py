"""Reuse a verified preflight; run_tasks still sets and verifies the real UI."""
import time
import uuid

from .credit_policy import balance_ready
from .pricing import fixed_cost
from .setup_support import project_identity
from .timing import value as timing_value


def signature(job):
    return tuple(job.get(key) for key in ('model', 'duration', 'aspect', 'resolution'))


def remember(service, client, row, data):
    instance = data.get('page_instance')
    if (not instance or instance != client.get('page_instance') or
            str(data.get('email') or '').strip().casefold() != str(client.get('account') or '').strip().casefold() or
            not data.get('verified') or data.get('model') != 'omni_1_1_flash' or fixed_cost(row['payload']) is None):
        return
    project = project_identity(client.get('project'))
    if not project or project != project_identity(client.get('saved_project')): return
    client['prepare_snapshot'] = {'at': time.time(), 'instance': instance, 'project': project,
        'account': client['account'], 'settings': signature(row['payload']), 'data': {
            key: data[key] for key in ('verified', 'model', 'email', 'page_instance')},
        'extension': client.get('extension_version')}


def reuse(service, client, row):
    saved = client.get('prepare_snapshot') or {}
    lifetime = timing_value(service.preferences, 'prepare_snapshot_seconds')
    if (not lifetime or not saved or time.time() - saved['at'] >= lifetime or
            saved['account'] != client.get('account') or saved['instance'] != client.get('page_instance') or
            saved['extension'] != client.get('extension_version') or saved['settings'] != signature(row['payload']) or
            saved['project'] != project_identity(client.get('project')) or
            saved['project'] != project_identity(client.get('saved_project')) or
            not client.get('page_ready') or not balance_ready(client, preferences=service.preferences) or
            fixed_cost(row['payload']) is None):
        return False
    # Share the actual response's account/budget/credit transition. Cached
    # observations keep their original timestamp instead of sliding forever.
    request_id = uuid.uuid4().hex
    service.pending[request_id] = {'action': 'prepare', 'job': row['id'], 'client': client['id'],
        'at': time.time(), 'attempt': row['attempt'], 'snapshot_reuse': True}
    service.store.update(row['id'], row['status'], remote={**row['remote'],
        'prepare_snapshot': {'reused': True, 'observed_at': saved['at']}}, error='')
    service._response(client, {'request_id': request_id, 'success': True, 'data': dict(saved['data'])})
    return True
