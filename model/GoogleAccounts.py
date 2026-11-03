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
import re
from uuid import uuid4

from model.AIProviderKeys import _dpapi
from model.AppDataStorage import application_storage
from model.ConfigTransactions import config_file_lock
from model.SensitiveData import register_sensitive_values
from model.GoogleAuthorizationRegistry import GoogleAuthorizationRegistry

# Pre-registry helper/login compatibility only. New plugin declarations supply
# their own API/version/scopes through GoogleAuthorizationRegistry.
SCOPES = {
    'sheets': 'https://www.googleapis.com/auth/spreadsheets',
    'drive': 'https://www.googleapis.com/auth/drive',
    'docs': 'https://www.googleapis.com/auth/documents',
}
SERVICES = {'sheets': ('sheets', 'v4'), 'drive': ('drive', 'v3'), 'docs': ('docs', 'v1')}
TOKEN_URI = 'https://oauth2.googleapis.com/token'
STORE_NAME = 'GoogleAccounts.json'
DISABLED = '@disabled'


def binding_key(feature, profile=None):
    if feature not in SCOPES: raise ValueError('未知 Google 功能。')
    if profile is None: return feature  # Compatibility API; never inherited by a plugin.
    if not isinstance(profile, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', profile):
        raise ValueError('Google 功能的独立授权标识无效。')
    return profile + '.' + feature


def binding_feature(key):
    if key in SCOPES: return key
    profile, separator, feature = str(key).rpartition('.')
    if not separator or binding_key(feature, profile) != key: raise ValueError('未知 Google 功能。')
    return feature


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
    def __init__(self, storage=None, *, registry=None):
        self.storage = storage or application_storage()
        self.registry = registry if registry is not None else getattr(self.storage, 'google_authorizations', None)
        if self.registry is None: self.registry = GoogleAuthorizationRegistry()
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
            bindings = copy.deepcopy(state['bindings'])
            for entry in self.registry.entries():
                selected = self._selection(state, entry.key)
                if selected is not None: bindings[entry.key] = selected
            return {'clients': [{'id': key, 'name': item['name']} for key, item in state['clients'].items()],
                    'accounts': [{'id': key, 'email': item['email'], 'label': item.get('label', item['email']),
                                  'client_id': item['client_id'],
                                  'scopes': sorted(token_scopes(item['credentials'])),
                                  'features': [name for name, scope in SCOPES.items()
                                               if scope in token_scopes(item['credentials'])]}
                                 for key, item in state['accounts'].items()],
                    'bindings': bindings}

    def required_scopes(self, key, state=None):
        entry = self.registry.resolve(key)
        if entry is not None: return set(entry.definition.scopes)
        saved = (state or {}).get('binding_requirements', {}).get(key)
        if saved: return set(saved)
        # Compatibility for pre-registry callers only. New plugin APIs require
        # a declaration and never infer permissions from a service name.
        return {SCOPES[binding_feature(key)]}

    def _selection(self, state, key):
        entry = self.registry.resolve(key)
        keys = (entry.key, *entry.definition.legacy_bindings) if entry else (key,)
        return next((state['bindings'][item] for item in keys if item in state['bindings']), None)

    def integrate_legacy(self, config=None):
        """Register original authorizations locally, without using/replacing their tokens.

        Old entry points remain authoritative by default. Importing is not binding.
        No network, guessed identity, source writes or deletion occurs here.
        """
        from model.PrivateDataPaths import private_path
        from model.PrivateDataCatalog import no_reparse

        def read(path):
            no_reparse(path, self.storage.root)
            with self.storage.file_at(path).open('r', encoding='utf-8-sig') as stream:
                raw = stream.read(262145)
            if len(raw) > 262144: raise ValueError('授权文件过大。')
            return json.loads(raw)

        if config is None:
            file = self.storage.file_at(self.storage.root / 'config.json')
            config = file.read_json() if file.exists() else {}
        if not isinstance(config, dict): raise ValueError('程序配置无效；未接入旧授权。')
        pairs = [('原有网盘授权（账号未核验）', 'GoogleDriveCredentials.json', 'GoogleDriveToken.json')]
        default_client = config.get('review_sheet_credentials_file') or 'GoogleSheetsCredentials.json'
        default_token = config.get('review_sheet_token_file') or 'GoogleSheetsToken.json'
        for prefix, label in [('review_sheet', '原有表格授权'), ('task_submission_sheet', '任务提交表授权'),
                              ('sheet_monitor', '表格监视授权')]:
            pairs.append((label + '（账号未核验）', config.get(prefix + '_credentials_file') or default_client,
                          config.get(prefix + '_token_file') or default_token))
        candidates, errors, seen = [], [], set()
        for label, client_path, token_path in pairs:
            try:
                paths = [private_path(value, storage=self.storage, base=self.storage.root)
                         for value in (client_path, token_path)]
                if not all(self.storage.file_at(path).exists() for path in paths): continue
                refs = [path.relative_to(self.storage.root).as_posix() if path.is_relative_to(self.storage.root)
                        else str(path) for path in paths]
                source = hashlib.sha256(json.dumps(refs).casefold().encode()).hexdigest()
                if source in seen: continue
                seen.add(source)
                client, credentials = validate_client(read(paths[0])), validate_token(read(paths[1]))
                if any(credentials[key] != client['installed'][key] for key in ('client_id', 'client_secret')):
                    raise ValueError('客户端与令牌不匹配。')
                candidates.append((source, label, client, credentials))
            except Exception:
                errors.append(label + '未接入：文件无法读取或客户端与令牌不匹配；原文件未改动。')
        with self._transaction():
            state = self._read(); changed = False
            sources = state.setdefault('legacy_sources', {})
            for source, label, client, credentials in candidates:
                if source in sources: continue  # Removed entries stay removed; imports are idempotent.
                client_id = next((key for key, item in state['clients'].items()
                                  if item['config']['installed'] == client['installed']), uuid4().hex)
                if client_id not in state['clients']:
                    state['clients'][client_id] = {'name': label.replace('授权（账号未核验）', '客户端'), 'config': client}
                key = uuid4().hex
                state['accounts'][key] = {'subject': 'legacy:' + source, 'email': '', 'label': label,
                    'client_id': client_id, 'credentials': credentials, 'revision': uuid4().hex}
                sources[source] = key; changed = True
            if changed: self._write(state)
        return errors

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
            if any(account_id == key and not self.required_scopes(feature, state).issubset(token_scopes(value))
                   for feature, account_id in state['bindings'].items()):
                raise ValueError('本次授权缺少该账号已绑定功能的权限；请勾选这些功能后重新授权，原本地授权未覆盖。')
            state['accounts'][key] = {'subject': identity['sub'], 'email': identity['email'],
                                      'client_id': client_id, 'credentials': value, 'revision': uuid4().hex}
            self._write(state)
        return key

    def bind(self, feature, account_id):
        self.bind_many({feature: account_id})

    def bind_many(self, bindings):
        for feature in bindings: self.required_scopes(feature)
        with self._transaction():
            state = self._read()
            requirements = state.setdefault('binding_requirements', {})
            for feature, account_id in bindings.items():
                entry = self.registry.resolve(feature)
                feature = entry.key if entry else feature
                scopes = self.required_scopes(feature)
                if entry:
                    for alias in entry.definition.legacy_bindings:
                        state['bindings'].pop(alias, None); requirements.pop(alias, None)
                if account_id == DISABLED:
                    state['bindings'][feature] = DISABLED
                elif account_id:
                    account = state['accounts'].get(account_id)
                    if not account or not scopes.issubset(token_scopes(account['credentials'])):
                        raise ValueError('此账号没有该功能权限，请先补充授权。')
                    state['bindings'][feature] = account_id
                else:
                    state['bindings'].pop(feature, None)
                if account_id: requirements[feature] = sorted(scopes)
                else: requirements.pop(feature, None)
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
            key = self._selection(state, feature)
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


def load_drive_account_service(account_id, *, manager=None):
    """Core reference fallback: explicit saved account, no binding changes/login."""
    manager = manager or GoogleAccountManager()
    from model.OfflineMode import require_online
    require_online('Google参考文件授权读取')
    account = manager.account(account_id)
    scopes = token_scopes(account['credentials'])
    scope = next((value for value in ('https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/drive.file') if value in scopes), None)
    if scope is None: raise ValueError('此Google凭据没有Drive读取权限。')
    refresh_lock = config_file_lock(str(manager.file.path) + '.' + account_id + '.refresh')
    if not refresh_lock.acquire(timeout=35): raise RuntimeError('Google账号正在续期，请稍后再试。')
    try: credentials = _credentials(manager, account_id, required_scopes=(scope,))
    finally: refresh_lock.release()
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    import httplib2
    return build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def load_managed_service(feature, *, profile=None, manager=None, expected_account_id=None):
    """None means unbound/legacy. A bound-but-failed account NEVER falls back."""
    manager = manager or GoogleAccountManager()
    key = binding_key(feature, profile)
    if manager.registry.resolve(key) is not None:
        return load_authorization_service(key, manager=manager, expected_account_id=expected_account_id)
    return _load_service(key, SERVICES[feature], feature=feature, manager=manager,
                         expected_account_id=expected_account_id)


def load_authorization_service(authorization_id, *, manager=None, expected_account_id=None):
    manager = manager or GoogleAccountManager()
    entry = manager.registry.resolve(authorization_id)
    if entry is None: raise ValueError('Google 授权用途尚未注册，请检查插件是否启用。')
    if not manager.registry.owner_enabled(entry.owner): raise ValueError('此 Google 授权用途所属插件已禁用。')
    definition = entry.definition
    return _load_service(entry.key, (definition.api, definition.version), manager=manager,
                         required_scopes=definition.scopes, expected_account_id=expected_account_id)


def prepare_google_account(owner, purpose_id, *, manager=None):
    manager = manager or GoogleAccountManager()
    key = owner + '.' + purpose_id
    entry = manager.registry.resolve(key)
    if entry is None or entry.key != key or entry.owner != owner:
        raise ValueError('本插件尚未注册此 Google 授权用途。')
    if not manager.registry.owner_enabled(owner): raise ValueError('此 Google 授权用途所属插件已禁用。')
    bound = manager.bound_account(key)
    if bound is None: raise ValueError('请在 Google 账号设置中为“' + entry.definition.title + '”分配账号。')
    if not set(entry.definition.scopes).issubset(token_scopes(bound[1]['credentials'])):
        raise ValueError('此用途所需权限已改变，请在 Google 账号设置补充授权。')
    return {'authorization_id': key, 'account_id': bound[0], 'authorization_revision': _authorization_revision(entry)}


def _authorization_revision(entry):
    definition = entry.definition
    return hashlib.sha256(json.dumps([definition.api, definition.version, sorted(definition.scopes)]).encode()).hexdigest()


def load_google_service(request, *, manager=None):
    """Worker API: plain submission snapshot, no credential objects in job payloads."""
    if (not isinstance(request, dict) or not isinstance(request.get('authorization_id'), str)
            or not isinstance(request.get('account_id'), str) or not request['account_id']
            or request['account_id'] == DISABLED):
        raise ValueError('Google 授权请求缺少提交时的用途／账号快照。')
    manager = manager or GoogleAccountManager()
    entry = manager.registry.resolve(request['authorization_id'])
    if entry is None or entry.key != request['authorization_id']:
        raise ValueError('Google 授权用途尚未注册，请检查插件是否启用。')
    if request.get('authorization_revision') != _authorization_revision(entry):
        raise ValueError('Google 授权用途的服务／权限在提交后改变，请重新确认后提交。')
    return load_authorization_service(request['authorization_id'], manager=manager,
                                      expected_account_id=request['account_id'])


def _load_service(binding, api_version, *, manager, expected_account_id=None, feature=None, required_scopes=None):
    from model.OfflineMode import require_online
    bound = manager.bound_account(binding)
    if expected_account_id is not None and (bound is None or bound[0] != expected_account_id):
        raise ValueError('Google 功能账号在提交后发生变化，请确认账号后重新操作；未自动换号。')
    if bound is None: return None
    require_online('Google 账号服务')
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    import httplib2
    key, _account = bound
    refresh_lock = config_file_lock(str(manager.file.path) + '.' + key + '.refresh')
    if not refresh_lock.acquire(timeout=35):
        raise RuntimeError('Google 账号正在续期，请稍后重试；其他账号不会被阻塞。')
    try: credentials = _credentials(manager, key, feature, required_scopes=required_scopes)
    finally: refresh_lock.release()
    api, version = api_version
    return build(api, version, http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def _credentials(manager, key, feature=None, *, required_scopes=None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    account = manager.account(key)
    value = validate_token(account['credentials'])
    scopes = set(required_scopes) if required_scopes is not None else {SCOPES[feature]}
    if not scopes.issubset(token_scopes(value)):
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
