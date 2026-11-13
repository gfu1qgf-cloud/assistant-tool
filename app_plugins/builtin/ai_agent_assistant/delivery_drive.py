"""Bounded Drive enumeration and verified, no-overwrite media downloads."""
import hashlib
import http.cookiejar
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, build_opener
from uuid import uuid4

from model.AppDataStorage import data_directory, data_file, data_io
from model.GoogleDriveDownloader import (GoogleDriveRedirectHandler, allowed_download_url, parse_drive_link,
    export_url, open_download_response, filename_from_headers, safe_filename)
from model.PrivateDataCatalog import no_reparse

FOLDER = 'application/vnd.google-apps.folder'
VIDEO = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'}
URL = re.compile(r'https://(?:drive\.google\.com|drive\.usercontent\.google\.com|docs\.google\.com)/[^\s<>"\'，；。！？（）【】]+')


def error_message(error):
    status = getattr(getattr(error, 'resp', None), 'status', None)
    uri = str(getattr(error, 'uri', '') or '')
    if '/drive/' in uri:
        if status == 404:
            return 'Google Drive返回404：文件／文件夹不存在，或当前“AI Agent · 成品下载”账号无权访问。请核实真实链接及共享权限；稍后自动重试，不会误报已下载。'
        if status == 403:
            return 'Google Drive拒绝下载：请检查成品共享／下载权限、下载配额及该账号项目是否启用Drive API；稍后自动重试。'
        if status == 429: return 'Google Drive请求过于频繁，稍后自动重试。'
    return str(error)[:700]


def result_links(text):
    links = list(dict.fromkeys(match.group().rstrip(').,;') for match in URL.finditer(str(text))))
    if not links: raise ValueError('成品栏未找到完整的 Google Drive HTTPS 链接。')
    if len(links) > 50: raise ValueError('单任务成品链接超过50个，请分批交付。')
    for link in links:
        if not allowed_download_url(link): raise ValueError('成品链接不是受支持的安全 Google 地址。')
    return links


def _id(link):
    match = re.search(r'/folders/([A-Za-z0-9_-]+)', urlsplit(link).path)
    return (match.group(1), True) if match else (parse_drive_link(link).file_id, False)


def collect_files(links, drive, run, *, root_only=False):
    files, visited = {}, set()
    def visit(file_id, link, depth=0, known=None):
        run.check_canceled()
        if depth > 5 or len(visited) >= 1000: raise ValueError('成品文件夹过深或项目超过1000个，已停止本任务以免无限遍历。')
        if file_id in visited: return
        visited.add(file_id)
        if drive is None:
            if _id(link)[1]: raise ValueError('文件夹成品需要 Drive 读取授权，请在 Google账号中分配“AI Agent · 成品下载”。')
            parsed = parse_drive_link(link)
            if parsed.google_type: raise ValueError('成品必须是视频文件，不是 Google 文档。')
            files[file_id] = {'id': file_id, 'url': link, 'name': '', 'size': None, '_depth': max(0, depth - 1)}
            return
        if known is None:
            request = drive.files().get(fileId=file_id, supportsAllDrives=True,
                fields='id,name,mimeType,size,md5Checksum,trashed,capabilities(canDownload)')
            from urllib.parse import parse_qs
            key = (parse_qs(urlsplit(link).query).get('resourcekey') or [''])[0]
            if key: request.headers['X-Goog-Drive-Resource-Keys'] = file_id + '/' + key
            metadata = request.execute(num_retries=0)
        else: metadata = known
        if metadata.get('id') != file_id or metadata.get('trashed'): raise ValueError('成品文件身份无效或已经删除。')
        if metadata.get('mimeType') == FOLDER:
            if root_only and depth > 0: return
            token, pages = None, set()
            while True:
                run.check_canceled()
                if token in pages: raise ValueError('Drive 返回重复分页，已停止本任务。')
                pages.add(token)
                page = drive.files().list(q=f"'{file_id}' in parents and trashed=false", pageSize=100,
                    pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True, orderBy='name',
                    fields='nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,trashed,capabilities(canDownload))').execute(num_retries=0)
                if page.get('incompleteSearch'): raise ValueError('Drive文件夹检索结果不完整，稍后重试，未把部分结果当作全部成品。')
                for child in page.get('files', []):
                    child_id = str(child.get('id') or '')
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', child_id): raise ValueError('Drive 返回无效成品ID。')
                    visit(child_id, f'https://drive.google.com/file/d/{child_id}/view', depth + 1, child)
                token = page.get('nextPageToken')
                if not token: break
        elif metadata.get('mimeType') == 'application/vnd.google-apps.shortcut':
            raise ValueError('成品文件夹包含Drive快捷方式，请提供实际视频文件或其直接链接，未静默漏掉该项。')
        elif str(metadata.get('mimeType', '')).startswith('video/') or Path(metadata.get('name', '')).suffix.lower() in VIDEO:
            if metadata.get('capabilities', {}).get('canDownload') is False: raise ValueError('成品禁止下载，请调整文件共享／下载权限。')
            files[file_id] = {**metadata, 'url': link, '_depth': max(0, depth - 1)}
        if len(files) > 500: raise ValueError('单任务超过500个视频，请拆分成品文件夹。')
    for link in links:
        file_id, folder = _id(link)
        if folder and drive is None: raise ValueError('文件夹成品需要 Drive 读取授权，请在 Google账号中分配“AI Agent · 成品下载”。')
        visit(file_id, link)
    if not files: raise ValueError('成品文件夹暂时没有可下载的视频，稍后自动重试。')
    return list(files.values())


