"""Host-owned workflow AI capability. No Qt access from worker callbacks.

Prepared jobs contain an opaque session binding, never credentials/configuration.
Answers are DPAPI encrypted inside host-managed private storage, also on VC.
"""
from collections import OrderedDict
from contextlib import contextmanager
import copy
import hashlib
import json
import threading
import time
from uuid import uuid4

from model.AIProviderClient import AIProviderError, PROVIDERS
from model.AIProviderKeys import _dpapi, KEYS_CONFIG_KEY, cloudflare_account
from model.OfflineMode import require_online
from model.node_workflow.python_values import validate_data

MAX_ANSWER = 1024 * 1024


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf8')


class AIWorkflowService:
    def __init__(self, client, directory):
        self.client, self.directory = client, directory
        self._lock = threading.RLock()
        self._bindings = OrderedDict()
        self._pending = set()
        self._slots = {provider: threading.BoundedSemaphore(2) for provider in PROVIDERS}

    def bind(self, provider, config):
        """GUI submission step; copy ONLY this provider's connection settings."""
        if provider not in PROVIDERS:
            raise ValueError('未知的 AI 服务。')
        restricted = {}
        if provider != 'gemini':
            stored = config.get(KEYS_CONFIG_KEY) or {}
            if isinstance(stored, dict) and stored.get(provider):
                restricted[KEYS_CONFIG_KEY] = {provider: copy.deepcopy(stored[provider])}
        if provider == 'cloudflare':
            restricted['ai_model_client'] = {'cloudflare_account_id': cloudflare_account(config)}
        signature = hashlib.sha256(serialized([provider, restricted])).hexdigest()
        now = time.monotonic()
        with self._lock:
            for token in list(self._bindings):
                if self._bindings[token]['expires'] < now:
                    del self._bindings[token]
            for token, row in self._bindings.items():
                if row['signature'] == signature:
                    row['expires'] = now + 86400
                    return {'binding': token}
            if len(self._bindings) >= 128:
                raise ValueError('待执行的 AI 连接快照过多，请完成现有任务后再提交。')
            token = uuid4().hex
            self._bindings[token] = {'provider': provider, 'config': restricted,
                                     'signature': signature, 'expires': now + 86400}
            return {'binding': token}

    def _binding(self, token, provider):
        with self._lock:
            row = self._bindings.get(token)
            if row is None or row['expires'] < time.monotonic() or row['provider'] != provider:
                raise ValueError('AI 连接快照已失效，请从工作流／插件窗口重新提交；密钥没有写入工作流。')
            return copy.deepcopy(row['config'])

    def _read(self, key):
        file = self.directory.file('answers.sqlite3')
        if not file.exists():
            return None
        with self._lock:
            db = file.connect(read_only=True)
            try:
                row = db.execute('SELECT payload FROM answers WHERE key=?', (key,)).fetchone()
            finally:
                db.close()
        if row is None:
            return None
        if len(row[0]) > MAX_ANSWER + 65536:
            raise ValueError('AI 回答缓存过大或已损坏；请更换缓存版本号重试。')
        try:
            result = json.loads(_dpapi(bytes(row[0]), decrypt=True))
            validate_data(result)
            if not isinstance(result, dict) or not isinstance(result.get('text'), str):
                raise ValueError()
            return result
        except Exception as error:
            # Do not silently discard an unreadable cache and bill another call.
            raise ValueError('AI 回答缓存无法解密或已损坏；请更换缓存版本号，或恢复原 Windows 用户。') from error

    def _write(self, key, result):
        raw = serialized(result)
        if len(raw) > MAX_ANSWER:
            raise ValueError('AI 回答超过 1 MB，未截断、未写入缓存。')
        encrypted = _dpapi(raw)
        file = self.directory.file('answers.sqlite3')
        with self._lock:
            db = file.connect()
            try:
                with db:
                    db.execute('CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, payload BLOB NOT NULL, created REAL NOT NULL)')
                    db.execute('INSERT OR REPLACE INTO answers VALUES (?,?,?)', (key, encrypted, time.time()))
                    # Bound both count and bytes. Only encrypted payloads enter SQLite/journals.
                    rows = db.execute('SELECT key,length(payload) FROM answers ORDER BY created DESC,key').fetchall()
                    total = 0
                    for index, (old_key, size) in enumerate(rows):
                        total += size
                        if index >= 256 or total > 64 * 1024 * 1024:
                            db.execute('DELETE FROM answers WHERE key=?', (old_key,))
            finally:
                db.close()

    @staticmethod
    def _wait(context, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            context.check_canceled()
            time.sleep(min(.1, max(0, deadline-time.monotonic())))

    @contextmanager
    def _single_flight(self, key, context):
        while True:
            context.check_canceled()
            with self._lock:
                if key not in self._pending:
                    self._pending.add(key)
                    break
            self._wait(context, .1)
        try:
            yield
        finally:
            with self._lock:
                self._pending.discard(key)

    @staticmethod
    def _count_request(context, limit):
        execution = getattr(context, 'workflow_execution_id', None)
        usage = getattr(context, '_workflow_ai_usage', None)
        if usage is None or usage.get('execution') != execution:
            usage = {'requests': 0, 'limit': limit, 'execution': execution}
        usage['limit'] = min(usage['limit'], limit)
        if usage['requests'] >= usage['limit']:
            raise ValueError(f"本次执行达到 AI 请求上限 {usage['limit']} 次（含重试），已停止，未自动加额度。")
        usage['requests'] += 1
        context._workflow_ai_usage = usage

    def execute(self, binding, provider, model, messages, options, context, *, cache=True,
                revision=1, allow_preview=False, request_limit=20, retries=0, output_format='text',
                validate_result=None):
        context.check_canceled()
        self.directory.storage.check() if self.directory.storage else self.directory.exists()
        config = self._binding(binding, provider)
        key = hashlib.sha256(serialized([1, provider, model,
            (config.get('ai_model_client') or {}).get('cloudflare_account_id', ''),
            messages, options, revision, output_format])).hexdigest()
        preview = bool(getattr(getattr(context, 'workflow_globals', None), 'is_preview', False))
        with self._single_flight(key, context):
            result = self._read(key) if cache else None
            if result is not None:
                if validate_result is not None: validate_result(result)
                context.report('AI 节点：复用加密回答缓存，不发送网络请求。', None)
                return result, True
            if preview and not allow_preview:
                raise ValueError('AI 节点预览默认不联网，当前没有回答缓存。请先正式运行一次；'
                                 '或明确打开该节点的“允许预览联网”（可能消耗额度）。')
            require_online('工作流 AI 请求')
            if cache:
                # Detect unavailable crypto/storage before spending an API request.
                _dpapi(b'AI workflow cache check')
                self.directory.ensure()
            slot = self._slots[provider]
            while not slot.acquire(timeout=.1):
                context.check_canceled()
            try:
                for attempt in range(retries + 1):
                    context.check_canceled()
                    self._count_request(context, request_limit)
                    context.report(f'AI 节点：请求 {PROVIDERS[provider]} / {model}（不发送未连接的任务字段）。', None)
                    try:
                        answer = self.client.chat(provider, model, copy.deepcopy(messages), copy.deepcopy(options), config, context)
                        break
                    except AIProviderError as error:
                        if attempt >= retries or error.status not in {429, 502, 503, 504}:
                            raise
                        delay = error.retry_after if error.status == 429 else 2 ** attempt
                        if delay is None or not 0 <= delay <= 10:
                            raise  # Long quota cooldowns must not stall workflow workers.
                        context.report(f'AI 节点：服务暂不可用，{delay:.1f} 秒后有限重试；不换模型。', None)
                        self._wait(context, delay + .05)
                context.check_canceled()
                result = {name: copy.deepcopy(answer.get(name, default)) for name, default in (
                    ('text', ''), ('provider', provider), ('model', model), ('usage', {}), ('elapsed', 0), ('warning', ''))}
                validate_data(result)
                if not isinstance(result['text'], str) or not result['text'].strip():
                    raise ValueError('AI 未返回可用文字，未生成替代回答。')
                if len(serialized(result)) > MAX_ANSWER:
                    raise ValueError('AI 回答超过 1 MB，未截断。')
                if validate_result is not None: validate_result(result)
                if cache:
                    self._write(key, result)
                return result, False
            finally:
                slot.release()
