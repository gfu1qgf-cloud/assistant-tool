"""Read-only Drive browser service. Private records use host storage, never cache."""
from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from model.AppDataStorage import application_storage, data_directory, data_file
from model.DrivePreviewCache import DrivePreviewCache
from model.GoogleDriveHelper import CLIENT_SECRET_FILE, TOKEN_FILE, escape_query_text
from model.OfflineMode import require_online

READONLY = 'https://www.googleapis.com/auth/drive.readonly'
FULL = 'https://www.googleapis.com/auth/drive'
FOLDER = 'application/vnd.google-apps.folder'
SHORTCUT = 'application/vnd.google-apps.shortcut'
FIELDS = ('id,name,mimeType,size,modifiedTime,version,resourceKey,parents,'
          'capabilities(canDownload),imageMediaMetadata(width,height),'
          'videoMediaMetadata(width,height,durationMillis),'
          'shortcutDetails(targetId,targetMimeType,targetResourceKey)')
AUTH_LOCK = threading.RLock()


def private_root():
    storage = application_storage()
    if not storage.protected:
        raise ValueError('谷歌网盘收藏与授权必须保存在已验证的密盘。请先启用主程序VC数据存储。')
    storage.check()
    return storage.for_plugin('google_drive_browser')


def target(value):
    text = str(value or '').strip()
    if text in {'root', 'shared'}: return {'id': text, 'resourceKey': ''}
    parsed = urlsplit(text)
    if (parsed.scheme != 'https' or parsed.hostname not in {'drive.google.com', 'docs.google.com'}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError('请输入HTTPS Google Drive文件／文件夹分享链接。')
    query = parse_qs(parsed.query)
    if parsed.hostname == 'drive.google.com':
        if parsed.path.endswith('/my-drive'): return {'id': 'root', 'resourceKey': ''}
        if parsed.path.endswith('/shared-with-me'): return {'id': 'shared', 'resourceKey': ''}
    match = re.search(r'/(?:folders|file/d|document/d|spreadsheets/d|presentation/d)/([A-Za-z0-9_-]+)(?:/|$)', parsed.path)
    file_id = match.group(1) if match else (query.get('id') or [''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', file_id): raise ValueError('链接中没有有效的网盘文件ID。')
    key = (query.get('resourcekey') or [''])[0]
    if key and not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', key): raise ValueError('网盘资源密钥格式无效。')
    return {'id': file_id, 'resourceKey': key}


def item_link(item):
    file_id = item['id']
    if file_id == 'root': return 'https://drive.google.com/drive/my-drive'
    if file_id == 'shared': return 'https://drive.google.com/drive/shared-with-me'
    path = 'drive/folders/' + file_id if item.get('mimeType') == FOLDER else 'file/d/' + file_id + '/view'
    return 'https://drive.google.com/' + path + ('?' + urlencode({'resourcekey': item['resourceKey']}) if item.get('resourceKey') else '')


def read_bookmarks():
    file = private_root().file('bookmarks.json')
    value = file.read_json() if file.exists() else {'version': 1, 'items': []}
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('items'), list):
        raise ValueError('网盘收藏记录格式错误，未重置原记录。')
    for item in value['items']:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str): raise ValueError('网盘收藏记录损坏。')
        target(item.get('url'))
    return copy.deepcopy(value['items'])


def snapshot_key(item, search='', order='name_natural', media='all'):
    return hashlib.sha256(json.dumps([item['id'], item.get('resourceKey', ''), search, order, media], ensure_ascii=False).encode()).hexdigest()


def save_snapshot(key, value):
    if not re.fullmatch('[a-f0-9]{64}', key): raise ValueError('目录缓存标识无效。')
    # Folder listings/filenames are sensitive too, unlike the image cache.
    private_root().directory('folders').file(key + '.json').write_json(value)


def load_snapshot(key):
    if not re.fullmatch('[a-f0-9]{64}', key): raise ValueError('目录缓存标识无效。')
    file = private_root().directory('folders').file(key + '.json')
    return file.read_json() if file.exists() else None


def bookmark(name, url, *, remove=False):
    parsed = target(url)
    if parsed['id'] in {'root', 'shared'}: raise ValueError('我的网盘／与我共享已有快捷入口，无需收藏。')
    with AUTH_LOCK:
        values = read_bookmarks()
        values = [item for item in values if target(item['url'])['id'] != parsed['id']]
        if not remove: values.append({'name': str(name).strip() or '未命名网盘收藏', 'url': str(url).strip()})
        private_root().file('bookmarks.json').write_json({'version': 1, 'items': values})
    return values


def client_config(value):
    item = value.get('installed') if isinstance(value, dict) else None
    if (not isinstance(item, dict) or not item.get('client_id') or not item.get('client_secret')
            or item.get('auth_uri') not in {'https://accounts.google.com/o/oauth2/auth', 'https://accounts.google.com/o/oauth2/v2/auth'}
            or item.get('token_uri') != 'https://oauth2.googleapis.com/token'):
        raise ValueError('请选择Google Cloud的桌面应用OAuth客户端JSON，不能使用服务账号。')
    return {'installed': copy.deepcopy(item)}


def configure_client(source):
    with data_file(source).open('r', encoding='utf-8-sig') as stream:
        value = json.loads(stream.read(262145))
    private_root().file('google_client.json').write_json(client_config(value))


def authorize(run):
    require_online('谷歌网盘浏览器授权')
    from google_auth_oauthlib.flow import InstalledAppFlow
    root = private_root(); selected = root.file('google_client.json')
    client = selected if selected.exists() else data_file(CLIENT_SECRET_FILE)
    if not client.exists(): raise ValueError('没有OAuth客户端，请在“网盘授权”菜单选择Google桌面应用客户端JSON。')
    flow = InstalledAppFlow.from_client_config(client_config(client.read_json()), [READONLY])
    received, state = [], ['']
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            parts = urlsplit(self.path); query = parse_qs(parts.query)
            valid = parts.path == '/' and query.get('state') == state and bool(query.get('code') or query.get('error'))
            self.send_response(200 if valid else 400); self.send_header('Content-Type', 'text/plain; charset=utf-8'); self.end_headers()
            self.wfile.write(('授权结果已收到，可以关闭页面。' if valid else '无效回调。').encode())
            if valid: received.append(self.path)
    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = .5
        flow.redirect_uri = f'http://127.0.0.1:{server.server_port}/'
        url, state[0] = flow.authorization_url(access_type='offline', prompt='consent')
        run.report('请在浏览器完成Google只读授权；可取消，最多等待3分钟。', 0)
        if not webbrowser.open(url): raise RuntimeError('系统无法打开授权浏览器。')
        deadline = time.monotonic() + 180
        while not received:
            run.check_canceled(); private_root()
            if time.monotonic() > deadline: raise TimeoutError('Google授权等待超时。')
            server.handle_request()
    run.check_canceled()
    if parse_qs(urlsplit(received[0]).query).get('error'): raise ValueError('授权已取消，原有网盘授权没有改变。')
    try: flow.fetch_token(authorization_response=f'https://127.0.0.1:{server.server_port}{received[0]}', timeout=30)
    except Exception: raise RuntimeError('Google授权兑换失败，请重新授权。密钥和授权码不会写入日志。') from None
    with AUTH_LOCK: private_root().file('google_token.json').write_text(flow.credentials.to_json())
    return {'kind': 'authorize'}


def load_credentials():
    require_online('谷歌网盘浏览器')
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    with AUTH_LOCK:
        root = private_root(); own = root.file('google_token.json')
        source = own if own.exists() else data_file(TOKEN_FILE)
        if not source.exists(): raise ValueError('尚无网盘授权，请点击“网盘授权 → Google只读授权”。')
        value = source.read_json()
        scopes = value.get('scopes', [])
        if isinstance(scopes, str): scopes = scopes.split()
        if value.get('token_uri') != 'https://oauth2.googleapis.com/token' or not {READONLY, FULL}.intersection(scopes):
            raise ValueError('现有授权不含网盘读取权限，请执行Google只读授权。不会扩张或覆盖上传授权。')
        credentials = Credentials.from_authorized_user_info(value, scopes=scopes)
        if not credentials.valid:
            if not credentials.refresh_token: raise ValueError('网盘登录失效，请重新只读授权。')
            try: credentials.refresh(lambda *args, **kwargs: Request()(*args, **{**kwargs, 'timeout': 30}))
            except Exception: raise RuntimeError('网盘授权刷新失败，请检查网络或重新只读授权。') from None
        # Copy a reusable token to this plugin's protected namespace only.
        own.write_text(credentials.to_json())
    account = hashlib.sha256((str(credentials.client_id) + '\0' + str(credentials.refresh_token)).encode()).hexdigest()
    return credentials, account


def safe_google_url(url):
    p = urlsplit(str(url))
    host = (p.hostname or '').lower()
    return (p.scheme == 'https' and not p.username and not p.password and p.port in {None, 443}
            and any(host == domain or host.endswith('.' + domain) for domain in ('googleapis.com', 'googleusercontent.com', 'google.com')))


class DriveBrowserService:
    def __init__(self, session, account, run):
        self.session, self.account, self.run = session, account, run

    @contextmanager
    def response(self, url, **options):
        from urllib.parse import urljoin
        require_online('谷歌网盘读取')
        response = None
        timeout = options.pop('timeout', (10, 30))
        try:
            for _ in range(5):
                self.run.check_canceled()
                if not safe_google_url(url): raise ValueError('网盘返回了非Google下载地址，已阻止发送授权。')
                try: response = self.session.get(url, timeout=timeout, stream=True, allow_redirects=False, **options)
                except Exception: raise RuntimeError('网盘连接失败，请检查网络后重试。') from None
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get('Location', '')); response.close(); response = None
                    options.pop('params', None); continue
                if response.status_code >= 400:
                    text = {401: '登录过期，请重新只读授权。', 403: '没有读取／下载权限，或网盘配额受限。',
                            404: '文件不存在，或当前Google账号没有权限。', 429: '网盘请求限流，请稍后重试。'}.get(response.status_code, 'Google网盘暂时无法读取，请稍后重试。')
                    raise ValueError(text)
                yield response
                return
            raise ValueError('网盘重定向次数过多。')
        finally:
            if response is not None: response.close()

    def request(self, suffix, *, params=None, item=None):
        headers = self.headers(item)
        with self.response('https://www.googleapis.com/drive/v3/' + suffix, params=params, headers=headers) as response:
            data = bytearray()
            for block in response.iter_content(65536):
                self.run.check_canceled(); data.extend(block)
                if len(data) > 4 * 1024**2: raise ValueError('网盘目录响应异常大，已停止读取。')
            try: return json.loads(data)
            except ValueError: raise ValueError('Google未返回有效目录数据。') from None

    @staticmethod
    def headers(item):
        if not item: return {}
        file_id, key = item.get('id', ''), item.get('resourceKey', '')
        if not re.fullmatch('[A-Za-z0-9_-]{1,256}', file_id): raise ValueError('网盘文件ID无效。')
        if key and not re.fullmatch('[A-Za-z0-9_-]{1,256}', key): raise ValueError('网盘资源密钥无效。')
        return {'X-Goog-Drive-Resource-Keys': file_id + '/' + key} if key else {}

    def get(self, item, _seen=()):
        self.headers(item)
        if item['id'] in _seen or len(_seen) > 8: raise ValueError('网盘快捷方式发生循环。')
        result = self.request('files/' + item['id'], params={'fields': FIELDS, 'supportsAllDrives': 'true'}, item=item)
        if item.get('resourceKey'): result.setdefault('resourceKey', item['resourceKey'])
        if result.get('mimeType') == SHORTCUT:
            details = result.get('shortcutDetails', {})
            return self.get({'id': details.get('targetId', ''), 'resourceKey': details.get('targetResourceKey', '')}, _seen + (item['id'],))
        return result

    def browse(self, item, *, page='', search='', order='name_natural', media='all'):
        if item['id'] not in {'root', 'shared'}:
            item = self.get(item)
            if item.get('mimeType') != FOLDER: return {'kind': 'file', 'item': item}
        query = ["trashed = false"]
        query.append('sharedWithMe = true' if item['id'] == 'shared' else f"'{escape_query_text(item['id'])}' in parents")
        if search.strip(): query.append("name contains '" + escape_query_text(search.strip()) + "'")
        if media in {'image', 'video'}:
            query.append("(mimeType = '" + FOLDER + "' or mimeType contains '" + media + "/' or mimeType = '" + SHORTCUT + "')")
        if order not in {'name_natural', 'modifiedTime desc', 'quotaBytesUsed desc'}: raise ValueError('未知排序方式。')
        params = {'q': ' and '.join(query), 'pageSize': 60, 'fields': 'nextPageToken,incompleteSearch,files(' + FIELDS + ')',
                  'orderBy': 'folder,' + order, 'spaces': 'drive', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
        if page: params['pageToken'] = page
        result = self.request('files', params=params, item=None if item['id'] == 'shared' else item)
        return {'kind': 'list', 'folder': item, 'items': result.get('files', []), 'next': result.get('nextPageToken', ''),
                'incomplete': bool(result.get('incompleteSearch'))}

    def thumbnail(self, item, cache):
        path = cache.path('thumbs', self.account, item)
        existing = cache.existing(path)
        if existing: return str(existing)
        mime = item.get('mimeType', '')
        if not mime.startswith(('image/', 'video/')): return ''
        if item.get('capabilities', {}).get('canDownload') is False: raise ValueError('所有者禁止读取原件，未使用谷歌裁切缩略图替代。')
        size = int(item.get('size') or 0)
        if mime.startswith('video/'):
            if size <= 0: raise ValueError('缺少视频大小，无法进行安全分段读取。')
            from model.DriveVideoFrames import extract_frame
            duration = float(item.get('videoMediaMetadata', {}).get('durationMillis') or 0) / 1000
            raw, used = extract_frame(size, lambda start, end: self.read_range(item, start, end), self.run.check_canceled,
                                      seconds=min(1, duration*.1))
        else:
            if size > 64 * 1024**2: raise ValueError('原图超过64MB，未自动下载。可手动打开完整预览。')
            data = bytearray()
            with self.response('https://www.googleapis.com/drive/v3/files/' + item['id'],
                               params={'alt': 'media', 'supportsAllDrives': 'true'}, headers=self.headers(item)) as response:
                for block in response.iter_content(65536):
                    self.run.check_canceled(); data.extend(block)
                    if len(data) > 64 * 1024**2: raise ValueError('原图超过64MB，缩略图准备已停止。')
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > 64_000_000: raise ValueError('原图超过6400万像素，未自动解码。')
                original.thumbnail((960, 960))
                image = ImageOps.exif_transpose(original).convert('RGB')
                output = io.BytesIO(); image.save(output, format='JPEG', quality=88); image.close()
            raw = output.getvalue()
        with cache.writer(path, len(raw)) as stream: stream.write(raw)
        return str(path)

    def read_range(self, item, start, end):
        headers = {**self.headers(item), 'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}
        with self.response('https://www.googleapis.com/drive/v3/files/' + item['id'],
                           params={'alt': 'media', 'supportsAllDrives': 'true'}, headers=headers, timeout=(8, 10)) as response:
            expected = f'bytes {start}-{end}/{item["size"]}'
            if response.status_code != 206 or response.headers.get('Content-Range') != expected:
                raise ValueError('网盘没有按范围返回视频数据；已停止，未下载完整视频。')
            data = bytearray()
            for block in response.iter_content(65536):
                self.run.check_canceled(); data.extend(block)
                if len(data) > end-start+1: raise ValueError('网盘分段响应超出请求长度。')
            return bytes(data)

    def media(self, item, cache):
        item = self.get(item)
        mime = item.get('mimeType', '')
        if not mime.startswith(('image/', 'video/')): raise ValueError('此文件不是可预览的图片或视频。')
        if item.get('capabilities', {}).get('canDownload') is False: raise ValueError('文件所有者禁止下载，无法生成本地完整预览。')
        total = int(item.get('size') or 0)
        if total <= 0: raise ValueError('网盘未返回有效文件大小，无法安全准备完整预览，请联网刷新目录。')
        limit = cache.limits['media']
        if total > limit: raise ValueError('文件超过当前预览缓存上限，请在网页中查看或调整预览缓存容量。')
        path = cache.path('media', self.account, item)
        if cache.existing(path): return item, str(path)
        with cache.writer(path, total) as stream:
            with self.response('https://www.googleapis.com/drive/v3/files/' + item['id'],
                               params={'alt': 'media', 'supportsAllDrives': 'true'}, headers=self.headers(item)) as response:
                expected = int(response.headers.get('Content-Length') or 0)
                if expected > limit: raise ValueError('文件超过预览容量上限。')
                written, updated = 0, 0.
                for block in response.iter_content(1024**2):
                    self.run.check_canceled(); written += len(block)
                    if written > limit: raise ValueError('文件超过预览容量上限，已停止下载。')
                    stream.write(block)
                    if time.monotonic() - updated > .25:
                        self.run.report(f'正在准备完整预览 · {written / 1024**2:.1f} MB', min(99, int(written*100/(total or expected or limit))))
                        updated = time.monotonic()
                if (total and written != total) or (expected and written != expected): raise ValueError('网盘预览下载不完整，请重试。')
        return item, str(path)


@contextmanager
def service(run):
    from google.auth.transport.requests import AuthorizedSession
    credentials, account = load_credentials()
    with AuthorizedSession(credentials) as session:
        yield DriveBrowserService(session, account, run)
