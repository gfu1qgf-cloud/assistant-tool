"""Independent Docs OAuth token, broker storage and cancelable loopback login."""
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from model.AppDataStorage import data_directory, data_file
from model.OfflineMode import require_online

SCOPES = ['https://www.googleapis.com/auth/documents']


def client_config(value):
    installed = value.get('installed') if isinstance(value, dict) else None
    if (not isinstance(installed, dict) or not installed.get('client_id') or not installed.get('client_secret')
            or installed.get('auth_uri') not in {'https://accounts.google.com/o/oauth2/auth', 'https://accounts.google.com/o/oauth2/v2/auth'}
            or installed.get('token_uri') != 'https://oauth2.googleapis.com/token'):
        raise ValueError('请选择Google Cloud下载的“桌面应用”OAuth客户端JSON；不支持服务账号或其他登录服务器。')
    return {'installed': copy.deepcopy(installed)}


def configure_client(source, private_root):
    with data_file(source).open('r', encoding='utf-8-sig') as handle:
        import json
        value = json.loads(handle.read(262145))
    value = client_config(value)
    data_directory(private_root).file('google_client.json').write_json(value)


def authorize(private_root, run):
    require_online('Google Docs 授权')
    from google_auth_oauthlib.flow import InstalledAppFlow
    root = data_directory(private_root)
    client = root.file('google_client.json')
    if not client.exists(): raise ValueError('请先选择Google桌面应用OAuth客户端JSON。不会改动原有表格或网盘授权。')
    flow = InstalledAppFlow.from_client_config(client_config(client.read_json()), SCOPES)
    received, expected_state = [], ['']
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args): pass  # Authorization codes must never enter logs.
        def do_GET(self):
            parts = urlsplit(self.path); values = parse_qs(parts.query)
            valid = parts.path == '/' and values.get('state') == [expected_state[0]] and bool(values.get('code') or values.get('error'))
            self.send_response(200 if valid else 400); self.send_header('Content-Type', 'text/plain; charset=utf-8'); self.end_headers()
            self.wfile.write(('已收到授权结果，可以关闭此页面。' if valid else '无效的授权回调。').encode('utf-8'))
            if valid: received.append(self.path)
    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = 0.5
        flow.redirect_uri = f'http://127.0.0.1:{server.server_port}/'
        url, state = flow.authorization_url(access_type='offline', prompt='consent')
        expected_state[0] = state
        run.report('请在浏览器中完成Google文档授权；可取消，最多等待3分钟。', 10)
        if not webbrowser.open(url): raise RuntimeError('无法打开授权浏览器，请检查系统默认浏览器。')
        deadline = time.monotonic() + 180
        while not received:
            run.check_canceled()
            root.storage.check(root.path)
            if time.monotonic() > deadline: raise TimeoutError('Google授权等待超时；原有密钥和授权未改变。')
            server.handle_request()
    run.check_canceled()
    root.storage.check(root.path, write=True)
    if parse_qs(urlsplit(received[0]).query).get('error'): raise RuntimeError('Google授权被拒绝，未保存新令牌。')
    # Same loopback-response normalization used by the official OAuth SDK.
    callback = f'https://127.0.0.1:{server.server_port}{received[0]}'
    try: flow.fetch_token(authorization_response=callback, timeout=45)
    except Exception: raise RuntimeError('Google授权兑换失败；请重新授权。授权码与密钥未写入日志。') from None
    run.check_canceled()
    root.file('google_token.json').write_text(flow.credentials.to_json())
    return {'kind': 'auth', 'paths': []}


def load_service(private_root):
    require_online('Google Docs 请求')
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    root = data_directory(private_root); token = root.file('google_token.json')
    if not token.exists(): raise ValueError('请先点击“Google授权”。公开分享链接不等于API编辑授权。')
    value = token.read_json()
    if value.get('token_uri') != 'https://oauth2.googleapis.com/token': raise ValueError('Google授权服务器无效，请重新授权。')
    declared = value.get('scopes', [])
    if isinstance(declared, str): declared = declared.split()
    if not isinstance(declared, list) or not set(SCOPES).issubset(declared):
        raise ValueError('当前令牌不包含Docs编辑权限，请重新Google授权；不会尝试扩张旧令牌权限。')
    client = root.file('google_client.json')
    if client.exists() and value.get('client_id') != client_config(client.read_json())['installed']['client_id']:
        raise ValueError('Docs客户端已更换，请重新Google授权；旧令牌未删除。')
    credentials = Credentials.from_authorized_user_info(value, SCOPES)
    if not credentials.valid:
        if not credentials.refresh_token: raise ValueError('Google授权已失效，请重新授权。')
        def request(*args, **kwargs):
            kwargs['timeout'] = 45
            return Request()(*args, **kwargs)
        try: credentials.refresh(request)
        except Exception: raise RuntimeError('Google授权刷新失败，请重新授权；不会改动其他服务的密钥。') from None
        token.write_text(credentials.to_json())
    return build('docs', 'v1', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=45)), cache_discovery=False)
