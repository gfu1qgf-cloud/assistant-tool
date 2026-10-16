from model.AppDataStorage import data_io, data_file, data_directory
"""Serialize short config transactions, not network/model work."""
import copy
import json
from pathlib import Path
import threading

_LOCKS = {}
_GUARD = threading.Lock()
ELEVENLABS_STATUSES = "elevenlabs_api_key_statuses"


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


def merge_latest_api_statuses(path, config):
    """A settings dialog may have an older snapshot than the audio worker."""
    if Path(path).is_file():
        latest = read_api_statuses(path)
        if latest:
            config[ELEVENLABS_STATUSES] = {**(config.get(ELEVENLABS_STATUSES) or {}), **latest}
    return config
