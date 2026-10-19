"""Antigravity 2.x's ONE known Windows credential, with no enumeration.

Credential reads remain in memory. Archives go through the host private store.
JWT claims describe a local login record, not online/signature verification.
"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import re

TARGET = 'gemini:antigravity'
NATIVE_AUTH_KEY = 'antigravityNativeWindowsCredential.v1'
MAX_RECORD = 128 * 1024


class NativeAuthError(OSError):
    pass


def _object(raw):
    try:
        if not isinstance(raw, (str, bytes)) or len(raw) > MAX_RECORD:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError) as error:
        raise NativeAuthError('新版 Antigravity 登录记录格式无法确认；未猜测邮箱或覆盖账号。') from error


def unpack_record(raw):
    value = _object(raw)
    try:
        if (type(value.get('version')) is not int or value['version'] != 1 or
                type(value.get('persist')) is not int or value['persist'] not in (1, 2, 3)):
            raise ValueError()
        if any(not isinstance(value.get(key), str) or len(value[key]) > 1024 for key in ('username', 'comment')):
            raise ValueError()
        blob = base64.b64decode(value['blob'], validate=True)
        if not 0 < len(blob) <= MAX_RECORD:
            raise ValueError()
    except (ValueError, KeyError, TypeError) as error:
        raise NativeAuthError('系统登录档案封装无效，未写入 Windows 凭据。') from error
    return value, blob


def inspect_native_record(raw):
    _metadata, blob = unpack_record(raw)
    payload = _object(blob)
    token = payload.get('token')
    if (not isinstance(token, dict) or
            any(not isinstance(token.get(key), str) or len(token[key]) < 6
                for key in ('access_token', 'refresh_token'))):
        raise NativeAuthError('新版登录记录缺少完整 OAuth 凭据，未保存账号。')
    encoded = payload.get('id_token')
    try:
        if not isinstance(encoded, str) or len(encoded) > 32768:
            raise ValueError()
        parts = encoded.split('.')
        if len(parts) != 3 or not all(parts):
            raise ValueError()
        claims = _object(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        email = claims.get('email')
        if (claims.get('iss') not in ('https://accounts.google.com', 'accounts.google.com') or
                claims.get('email_verified') is not True or not claims.get('sub') or
                not isinstance(email, str) or len(email) > 320 or
                not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', email)):
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise NativeAuthError('新版登录记录没有可确认的 Google 账号字段；不改读旧 IDE 缓存。') from error
    name = claims.get('name')
    return {'email': email, 'name': name[:200] if isinstance(name, str) else None,
            'plan': None, 'tier': None, 'avatar': None, 'has_token': True, 'identity_error': None}


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def _api():
    if os.name != 'nt':
        raise NativeAuthError('新版 Antigravity 系统凭据当前仅支持 Windows。')
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    return api


class WindowsCredentialBackend:
    def read(self):
        api = _api()
        pointer = ctypes.POINTER(_Credential)()
        if not api.CredReadW(TARGET, 1, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == 1168:
                return None
            raise NativeAuthError(f'读取 Antigravity 系统登录记录失败（Windows {code}），未改读其他账号。')
        try:
            record = pointer.contents
            if (record.Type != 1 or record.TargetName != TARGET or record.Flags or
                    record.AttributeCount or record.TargetAlias or
                    not 0 < record.CredentialBlobSize <= MAX_RECORD):
                raise NativeAuthError('系统登录记录使用了未支持的格式，未保存或改写。')
            blob = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
            return json.dumps({'version': 1, 'blob': base64.b64encode(blob).decode('ascii'),
                'username': record.UserName or '', 'comment': record.Comment or '', 'persist': record.Persist})
        finally:
            api.CredFree(pointer)

    def write(self, raw):
        api = _api()
        if raw is None:
            if not api.CredDeleteW(TARGET, 1, 0) and ctypes.get_last_error() != 1168:
                raise NativeAuthError('清除待绑定环境的系统登录记录失败；保留恢复记录。')
            return
        metadata, blob = unpack_record(raw)
        inspect_native_record(raw)
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        record = _Credential(Type=1, TargetName=TARGET, Comment=metadata['comment'],
            CredentialBlobSize=len(blob), CredentialBlob=buffer, Persist=metadata['persist'],
            UserName=metadata['username'])
        try:
            if not api.CredWriteW(ctypes.byref(record), 0):
                raise NativeAuthError('写入 Antigravity 系统登录记录失败；保留原账号及恢复档案。')
        finally:
            ctypes.memset(buffer, 0, len(buffer))
