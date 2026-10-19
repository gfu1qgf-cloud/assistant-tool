from model.AppDataStorage import data_io, data_file, data_directory
"""Serialize short config transactions, not network/model work."""
import copy
import json
from pathlib import Path
import threading

_LOCKS = {}
_GUARD = threading.Lock()
ELEVENLABS_STATUSES = "elevenlabs_api_key_statuses"


def _status_time(value):
    try:
        return max(0, int(value)) if type(value) in (int, float) else 0
    except (ValueError, OverflowError):
        return 0


def config_file_lock(path):
    key = str(Path(path).expanduser().resolve()).casefold()
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def read_api_statuses(path):
    with config_file_lock(path):
        try:
            data = json.loads(data_file(Path(path)).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        statuses = data.get(ELEVENLABS_STATUSES, {})
        return copy.deepcopy(statuses) if isinstance(statuses, dict) else {}


def merge_latest_api_statuses(path, config, *, reset_key_ids=(), permission_updates=None):
    """Worker-owned disk status is authoritative, including explicit deletion.

    A settings reset is a per-key operation, applied after reading the latest
    worker status under the same transaction lock; it is never persisted as
    extra configuration metadata or a copy of the actual API key.
    """
    local = config.get(ELEVENLABS_STATUSES)
    statuses = copy.deepcopy(local) if isinstance(local, dict) else {}
    if Path(path).is_file():
        statuses = read_api_statuses(path)
    for key_id in reset_key_ids:
        statuses.pop(key_id, None)
    # Apply only the form's explicit permission deltas after loading concurrent
    # worker quota/status changes. Never replace a complete stale status row.
    for key_id, updates in (permission_updates or {}).items():
        state = statuses.setdefault(key_id, {})
        if not isinstance(state, dict):
            state = statuses[key_id] = {}
        auth_time = _status_time(updates.get('permission_authentication_at'))
        latest_success = max([_status_time(state.get('last_success_unix'))] + [
            _status_time(state.get('permission_' + name + '_at'))
            for name in ('text_to_speech', 'forced_alignment')
            if state.get('permission_' + name) is True])
        stale_auth = (_status_time(state.get('permission_authentication_at')) > auth_time or
                      updates.get('permission_authentication') is False and latest_success >= auth_time)
        for name, value in updates.items():
            if not name.startswith('permission_'):
                continue
            if stale_auth and name in {'permission_authentication', 'permission_authentication_at'}:
                continue
            if value is None:
                state.pop(name, None)
            else:
                state[name] = copy.deepcopy(value)
    if ELEVENLABS_STATUSES in config or statuses or reset_key_ids or permission_updates:
        config[ELEVENLABS_STATUSES] = statuses
    return config
