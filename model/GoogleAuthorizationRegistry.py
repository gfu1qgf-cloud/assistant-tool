"""In-memory authorization declarations, never tokens or an installed-plugin list."""
from dataclasses import dataclass
import re
import threading


def valid_scope(scope):
    return isinstance(scope, str) and (scope == 'https://mail.google.com/' or re.fullmatch(
        r'https://www\.googleapis\.com/auth/[A-Za-z0-9._/-]{1,200}', scope) is not None
    )


@dataclass(frozen=True)
class GoogleAuthorization:
    purpose_id: str
    title: str
    api: str
    version: str
    scopes: tuple
    order: int = 100
    allow_legacy: bool = False
    legacy_bindings: tuple = ()


@dataclass(frozen=True)
class RegisteredAuthorization:
    owner: str
    definition: GoogleAuthorization

    @property
    def key(self): return self.owner + '.' + self.definition.purpose_id


class GoogleAuthorizationRegistry:
    def __init__(self, owner_enabled=None):
        self._entries, self._aliases = {}, {}
        self._lock = threading.RLock()
        self.owner_enabled = owner_enabled or (lambda _owner: True)

    def register(self, owner, definition):
        if not isinstance(definition, GoogleAuthorization):
            raise TypeError('register_google_authorization 只接受 GoogleAuthorization。')
        identifier = lambda value: isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', value)
        if not identifier(owner) or not identifier(definition.purpose_id):
            raise ValueError('Google 授权插件／用途 ID 无效。')
        if (not isinstance(definition.title, str) or not definition.title.strip() or len(definition.title) > 160
                or not isinstance(definition.api, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', definition.api)
                or not isinstance(definition.version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,31}', definition.version)
                or not isinstance(definition.scopes, tuple) or not 1 <= len(definition.scopes) <= 32
                or any(not valid_scope(scope) for scope in definition.scopes)
                or len(set(definition.scopes)) != len(definition.scopes)
                or type(definition.order) is not int or type(definition.allow_legacy) is not bool
                or not isinstance(definition.legacy_bindings, tuple)
                or any(not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}(\.[a-z][a-z0-9_]{0,79})?', key)
                       for key in definition.legacy_bindings)):
            raise ValueError('Google 授权声明无效：请提供用途名称、API、版本及 Google OAuth 权限。')
        entry = RegisteredAuthorization(owner, definition)
        keys = (entry.key, *definition.legacy_bindings)
        with self._lock:
            if len(set(keys)) != len(keys) or any(key in self._entries or key in self._aliases for key in keys):
                raise ValueError('Google 授权用途或兼容别名重复：' + entry.key)
            self._entries[entry.key] = entry
            for alias in definition.legacy_bindings: self._aliases[alias] = entry.key
        return entry

    def resolve(self, key):
        with self._lock: return self._entries.get(self._aliases.get(key, key))

    def entries(self, *, active_only=False):
        with self._lock:
            return tuple(sorted((entry for entry in self._entries.values()
                                 if not active_only or self.owner_enabled(entry.owner)),
                                key=lambda entry: (entry.definition.order, entry.key)))

    def unregister_owner(self, owner):
        with self._lock:
            self._entries = {key: entry for key, entry in self._entries.items() if entry.owner != owner}
            self._aliases = {key: value for key, value in self._aliases.items() if value in self._entries}
