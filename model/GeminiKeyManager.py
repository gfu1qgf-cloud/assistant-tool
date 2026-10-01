"""Main-owned credentials. Workers request/report; only the owner saves on Qt's GUI thread."""
from copy import deepcopy
import hashlib
import math
import re
import threading
import time
from qt_compat import QtCore
from model.SensitiveData import register_sensitive_values

KEYS_CONFIG_KEY = "gemini_api_keys"
STATUSES_CONFIG_KEY = "gemini_api_key_statuses"


def normalize_keys(value):
    if isinstance(value, str):
        value = re.split(r"[,;；，\s]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(k).strip() for k in value if str(k).strip()))


def key_id(key):
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def _entry(value):
    if not isinstance(value, dict):
        return {}
    # Whitelist fields; never save arbitrary API messages or request URLs.
    return {"state": str(value.get("state", "unknown")),
            "checked_at": _number(value.get("checked_at")),
            "blocked_until": _number(value.get("blocked_until")),
            "http_status": int(_number(value.get("http_status")))}


def merge_statuses(older, newer):
    result = {}
    for source in (older, newer):
        if not isinstance(source, dict):
            continue
        for fingerprint, record in source.items():
            if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)) or not isinstance(record, dict):
                continue
            target = result.setdefault(fingerprint, {"auth": {}, "models": {}})
            incoming = _entry(record.get("auth"))
            if incoming and incoming["checked_at"] >= target["auth"].get("checked_at", 0):
                target["auth"] = incoming
            models = record.get("models", {})
            if isinstance(models, dict):
                for model, raw in models.items():
                    incoming = _entry(raw)
                    if incoming and incoming["checked_at"] >= target["models"].get(model, {}).get("checked_at", 0):
                        target["models"][str(model)] = incoming
    return result


class GeminiKeyManager(QtCore.QObject):
    changed = QtCore.pyqtSignal()

    def __init__(self, config=None, parent=None, clock=time.time):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._clock = clock
        self._keys, self._statuses = [], {}
        self._revision = self._saved_revision = 0
        self.apply_config(config or {}, merge_local=False)

    def apply_config(self, config, merge_local=True):
        with self._lock:
            self._keys = normalize_keys(config.get(KEYS_CONFIG_KEY, config.get("gemini_api_key", [])))
            register_sensitive_values(self._keys)
            self._statuses = merge_statuses(config.get(STATUSES_CONFIG_KEY, {}),
                                            self._statuses if merge_local else {})

    def update_config(self, config):
        with self._lock:
            config[KEYS_CONFIG_KEY] = list(self._keys)
            ids = {key_id(k) for k in self._keys}
            config[STATUSES_CONFIG_KEY] = deepcopy({k: v for k, v in self._statuses.items() if k in ids})
        return config

    def snapshot(self):
        return self.update_config({})

    @property
    def revision(self):
        with self._lock:
            return self._revision

    @property
    def dirty(self):
        with self._lock:
            return self._revision != self._saved_revision

    def mark_saved(self, revision):
        with self._lock:
            self._saved_revision = revision

    def _available(self, key, model):
        if key not in self._keys:
            return False
        record = self._statuses.get(key_id(key), {})
        if record.get("auth", {}).get("state") == "invalid":
            return False
        return record.get("models", {}).get(str(model or ""), {}).get("blocked_until", 0) <= self._clock()

    def request_keys(self, model=None):
        with self._lock:
            return [k for k in self._keys if self._available(k, model)]

    def request_key(self, model=None):
        keys = self.request_keys(model)
        return keys[0] if keys else None

    def is_available(self, key, model=None):
        with self._lock:
            return self._available(key, model)

    def _record(self, key, model, state, code=0, delay=0, invalid=False):
        with self._lock:
            if key not in self._keys:
                return
            now = self._clock()
            record = self._statuses.setdefault(key_id(key), {"auth": {}, "models": {}})
            if invalid or state == "usable":
                record["auth"] = {"state": "invalid" if invalid else "valid", "checked_at": now,
                                  "blocked_until": 0, "http_status": code}
            record["models"][str(model or "")] = {"state": state, "checked_at": now,
                "blocked_until": now + delay if delay else 0, "http_status": code}
            self._revision += 1
        self.changed.emit()

    def report_success(self, key, model=None):
        self._record(key, model, "usable")

    def report_failure(self, key, model=None, http_status=None, retry_after=None, message=""):
        code = int(_number(http_status))
        invalid = code == 401 or (code == 400 and any(marker in str(message).upper()
            for marker in ("API_KEY_INVALID", "API KEY NOT VALID", "API_KEY_EXPIRED")))
        if invalid:
            state, delay = "invalid", 0
        elif code == 429:
            state, delay = "rate_limited", max(1, _number(retry_after, 600)) + 5
        elif code == 403:
            state, delay = "forbidden", 86400
        elif code >= 500 or not code:
            state, delay = "transient", 0
        else:
            state, delay = "request_error", 0
        self._record(key, model, state, code, delay, invalid)

    def status_text(self, key):
        labels = {"usable": "可用", "rate_limited": "限流/额度限制", "forbidden": "模型无权限",
                  "transient": "网络/服务异常（仍可重试）", "request_error": "请求参数错误", "unknown": "未验证"}
        with self._lock:
            record = deepcopy(self._statuses.get(key_id(key), {}))
        if record.get("auth", {}).get("state") == "invalid":
            return "密钥无效（请更换或重置状态）"
        lines = []
        for model, entry in record.get("models", {}).items():
            state = labels.get(entry.get("state"), "未验证")
            until = entry.get("blocked_until", 0)
            if until > self._clock():
                state += "，" + time.strftime("%m-%d %H:%M", time.localtime(until)) + " 后重试"
            elif until:
                state += "（冷却已结束，可重试）"
            lines.append((model or "默认模型") + "：" + state)
        return "；".join(lines) or "未验证（首次调用后自动记录）"

    def redact(self, message):
        with self._lock:
            keys = list(self._keys)
        for key in keys:
            message = str(message).replace(key, "[已隐藏密钥]")
        return str(message)

    def unavailable_message(self, model=None):
        with self._lock:
            count = len(self._keys)
        return ("未配置 Gemini Key" if not count else "Gemini Key 当前均不可用或处于冷却中") + "；请查看程序设置 → AI 密钥。"
