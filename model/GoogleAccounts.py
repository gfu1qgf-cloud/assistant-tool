"""Host-owned, current-Windows-user encrypted Google account registry.

No credential paths or tokens are placed in config.json. Existing independent
OAuth files are left alone until a feature is explicitly bound to this registry.
"""
import base64
import copy
from contextlib import contextmanager
import ctypes
import hashlib
import json
from uuid import uuid4

from model.AIProviderKeys import _dpapi
from model.AppDataStorage import application_storage
from model.ConfigTransactions import config_file_lock
from model.SensitiveData import register_sensitive_values

SCOPES = {
    'sheets': 'https://www.googleapis.com/auth/spreadsheets',
    'drive': 'https://www.googleapis.com/auth/drive',
    'docs': 'https://www.googleapis.com/auth/documents',
}
SERVICES = {'sheets': ('sheets', 'v4'), 'drive': ('drive', 'v3'), 'docs': ('docs', 'v1')}
TOKEN_URI = 'https://oauth2.googleapis.com/token'
STORE_NAME = 'GoogleAccounts.json'
DISABLED = '@disabled'


def validate_client(value):
    installed = value.get('installed') if isinstance(value, dict) else None
    if (not isinstance(installed, dict) or not installed.get('client_id') or not installed.get('client_secret')
            or installed.get('auth_uri') not in {'https://accounts.google.com/o/oauth2/auth',
                                                'https://accounts.google.com/o/oauth2/v2/auth'}
            or installed.get('token_uri') != TOKEN_URI):
        raise ValueError('请选择 Google Cloud 的“桌面应用”OAuth 客户端 JSON，不支持服务账号或其他授权服务器。')
    # Discard unrelated fields; the loopback redirect is selected by the host.
    register_sensitive_values([installed['client_secret']])
    return {'installed': {key: installed[key] for key in ('client_id', 'client_secret', 'auth_uri', 'token_uri')}}


def token_scopes(value):
    scopes = value.get('scopes', [])
    if isinstance(scopes, str): scopes = scopes.split()
    if not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes):
        raise ValueError('Google 授权权限记录无效，请重新登录。')
    return set(scopes)


def validate_token(value):
    if (not isinstance(value, dict) or value.get('token_uri') != TOKEN_URI
            or not all(isinstance(value.get(key), str) and value[key]
                       for key in ('client_id', 'client_secret', 'refresh_token'))):
        raise ValueError('Google 授权记录无效或不支持离线续期，请重新登录。')
    token_scopes(value)
    register_sensitive_values([value.get('token'), value['refresh_token'], value['client_secret']])
    return copy.deepcopy(value)


