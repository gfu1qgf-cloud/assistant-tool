"""Explicit recovery and safeguards for main-owned Gemini credentials.

Never automatically resurrect deleted keys from old backups. Recovery is an
explicit user action, and changes only the shared credential field.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from model.GeminiKeyManager import KEYS_CONFIG_KEY, normalize_keys


def config_keys(config):
    return normalize_keys(config.get(KEYS_CONFIG_KEY, config.get("gemini_api_key", [])))


def read_backup_keys(path):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("配置备份过大，未读取")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("配置备份必须是 JSON 对象")
    keys = config_keys(value)
    if not keys:
        raise ValueError("这个备份没有 Gemini Key")
    return keys


def preserve_unedited_keys(config, disk_config):
    """An automatic/state-only save must not erase populated disk credentials.

    Explicit edits in the credential dialog bypass this safeguard, including
    intentional deletion of the last key. An explicit empty disk list stays
    empty, so an external deletion is not undone.
    """
    if not config_keys(config) and config_keys(disk_config):
        config[KEYS_CONFIG_KEY] = config_keys(disk_config)
        return True
    return False


def restore_keys_from_backup(config_path, backup_path):
    """Merge keys atomically without replacing unrelated or newer settings."""
    config_path = Path(config_path)
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("主配置必须是 JSON 对象")
    keys = config_keys(config)
    recovered = read_backup_keys(backup_path)
    config[KEYS_CONFIG_KEY] = list(dict.fromkeys(keys + recovered))
    if config[KEYS_CONFIG_KEY] == keys:
        return {"count": len(keys), "added": 0, "backup": None}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safety_backup = config_path.with_name(f"{config_path.name}.bak-gemini-recovery-{stamp}")
    temporary = config_path.with_name(f"{config_path.name}.{uuid.uuid4().hex}.recovery.tmp")
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
        # Do not overwrite a configuration saved by another process meanwhile.
        if hashlib.sha256(config_path.read_bytes()).digest() != hashlib.sha256(original).digest():
            raise ValueError("恢复期间配置已被修改，未覆盖；请重试")
        shutil.copy2(config_path, safety_backup)
        temporary.replace(config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"count": len(config[KEYS_CONFIG_KEY]),
            "added": len(config[KEYS_CONFIG_KEY]) - len(keys), "backup": str(safety_backup)}
