"""Noninteractive optional auth for the CORE public-first reference downloader."""
import hashlib
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from model.AppDataStorage import application_storage, data_file, data_io
from model.GoogleDriveDownloader import DownloadError, safe_filename, normalize_jfif_filename, jfif_content_extension


class ReferenceAccessError(DownloadError): pass
class ReferenceCredentialError(ReferenceAccessError): pass


def load_legacy_reference_service(token_file):
    """Read an existing legacy credential; never open OAuth or save plain copies."""
    from model.OfflineMode import require_online
    require_online('参考文件授权下载')
    from model.GoogleAccounts import validate_token
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    import httplib2
    value = validate_token(token_file.read_json())
    credentials = Credentials.from_authorized_user_info(value)
    if not credentials.valid:
        try:
            def refresh(*args, **kwargs): kwargs['timeout'] = 30; return Request()(*args, **kwargs)
            credentials.refresh(refresh)
        except Exception: raise ReferenceAccessError('主程序已有的Drive授权无法续期，请在账号设置修复；后台不会重新登录或改用插件账号。') from None
        # Do not create refreshed plaintext copies of a legacy token.
    return build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def _credential_identity(value):
    return hashlib.sha256((str(value.get('client_id') or '') + '\0' + str(value.get('refresh_token') or '')).encode()).hexdigest()


class ReferenceAccountPool:
    """All saved Drive-capable credentials, lazy services, unchanged bindings."""
    def __init__(self, *, manager=None, legacy_files=None, log=lambda _text: None):
        from model.GoogleAccounts import GoogleAccountManager, load_drive_account_service, token_scopes
        self.manager = manager or GoogleAccountManager()
        if legacy_files is None:
            # Reuse the manager's idempotent encrypted import, including its
            # removal tombstones. Never resurrect a removed account by scanning
            # the still-preserved legacy Token files behind the manager's back.
            self.manager.integrate_legacy()
            legacy_files = []
        self.log, self.preferred, self.services, self.invalid, self.candidates = log, None, {}, {}, []
        self.denied = {}
        identities = set(); allowed = {'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/drive.file'}
        for account in self.manager.snapshot()['accounts']:
            if not allowed.intersection(account.get('scopes', [])): continue
            value = self.manager.account(account['id'])['credentials']
            identity = _credential_identity(value)
            if identity in identities: continue
            identities.add(identity); key = account['id']
            self.candidates.append((key, lambda key=key: load_drive_account_service(key, manager=self.manager)))
        for index, file in enumerate(legacy_files):
            try: value = file.read_json(); scopes = token_scopes(value)
            except ValueError:
                self.log('一个旧Google凭据文件内容无效，已略过；不覆盖原文件。'); continue
            if not allowed.intersection(scopes) and not (file.path.name == 'GoogleDriveToken.json' and not scopes): continue
            identity = _credential_identity(value)
            if identity in identities: continue
            identities.add(identity)
            self.candidates.append(('legacy-' + str(index), lambda file=file: load_legacy_reference_service(file)))

    def download(self, link, directory, *, canceled=lambda: False, export_formats=None):
        candidates = sorted(self.candidates, key=lambda item: item[0] != self.preferred)
        denied = self.denied.setdefault(link.file_id, set())
        tried = 0
        for key, loader in candidates:
            if canceled(): raise InterruptedError('参考文件下载已取消。')
            if key in self.invalid or key in denied: continue
            if key not in self.services:
                try: self.services[key] = loader()
                except Exception:
                    self.invalid[key] = True
                    self.log('一个已保存的Google凭据暂不可用，继续检查其他已有凭据；未改变任何账号绑定。')
                    continue
            tried += 1
            try: result = download_reference(self.services[key], link, directory, canceled=canceled, export_formats=export_formats)
            except ReferenceCredentialError:
                self.invalid[key] = True; continue
            except ReferenceAccessError:
                denied.add(key); continue
            else:
                self.preferred = key
                return result
        raise ReferenceAccessError(f'此参考文件需要登录；已检查 {len(candidates)} 个已有Drive凭据，仍无法访问。请核对链接和共享／下载权限；未改动任何插件账号绑定，也不会继续盲目重试。')


