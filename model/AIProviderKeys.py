"""New provider credentials: Windows current-user DPAPI, or environment vars.

Existing Gemini credentials are deliberately not migrated or rewritten here.
"""
import base64
import ctypes
import os
from ctypes import wintypes

from model.SensitiveData import register_sensitive_values

KEYS_CONFIG_KEY = "ai_provider_keys"
ENV_KEYS = {"mistral": "MISTRAL_API_KEY", "openrouter": "OPENROUTER_API_KEY",
            "cloudflare": "CLOUDFLARE_API_TOKEN", "groq": "GROQ_API_KEY"}


def cloudflare_account(config):
    import re
    settings = config.get("ai_model_client") or {}
    settings = settings if isinstance(settings, dict) else {}
    account = str(settings.get("cloudflare_account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")).strip()
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


def read_provider_key(config, provider):
    if provider not in ENV_KEYS:
        raise ValueError("未知的 AI 服务。")
    stored = config.get(KEYS_CONFIG_KEY, {})
    stored = stored if isinstance(stored, dict) else {}
    encrypted = stored.get(provider)
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
        raise ValueError("请在“AI 模型试用台 → 连接设置”填写 " + provider + " Key。")
    register_sensitive_values([key])
    return key