def output_directory(job, *, supplemental=False):
    raw = Path(str(job['payload'].get('target_dir') or ''))
    if not raw.is_absolute(): raise ValueError('原任务目录不是绝对路径，未下载到其他目录。')
    root = no_reparse(raw, Path(raw.anchor))
    if not root.is_dir(): raise ValueError('提交时的原任务目录不可用；请恢复磁盘／目录，任务会自动重试。')
    directory = no_reparse(root/('Agent附加素材' if supplemental else 'result'), root)
    data_directory(directory).ensure()
    return directory


def _digest(path, run, algorithm='sha256'):
    checksum = hashlib.new(algorithm)
    with data_file(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            run.check_canceled(); checksum.update(chunk)
    return checksum.hexdigest()


def verify_video(path):
    from model.node_workflow.render import probe, resolve_tools
    return probe(path, resolve_tools()[1])


def download_file(job, item, directory, drive, run, *, verify=verify_video):
    """Publish only complete verified bytes; existing unrelated files never replaced."""
    response = None
    size = int(item['size']) if item.get('size') is not None else None
    if size is not None and (size <= 0 or size > 20 * 1024**3): raise ValueError('成品大小无效或超过20GiB，未下载。')
    if shutil.disk_usage(directory).free < (size or 0) + 64 * 1024**2: raise OSError('任务磁盘空间不足，稍后重试。')
    name = item.get('name') or ''
    link = parse_drive_link(item['url'])
    request = None
    if drive is not None:
        request = drive.files().get_media(fileId=item['id'], supportsAllDrives=True)
        if link.resource_key: request.headers['X-Goog-Drive-Resource-Keys'] = item['id'] + '/' + link.resource_key
    else:
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()), GoogleDriveRedirectHandler())
        response = open_download_response(opener, export_url(link, {}), 30)
        name = filename_from_headers(response.headers) or item['id'] + '.mp4'
        length = response.headers.get('Content-Length')
        size = int(length) if length and length.isdigit() else None
        if size is not None and (size <= 0 or size > 20 * 1024**3 or shutil.disk_usage(directory).free < size + 64 * 1024**2):
            response.close(); raise OSError('成品大小无效、超过20GiB或任务磁盘空间不足。')
    name = safe_filename(name or item['id'] + '.mp4', item['id'] + '.mp4')[0]
    if Path(name).suffix.lower() not in VIDEO:
        if response: response.close()
        raise ValueError('成品不是受支持的视频文件，未把网页或文档当成视频。')
    identity = item['id'] + '\0' + str(item.get('md5Checksum') or '')
    prefix = f"Agent_{job['id'][:12]}_{hashlib.sha256(identity.encode()).hexdigest()[:12]}_"
    suffix = Path(name).suffix
    name = Path(name).stem[:120] + suffix
    target = no_reparse(directory/(prefix + name), directory)
    pending = directory/(prefix + uuid4().hex + '.pending' + suffix)
    deadline = time.monotonic() + 600
    written = 0
    try:
        with data_file(pending).atomic_writer(binary=True) as stream:
            if request is not None:
                from googleapiclient.http import MediaIoBaseDownload
                downloader = MediaIoBaseDownload(stream, request, chunksize=1024 * 1024)
                done = False
                while not done:
                    run.check_canceled()
                    if time.monotonic() > deadline: raise TimeoutError('单文件下载超过10分钟，稍后自动重试。')
                    progress, done = downloader.next_chunk(num_retries=0)
                    if stream.tell() > 20 * 1024**3: raise ValueError('成品超过20GiB，已停止。')
                    if progress: run.report('收取 Agent 成品：' + name, int(90 * progress.progress()))
            else:
                while True:
                    run.check_canceled()
                    if time.monotonic() > deadline: raise TimeoutError('单文件下载超过10分钟，稍后自动重试。')
                    block = response.read(1024 * 1024)
                    if not block: break
                    written += len(block)
                    if written > 20 * 1024**3: raise ValueError('成品超过20GiB，已停止。')
                    stream.write(block)
        run.check_canceled()
        actual_size = pending.stat().st_size
        if actual_size == 0 or size is not None and actual_size != size: raise ValueError('成品长度校验失败，未标记已下载。')
        actual_md5 = _digest(pending, run, 'md5')
        if item.get('md5Checksum') and actual_md5 != item['md5Checksum']: raise ValueError('Drive成品校验失败，稍后重新下载。')
        verify(pending)
        sha256 = _digest(pending, run)
        if not item.get('md5Checksum'):
            # Public downloads cannot identify remote revisions in advance.
            # Content identity lets a forced re-collection preserve both versions.
            no_reparse(target, directory)
            # Also reuse equal files published before content-based filenames.
            if not target.is_file() or _digest(target, run) != sha256:
                target = no_reparse(directory/(prefix + sha256[:16] + '_' + name), directory)
        run.check_canceled(); no_reparse(target, directory)
        try:
            if os.name == 'nt': data_io.rename(pending, target)
            else: data_io.link(pending, target); data_file(pending).remove()
        except FileExistsError:
            if not target.is_file() or _digest(target, run) != sha256: raise ValueError('成品目标已存在且内容不同，未覆盖原文件。') from None
        return {'state': 'downloaded', 'path': str(target), 'size': actual_size, 'sha256': sha256,
                'md5': actual_md5, 'attempts': 0, 'retry_at': 0}
    finally:
        if response: response.close()
        if pending.exists(): data_file(pending).remove()


def cached_file(record, directory):
    if record.get('state') != 'downloaded': return False
    path = Path(str(record.get('path') or ''))
    if path.parent != directory: return False
    no_reparse(path, directory)
    return path.is_file() and path.stat().st_size == record.get('size') and record.get('size', 0) > 0
