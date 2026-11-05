"""New provider credentials: Windows current-user DPAPI, or environment vars.

Existing Gemini credentials are deliberately not migrated or rewritten here.
"""
import base64
import ctypes
import os
import copy
import hashlib
from uuid import uuid4
from ctypes import wintypes

from model.SensitiveData import register_sensitive_values

KEYS_CONFIG_KEY = "ai_provider_keys"
ENV_KEYS = {"mistral": "MISTRAL_API_KEY", "openrouter": "OPENROUTER_API_KEY",
            "cloudflare": "CLOUDFLARE_API_TOKEN", "groq": "GROQ_API_KEY",
            "openai": "OPENAI_API_KEY"}


def cloudflare_account(config):
    import re
    settings = config.get("ai_model_client") or {}
    settings = settings if isinstance(settings, dict) else {}
    selected=selected_credential(config,'cloudflare')
    account = str((selected or {}).get('account_id') or settings.get("cloudflare_account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", account):
        raise ValueError("请在连接设置填写 Cloudflare Account ID（32位十六进制，不是 API Token）。")
    return account.lower()


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("密钥本地加密仅支持 Windows；其他系统请使用对应的环境变量。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.POINTER(wintypes.LPWSTR),
                             ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None if decrypt else "LZX AI credential",
                    None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError("Windows 密钥加密/解密失败，可能不是原 Windows 用户，请重新填写该服务的密钥。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def protect_key(key):
    key = str(key).strip()
    if not key or len(key) > 4096 or any(character.isspace() for character in key):
        raise ValueError("API Key 不能为空、不能含空白，且长度不能超过4096字符。")
    register_sensitive_values([key])
    return "dpapi:" + base64.b64encode(_dpapi(key.encode("utf-8"))).decode("ascii")


def key_pool(value):
    """Read old encrypted strings without mutating them; validate new pools."""
    if value is None or value == '': return {'version':1,'active':None,'items':[]}
    if isinstance(value,str):
        if not value.startswith('dpapi:'): raise ValueError('拒绝使用未加密密钥，请在密钥列表中更换。')
        identifier=hashlib.sha256(value.encode()).hexdigest()[:32]
        return {'version':1,'active':identifier,'items':[{'id':identifier,'name':'原有密钥',
                'encrypted':value,'hint':'••••（旧密钥）'}]}
    if not isinstance(value,dict) or value.get('version')!=1 or not isinstance(value.get('items'),list) or len(value['items'])>1000:
        raise ValueError('AI 密钥列表格式不正确。')
    result=copy.deepcopy(value); identifiers=set()
    for row in result['items']:
        if (not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or len(row['id'])>64 or
            row['id'] in identifiers or not isinstance(row.get('encrypted'),str) or not row['encrypted'].startswith('dpapi:')):
            raise ValueError('AI 密钥列表存在损坏或重复记录。')
        identifiers.add(row['id'])
        row['name']=str(row.get('name') or '未命名密钥')[:120]
        row['hint']=str(row.get('hint') or '••••')[:40]
    active=result.get('active')
    if active is not None and active!='@environment' and active not in identifiers:
        raise ValueError('当前使用的密钥已不存在，请重新选择。')
    return result


def credential_entry(key, name='', *, account_id=''):
    key=str(key).strip()
    encrypted=protect_key(key)
    row={'id':uuid4().hex,'name':str(name).strip()[:120] or '未命名密钥',
         'encrypted':encrypted,'hint':'••••'+key[-4:] if len(key)>=8 else '••••',
         'fingerprint':hashlib.sha256(key.encode()).hexdigest()}
    if account_id:
        import re
        if not re.fullmatch(r'[0-9a-fA-F]{32}',account_id.strip()): raise ValueError('Account ID 必须是32位十六进制。')
        row['account_id']=account_id.strip().lower()
    return row


def selected_credential(config, provider):
    if provider not in ENV_KEYS:
        raise ValueError("未知的 AI 服务。")
    stored = config.get(KEYS_CONFIG_KEY, {})
    stored = stored if isinstance(stored, dict) else {}
    pool=key_pool(stored.get(provider))
    active=pool.get('active')
    if active=='@environment':
        if not os.environ.get(ENV_KEYS[provider],'').strip(): raise ValueError('选中的环境变量密钥当前为空。')
        return None
    if pool['items'] and not active: raise ValueError('请在 '+provider+' 密钥列表中选择当前使用的 Key。')
    return next((row for row in pool['items'] if row['id']==active),None)


def provider_key_identity(config,provider):
    try: row=selected_credential(config,provider)
    except ValueError: return None
    value=row['encrypted'] if row else os.environ.get(ENV_KEYS[provider],'').strip()
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def pin_provider_config(config,provider):
    """Pin only the active credential at submission, not an entire key list."""
    if provider=='gemini': return {}
    row=selected_credential(config,provider)
    result={KEYS_CONFIG_KEY:{provider:row['encrypted']}} if row else {}
    if provider=='cloudflare': result['ai_model_client']={'cloudflare_account_id':cloudflare_account(config)}
    return result


def read_provider_key(config, provider):
    row=selected_credential(config,provider)
    encrypted=row['encrypted'] if row else None
    if encrypted:
        if not isinstance(encrypted, str) or not encrypted.startswith("dpapi:"):
            raise ValueError("AI 服务密钥格式不正确，请重新填写；不会使用未加密密钥。")
        try:
            key = _dpapi(base64.b64decode(encrypted[6:], validate=True), decrypt=True).decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ValueError("已保存的 AI 服务密钥无法读取，请重新填写。") from error
    else:
        key = os.environ.get(ENV_KEYS[provider], "").strip()
    if not key:
        raise ValueError("请在“程序设置 → AI 模型试用台 → 密钥列表”添加 " + provider + " Key 并设为当前。")
    register_sensitive_values([key])
    return key
