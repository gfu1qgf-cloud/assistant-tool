"""Read-only Chrome extension inspection and per-profile setup validation."""
import json
import base64
import hashlib
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

EXTENSION_ID = 'pnjppjjfgiccpcfajkpooklgohgihdom'
EXTENSION_VERSION = '1.1.27'
# Reviewed bridge releases with the same protocol and no automatic page reload.
# A patch update is available, but is not a reason to disable a working client.
def compatible_extension(version, *, minimum_patch=8):
    match = re.fullmatch(r'1\.1\.(0|[1-9][0-9]*)', str(version or ''))
    current = re.fullmatch(r'1\.1\.(0|[1-9][0-9]*)', EXTENSION_VERSION)
    return bool(match and current and minimum_patch <= int(match[1]) <= int(current[1]))
OPTIONS_URL = f'chrome-extension://{EXTENSION_ID}/options.html'
EXTENSIONS_URL = 'chrome://extensions/'


def setup_project_entry(pairing, entry='', *, action=''):
    """Chrome receives HTTPS only; extension pages open through extension APIs."""
    project = project_url(pairing.get('project'), required=True)
    args = {}
    if entry and project_identity(entry) == project_identity(project):
        args = parse_qs(urlsplit(entry).fragment)
    ticket = args.get('lzx_setup', [pairing.get('setup_id', '')])[0]
    try: port = int(args.get('lzx_port', [pairing.get('port', 0)])[0])
    except (TypeError, ValueError): port = 0
    action = action or args.get('lzx_action', [''])[0]
    expected = pairing.get('setup_id')
    if (not re.fullmatch(r'[a-f0-9]{32}', str(ticket)) or not 1024 <= port <= 65535
            or action not in {'', 'options', 'update'} or expected and ticket != expected):
        raise ValueError('缺少有效的自动连接入口，请正常重启辅助工具后重新连接；不会打开空白标签页。')
    values = {'lzx_setup': ticket, 'lzx_port': port}
    if action: values['lzx_action'] = action
    return project + '#' + urlencode(values)


def profile_name(value):
    value = str(value or '').strip()
    if not value or len(value) > 100 or value in {'.', '..'} or re.search(r'[\\/:"<>|?*\x00-\x1f]', value):
        raise ValueError('请选择一个有效的 Chrome 配置目录。')
    return value


def project_url(value, *, required=False):
    value = str(value or '').strip()
    if not value and not required: return ''
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in {'labs.google', 'flow.google.com'}
                or url.username or url.password or url.port not in (None, 443)
                or not re.search(r'/project/[A-Za-z0-9_-]+/?$', url.path)):
            raise ValueError()
    except ValueError:
        raise ValueError('请手动填写这个 Chrome 配置的 Flow 项目地址（打开项目后复制网址）。') from None
    return urlunsplit(('https', url.hostname, url.path.rstrip('/'), url.query, ''))


def project_identity(value):
    try:
        url = urlsplit(project_url(value, required=True))
        return url.hostname, url.path
    except ValueError:
        return None


def project_block(client, preferences, *, include_pending=True):
    if client.get('extension_version') and not compatible_extension(client['extension_version']):
        return f'桥接扩展需更新到 {EXTENSION_VERSION}：浏览器空闲后在扩展管理页点“重新加载”，再刷新 Flow 项目页；无需重新配对。'
    saved = project_identity(client.get('saved_project'))
    if not saved: return '未保存 Flow 项目地址，请到“连接向导”手动添加。'
    expected = project_identity(preferences.get('profile_projects', {}).get(client.get('profile')))
    if expected and expected != saved:
        return '项目地址尚未同步到此浏览器，请在连接向导完成配对保存。'
    current = project_identity(client.get('project'))
    if current and current != (expected or saved):
        return '当前网页不是此配置保存的项目，请打开正确项目后刷新连接。'
    if include_pending and client.get('setup_pending'):
        return '扩展正在后台自动确认本配置；无需手动打开网页或复制配对信息。'
    return ''


def inspect_extension(root, profile):
    """Preferences are hints, not a live connection check. Never write them."""
    profile = profile_name(profile)
    if not str(root or '').strip() or not Path(root).is_absolute():
        raise ValueError('尚未确认 Chrome 用户数据目录。')
    root = Path(root).resolve()
    directory = (root / profile).resolve()
    if directory.parent != root:
        raise ValueError('Chrome 配置目录超出用户数据目录。')
    result = {'profile': profile, 'status': 'unknown', 'version': '', 'reason': '无法确认安装状态；打开扩展页检查。'}
    records, parsed, unreadable = [], 0, False
    for name in ('Preferences', 'Secure Preferences'):
        path = directory / name
        if not path.is_file(): continue
        try:
            with path.open('rb') as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024: raise ValueError('preference limit')
            data = json.loads(raw.decode('utf-8-sig'))
            settings = data.get('extensions', {}).get('settings', {})
            if not isinstance(settings, dict): raise ValueError('preference schema')
            parsed += 1
            record = settings.get(EXTENSION_ID)
            if isinstance(record, dict): records.append(record)
        except (OSError, ValueError, TypeError, AttributeError):
            unreadable = True
    if records:
        versions = {str(row['manifest']['version']) for row in records
                    if isinstance(row.get('manifest'), dict) and row['manifest'].get('version')}
        result['version'] = next(iter(versions)) if len(versions) == 1 else ''
        states = {str(row['state']) for row in records if row.get('state') is not None}
        if len(states) > 1 or len(versions) > 1:
            result['reason'] = '检测到相互矛盾的安装记录，请在此配置的扩展页确认。'
            return result
        if any(row.get('state') == 0 or row.get('disable_reasons') for row in records):
            result.update(status='disabled', reason='检测到安装记录，但扩展被停用；请在扩展页启用。')
        elif all(row.get('state') == 1 for row in records):
            result.update(status='enabled', reason='检测到已启用的安装记录；尚未连接不等于没安装。')
        # Chrome may omit both state and manifest for unpacked extensions.
        # Check the exact recorded path and identity, then let the live
        # handshake confirm readiness. Missing flags do not prove disabled.
        if result['status'] != 'disabled' and all(row.get('state') in (None, 1) for row in records):
            disk_versions = set()
            for row in records:
                path = Path(str(row.get('path') or ''))
                if not str(row.get('path') or '') or str(path).startswith(('\\\\', '//')):
                    continue
                if not path.is_absolute(): path = directory / 'Extensions' / path
                try:
                    with (path / 'manifest.json').open('rb') as stream:
                        raw = stream.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024: continue
                    manifest = json.loads(raw.decode('utf-8-sig'))
                    key = base64.b64decode(manifest.get('key', ''), validate=True)
                    identifier = ''.join(chr(ord('a') + int(c, 16)) for c in hashlib.sha256(key).hexdigest()[:32])
                    if key and identifier == EXTENSION_ID:
                        disk_versions.add(str(manifest.get('version') or ''))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            if len(disk_versions) == 1:
                result['version'] = next(iter(disk_versions))
                result['can_launch'] = compatible_extension(result['version'])
                result['reason'] = '已核对本配置安装路径及扩展身份；启动后等待扩展在线确认。'
    elif parsed and not unreadable and not (directory / 'Extensions' / EXTENSION_ID).exists():
        result.update(status='missing', reason='本地记录中未检测到桥接扩展；若刚安装，可能尚未落盘。')
    return result
