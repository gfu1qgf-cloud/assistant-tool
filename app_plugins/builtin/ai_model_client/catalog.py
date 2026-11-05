"""Credential-scoped catalog identity; never store/print plaintext keys."""
import hashlib
import os

from model.AIProviderKeys import KEYS_CONFIG_KEY


def openai_identity(config):
    keys = config.get(KEYS_CONFIG_KEY) or {}
    encrypted = keys.get('openai') if isinstance(keys, dict) else None
    value = encrypted or os.environ.get('OPENAI_API_KEY', '').strip()
    if not value:
        return None
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()