def _hash(path, canceled):
    # Drive exposes MD5 as a transfer checksum, not as an authenticity proof.
    value = hashlib.md5(usedforsecurity=False)
    with data_file(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            if canceled(): raise InterruptedError('参考文件下载已取消。')
            value.update(chunk)
    return value.hexdigest()


def download_reference(service, link, directory, *, canceled=lambda: False, export_formats=None):
    """One API attempt, verified bytes, no overwrite and no interactive OAuth."""
    directory = Path(directory)
    pending = None
    try:
        if link.resource_key and not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', link.resource_key): raise ReferenceAccessError('参考链接的resourcekey格式无效。')
        request = service.files().get(fileId=link.file_id, supportsAllDrives=True,
            fields='id,name,mimeType,size,md5Checksum,trashed,capabilities(canDownload)')
        if link.resource_key: request.headers['X-Goog-Drive-Resource-Keys'] = link.file_id + '/' + link.resource_key
        metadata = request.execute(num_retries=0)
        if metadata.get('id') != link.file_id or metadata.get('trashed'): raise ReferenceAccessError('参考文件不存在或已删除。')
        if metadata.get('mimeType') == 'application/vnd.google-apps.folder': raise ReferenceAccessError('参考链接指向文件夹，请提供文件直接链接。')
        if metadata.get('capabilities', {}).get('canDownload') is False: raise ReferenceAccessError('此参考文件禁止下载，请检查共享／下载权限。')
        name = safe_filename(metadata.get('name') or link.file_id, link.file_id)[0]
        name, jfif = normalize_jfif_filename(name)
        expected_size = int(metadata['size']) if metadata.get('size') is not None else None
        if link.google_type:
            fmt = (export_formats or {}).get(link.google_type)
            mime = {'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'pdf': 'application/pdf'}.get(fmt)
            if not mime: raise ReferenceAccessError('此Google文档导出格式不受支持。')
            if not name.lower().endswith('.' + fmt): name += '.' + fmt
            media = service.files().export_media(fileId=link.file_id, mimeType=mime); expected_size = None
        else: media = service.files().get_media(fileId=link.file_id, supportsAllDrives=True)
        if link.resource_key: media.headers['X-Goog-Drive-Resource-Keys'] = link.file_id + '/' + link.resource_key
        if expected_size is not None and (expected_size <= 0 or expected_size > 20 * 1024**3): raise ReferenceAccessError('参考文件大小无效或超过20GiB。')
        pending = directory/('.reference-' + uuid4().hex + '.part')
        from googleapiclient.http import MediaIoBaseDownload
        deadline = time.monotonic() + 600
        with data_file(pending).atomic_writer(binary=True) as stream:
            downloader = MediaIoBaseDownload(stream, media, chunksize=1024 * 1024)
            done = False
            while not done:
                if canceled(): raise InterruptedError('参考文件下载已取消。')
                if time.monotonic() > deadline: raise TimeoutError('参考文件下载超过10分钟，已停止本次尝试。')
                _progress, done = downloader.next_chunk(num_retries=0)
                if stream.tell() > 20 * 1024**3: raise ReferenceAccessError('参考文件超过20GiB。')
        size = pending.stat().st_size
        if not size or expected_size is not None and size != expected_size: raise DownloadError('参考文件长度校验失败，未归档。')
        checksum = _hash(pending, canceled)
        if not link.google_type and metadata.get('md5Checksum') and checksum != metadata['md5Checksum']: raise DownloadError('参考文件校验失败，未归档。')
        if jfif: name = str(Path(name).with_suffix(jfif_content_extension(pending)))
        target = directory/name
        if target.exists() and _hash(target, canceled) == checksum: return 'skipped', target
        if target.exists(): target = directory/(Path(name).stem[:120] + '-' + hashlib.sha256((link.file_id + checksum).encode()).hexdigest()[:16] + Path(name).suffix)
        if canceled(): raise InterruptedError('参考文件下载已取消。')
        try:
            if os.name == 'nt': data_io.rename(pending, target)
            else: data_io.link(pending, target); data_file(pending).remove()
        except FileExistsError:
            if _hash(target, canceled) != checksum: raise ReferenceAccessError('参考文件同名内容冲突，未覆盖。') from None
            return 'skipped', target
        return 'downloaded', target
    except Exception as error:
        from google.auth.exceptions import RefreshError
        if isinstance(error, RefreshError) and not getattr(error, 'retryable', False):
            raise ReferenceCredentialError('此Google凭据已失效，将尝试其他已有Drive凭据；不重新登录。') from None
        status = getattr(getattr(error, 'resp', None), 'status', None)
        if status == 401: raise ReferenceCredentialError('此Google凭据未通过验证，将尝试其他已有凭据。') from None
        if status in {400, 401, 403, 404}:
            raise ReferenceAccessError('主程序Drive授权仍无法读取此参考文件（Google返回' + str(status) + '）；请检查链接和该账号的访问／下载权限。本文件不再盲目重试。') from None
        if status is not None: raise DownloadError('Google Drive暂时无法下载参考文件（' + str(status) + '）。') from None
        if isinstance(error, (DownloadError, OSError)): raise
        raise DownloadError('参考文件授权下载暂时失败：' + type(error).__name__) from None
    finally:
        if pending is not None and pending.exists(): data_file(pending).remove()