class GoogleAccountManager:
    def __init__(self, storage=None):
        self.storage = storage or application_storage()
        self.file = self.storage.file_at(self.storage.root / STORE_NAME)
        self.lock = config_file_lock(self.file.path)

    @contextmanager
    def _transaction(self):
        # Short file transactions only: never hold this lock over OAuth/network.
        from model.PrivateDataCatalog import no_reparse
        with self.lock:
            self.storage.check(self.file.path, write=True)
            no_reparse(self.file.path, self.storage.root)
            # Windows named mutex: no extra lock file on a removable volume.
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            name = 'Local\\LZX.GoogleAccounts.' + hashlib.sha256(str(self.file.path).casefold().encode()).hexdigest()
            handle = kernel.CreateMutexW(None, False, name)
            if not handle: raise RuntimeError('无法取得 Google 账号库写入锁，未保存。')
            acquired = False
            try:
                acquired = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
                if not acquired: raise RuntimeError('另一个程序正在更新 Google 账号库，请稍后重试；未等待或覆盖。')
                yield
            finally:
                if acquired: kernel.ReleaseMutex(handle)
                kernel.CloseHandle(handle)

    def _read(self):
        if not self.file.exists(): return {'version': 1, 'clients': {}, 'accounts': {}, 'bindings': {}}
        try:
            from model.PrivateDataCatalog import no_reparse
            no_reparse(self.file.path, self.storage.root)
            with self.file.open('r', encoding='utf-8') as stream:
                text = stream.read(4 * 1024 * 1024 + 1)
            if len(text) > 4 * 1024 * 1024: raise ValueError()
            envelope = json.loads(text)
            if envelope.get('version') != 1 or envelope.get('protection') != 'windows-user-dpapi': raise ValueError()
            raw = _dpapi(base64.b64decode(envelope['payload'], validate=True), decrypt=True)
            state = json.loads(raw)
            if (state.get('version') != 1 or any(not isinstance(state.get(key), dict)
                                               for key in ('clients', 'accounts', 'bindings'))): raise ValueError()
            return state
        except Exception:
            # Never fall back to an empty registry or include SDK errors/secrets.
            raise ValueError('Google 账号库无法解密或已损坏；请使用原 Windows 用户，或恢复备份。未覆盖原文件。') from None

    def _write(self, state):
        raw = json.dumps(state, ensure_ascii=False).encode('utf-8')
        encrypted = _dpapi(raw)  # Encrypt BEFORE creating any temp/backup file.
        self.file.write_json({'version': 1, 'protection': 'windows-user-dpapi',
                              'payload': base64.b64encode(encrypted).decode('ascii')})

    def snapshot(self):
        """Safe UI projection: never return clients, refresh/access tokens."""
        with self.lock:
            state = self._read()
            return {'clients': [{'id': key, 'name': item['name']} for key, item in state['clients'].items()],
                    'accounts': [{'id': key, 'email': item['email'], 'client_id': item['client_id'],
                                  'features': [name for name, scope in SCOPES.items()
                                               if scope in token_scopes(item['credentials'])]}
                                 for key, item in state['accounts'].items()],
                    'bindings': copy.deepcopy(state['bindings'])}

    def add_client(self, value, name):
        client = validate_client(value)
        name = str(name).strip()[:120] or 'Google 桌面客户端'
        with self._transaction():
            state = self._read()
            key = next((key for key, item in state['clients'].items()
                        if item['config']['installed']['client_id'] == client['installed']['client_id']), uuid4().hex)
            state['clients'][key] = {'name': name, 'config': client}
            self._write(state)
        return key

    def client(self, client_id):
        with self.lock:
            value = self._read()['clients'].get(client_id)
            if value is None: raise ValueError('授权客户端已移除，请重新选择。')
            return validate_client(value['config'])

    def save_login(self, client_id, identity, credentials):
        if (not isinstance(identity, dict) or not identity.get('sub') or not identity.get('email')
                or identity.get('email_verified') is not True):
            raise ValueError('未取得 Google 验证过的账号身份，不保存猜测的账号。')
        value = validate_token(credentials)
        with self._transaction():
            state = self._read()
            client = state['clients'].get(client_id)
            if (not client or value['client_id'] != client['config']['installed']['client_id']
                    or value['client_secret'] != client['config']['installed']['client_secret']):
                raise ValueError('授权客户端已改变；本次登录未保存，请重试。')
            key = next((key for key, item in state['accounts'].items()
                        if item['subject'] == identity['sub'] and item['client_id'] == client_id), uuid4().hex)
            if any(account_id == key and SCOPES[feature] not in token_scopes(value)
                   for feature, account_id in state['bindings'].items()):
                raise ValueError('本次授权缺少该账号已绑定功能的权限；请勾选这些功能后重新授权，原本地授权未覆盖。')
            state['accounts'][key] = {'subject': identity['sub'], 'email': identity['email'],
                                      'client_id': client_id, 'credentials': value, 'revision': uuid4().hex}
            self._write(state)
        return key

    def bind(self, feature, account_id):
        self.bind_many({feature: account_id})

    def bind_many(self, bindings):
        if not set(bindings).issubset(SCOPES): raise ValueError('未知 Google 功能。')
        with self._transaction():
            state = self._read()
            for feature, account_id in bindings.items():
                if account_id == DISABLED:
                    state['bindings'][feature] = DISABLED
                elif account_id:
                    account = state['accounts'].get(account_id)
                    if not account or SCOPES[feature] not in token_scopes(account['credentials']):
                        raise ValueError('此账号没有该功能权限，请先补充授权。')
                    state['bindings'][feature] = account_id
                else:
                    state['bindings'].pop(feature, None)
            self._write(state)

    def remove(self, account_id):
        with self._transaction():
            state = self._read()
            if account_id not in state['accounts']: raise ValueError('账号已不存在。')
            del state['accounts'][account_id]
            state['bindings'] = {key: DISABLED if value == account_id else value
                                 for key, value in state['bindings'].items()}
            self._write(state)

    def bound_account(self, feature):
        with self.lock:
            state = self._read()
            key = state['bindings'].get(feature)
            if not key: return None
            if key == DISABLED:
                raise ValueError('此 Google 功能已停用；请在“程序设置 → Google 账号”分配账号。不会回退旧授权。')
            if key not in state['accounts']: raise ValueError('已绑定的 Google 账号不存在，请在程序设置中重新选择。')
            return key, copy.deepcopy(state['accounts'][key])

    def account(self, key):
        with self.lock:
            value = self._read()['accounts'].get(key)
            if value is None: raise ValueError('Google 账号已移除，请重新分配账号后再执行。')
            return copy.deepcopy(value)

    def save_refreshed(self, account_id, revision, value):
        value = validate_token(value)
        with self._transaction():
            state = self._read()
            current = state['accounts'].get(account_id)
            if not current or current['revision'] != revision:
                raise ValueError('Google 账号已删除或重新授权，旧请求未覆盖新授权；请重新执行该操作。')
            if current['credentials']['client_id'] != value['client_id']: raise ValueError('Google 客户端不匹配。')
            current['credentials'] = value
            current['revision'] = uuid4().hex
            self._write(state)


def load_managed_service(feature, *, manager=None):
    """None means unbound/legacy. A bound-but-failed account NEVER falls back."""
    from model.OfflineMode import require_online
    manager = manager or GoogleAccountManager()
    bound = manager.bound_account(feature)
    if bound is None: return None
    require_online('Google 账号服务')
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    import httplib2
    key, _account = bound
    refresh_lock = config_file_lock(str(manager.file.path) + '.' + key + '.refresh')
    if not refresh_lock.acquire(timeout=35):
        raise RuntimeError('Google 账号正在续期，请稍后重试；其他账号不会被阻塞。')
    try: credentials = _credentials(manager, key, feature)
    finally: refresh_lock.release()
    api, version = SERVICES[feature]
    return build(api, version, http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def _credentials(manager, key, feature):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    account = manager.account(key)
    value = validate_token(account['credentials'])
    if SCOPES[feature] not in token_scopes(value):
        raise ValueError('绑定的 Google 账号缺少此功能权限，请到“程序设置 → Google 账号”补充授权。')
    credentials = Credentials.from_authorized_user_info(value)
    if not credentials.valid:
        try:
            def request(*args, **kwargs):
                kwargs['timeout'] = 30
                return Request()(*args, **kwargs)
            credentials.refresh(request)
        except Exception:
            raise RuntimeError('Google 账号续期失败；请检查网络或到程序设置重新登录。未切换其他账号。') from None
        manager.save_refreshed(key, account['revision'], json.loads(credentials.to_json()))
    return credentials
