"""Cancelable OAuth/PKCE with verified identity, executed by host job workers."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import socket
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from model.GoogleAccounts import GoogleAccountManager, SCOPES, token_scopes
from model.OfflineMode import require_online
from model.SensitiveData import register_sensitive_values
from model.GoogleAuthorizationRegistry import valid_scope


def _identity(credentials):
    import requests
    try:
        response = requests.get('https://openidconnect.googleapis.com/v1/userinfo',
                                headers={'Authorization': 'Bearer ' + credentials.token}, timeout=(10, 30))
        response.raise_for_status()
        value = response.json()
    except Exception:
        raise RuntimeError('Google 登录已返回，但账号身份核验失败；未保存不明账号，请稍后重试。') from None
    if not isinstance(value, dict) or value.get('email_verified') is not True or not value.get('sub') or not value.get('email'):
        raise ValueError('Google 未返回已验证的账号身份；本次登录未保存。')
    return value


def authorize(payload, run, *, manager=None):
    require_online('Google 账号登录')
    from google_auth_oauthlib.flow import InstalledAppFlow
    manager = manager or GoogleAccountManager()
    requested = payload.get('scopes')
    if requested is None:  # Compatibility for old queued login requests.
        features = payload.get('features', [])
        if not features or any(item not in SCOPES for item in features): raise ValueError('请至少选择一项授权功能。')
        requested = [SCOPES[name] for name in features]
    if (not isinstance(requested, list) or not 1 <= len(requested) <= 128
            or any(not valid_scope(scope) for scope in requested)):
        raise ValueError('Google 登录权限声明无效，未打开授权页面。')
    requested = set(requested)
    scopes = sorted(requested | {'openid', 'https://www.googleapis.com/auth/userinfo.email'})
    client_id = payload['client_id']
    config = manager.client(client_id)
    flow = InstalledAppFlow.from_client_config(config, scopes, autogenerate_code_verifier=True)
    received, state = [], ['']
    callback_path = '/google-auth/' + secrets.token_urlsafe(24)

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass  # Never record authorization codes.
        def do_GET(self):
            parts = urlsplit(self.path)
            values = parse_qs(parts.query)
            valid = (parts.path == callback_path and values.get('state') == [state[0]]
                     and bool(values.get('code') or values.get('error')))
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(('Google 授权已返回，可以回到辅助工具。' if valid else '无效的授权回调。').encode('utf-8'))
            if valid and not received: received.append(self.path)

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(1)  # A silent local socket cannot block cancel forever.
            return connection, address

    run.check_canceled()
    with Server(('127.0.0.1', 0), Callback) as server:
        server.timeout = 0.25
        flow.redirect_uri = f'http://127.0.0.1:{server.server_port}{callback_path}'
        url, state[0] = flow.authorization_url(access_type='offline', prompt='consent select_account')
        if isinstance(flow.code_verifier, str): register_sensitive_values([flow.code_verifier])
        run.report('请在浏览器选择 Google 账号并授权；可取消，最多等待 3 分钟。', 10)
        if not webbrowser.open(url): raise RuntimeError('无法打开默认浏览器，请检查 Windows 默认浏览器设置。')
        deadline = time.monotonic() + 180
        while not received:
            run.check_canceled()
            manager.storage.check()
            if time.monotonic() >= deadline: raise TimeoutError('Google 登录等待超时；未改变已有账号。')
            try: server.handle_request()
            except (TimeoutError, socket.timeout): continue
        callback = f'https://127.0.0.1:{server.server_port}{received[0]}'
    run.check_canceled()
    if parse_qs(urlsplit(received[0]).query).get('error'):
        raise ValueError('Google 授权已取消或被拒绝，原账号未改变。')
    register_sensitive_values(parse_qs(urlsplit(received[0]).query).get('code', []))
    try: flow.fetch_token(authorization_response=callback, timeout=30)
    except Exception:
        raise RuntimeError('Google 授权兑换失败；未保存令牌，请重试。') from None
    run.check_canceled()
    credentials = flow.credentials
    value = json.loads(credentials.to_json())
    register_sensitive_values([value.get('token'), value.get('refresh_token'), value.get('id_token')])
    granted = getattr(credentials, 'granted_scopes', None)
    if granted is not None: value['scopes'] = list(granted)
    if not requested.issubset(token_scopes(value)):
        raise ValueError('未授予所选功能的全部权限，原账号未改变；请重新授权。')
    identity = _identity(credentials)
    run.check_canceled()
    key = manager.save_login(client_id, identity, value)
    run.report('Google 账号已验证并加密保存，请选择各功能使用的账号。', 100)
    return {'account_id': key}
