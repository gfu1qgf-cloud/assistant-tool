"""Redact credentials before persisting or displaying diagnostic messages."""
import re
import threading

_lock = threading.RLock()
_known_secrets = set()
_labels = re.compile(
    r'''(?i)(["']?(?:api[_ -]?keys?|xi-api-key|x-goog-api-key|access_token|refresh_token|id_token|client_secret|password)["']?\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|\[[^\]]*\]|[^\s,;}\]]+)''')
_bearer = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_recognized = re.compile(
    r"AIza[0-9A-Za-z_-]{35}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"
    r"|\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def register_sensitive_values(values):
    """Remember active/previous keys in memory only, including unlabeled errors."""
    with _lock:
        _known_secrets.update(str(value) for value in values if value and len(str(value)) >= 8)


def redact_sensitive_text(value):
    text = str(value)
    with _lock:
        known = tuple(sorted(_known_secrets, key=len, reverse=True))
    for secret in known:
        text = text.replace(secret, "[REDACTED]")
    text = _recognized.sub("[REDACTED]", text)
    text = _bearer.sub("Bearer [REDACTED]", text)
    return _labels.sub(lambda match: match.group(1) + "[REDACTED]", text)
