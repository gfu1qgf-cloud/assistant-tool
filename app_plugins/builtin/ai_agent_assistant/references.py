"""Local-image planning and confirmed, idempotent Drive uploads. No Qt or sharing writes."""
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4
import warnings
from contextlib import nullcontext

from app_plugins.api import load_google_service
from model.AppDataStorage import data_file
from model.PrivateDataCatalog import no_reparse
from .tasks import fingerprint
from .store import Store

IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif'}
MAX_BYTES = 64 * 1024**2


def folder_id(value):
    text = str(value or '').strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{10,160}', text): return text
    parts = urlsplit(text)
    match = re.fullmatch(r'/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,160})/?', parts.path)
    if parts.scheme == 'https' and parts.netloc == 'drive.google.com' and match: return match.group(1)
    raise ValueError('请填写有效的Drive素材文件夹链接或ID。')


def reference_sources(payload):
    sources, seen = [], set()
    for line in str(payload.get('reference') or '').splitlines():
        text = line.strip()
        if not text: continue
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}: text = text[1:-1]
        parts = urlsplit(text) if not re.match(r'^[A-Za-z]:[\\/]', text) else None
        if parts and parts.scheme == 'https' and parts.netloc in {'drive.google.com', 'docs.google.com'} and not re.search(r'\s', text):
            source = {'kind': 'link', 'value': text}
        else:
            if parts and parts.scheme == 'file':
                if parts.netloc not in {'', 'localhost'}: raise ValueError('本地素材不支持网络共享路径。')
                text = unquote(parts.path)
                if re.match(r'^/[A-Za-z]:/', text): text = text[1:]
            elif parts and parts.scheme: raise ValueError('素材仅支持Drive链接或本地图片文件，每行一项。')
            path = Path(text)
            if text.startswith(('\\\\', '//')) or Path(text).suffix.lower() not in IMAGES:
                raise ValueError('本地素材只支持图片文件；请每行填写一个文件路径或Drive链接。')
            if not path.is_absolute():
                root = Path(str(payload.get('target_dir') or ''))
                if not root.is_absolute() or '..' in path.parts: raise ValueError('相对图片路径必须位于原任务目录中。')
                path = root/path
            source = {'kind': 'local', 'value': os.path.abspath(path)}
        key = (source['kind'], os.path.normcase(source['value']) if source['kind'] == 'local' else source['value'])
        if key not in seen: sources.append(source); seen.add(key)
    return sources


def image_record(path, run, *, thumbnail=False, copy_to=None):
    path = no_reparse(Path(path), Path(path).anchor)
    if not path.is_file(): raise ValueError('找不到本地图片：' + str(path))
    before = path.stat()
    if not 0 < before.st_size <= MAX_BYTES: raise ValueError('本地图片为空或超过64MiB：' + path.name)
    # MD5 is required only to compare Drive's transfer checksum; SHA-256 is the
    # local content identity. Neither checksum authorizes a remote resource.
    sha, md5, total = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with data_file(path).open('rb') as stream, (data_file(copy_to).open('wb') if copy_to else nullcontext(None)) as output:
        while block := stream.read(1024 * 1024):
            run.check_canceled(); total += len(block)
            if total > MAX_BYTES: raise ValueError('本地图片读取时超过64MiB。')
            sha.update(block); md5.update(block)
            if output: output.write(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != before.st_size:
        raise ValueError('图片在读取期间改变，请重新确认：' + path.name)
    value = {'path': str(path), 'size': total, 'sha256': sha.hexdigest(), 'md5': md5.hexdigest(), 'name': path.name}
    if thumbnail:
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with data_file(path).open('rb') as stream, Image.open(stream) as image:
                if image.width * image.height > 40_000_000: raise ValueError('图片超过4000万像素，请缩小后再提交。')
                image.thumbnail((160, 110)); preview = ImageOps.exif_transpose(image).convert('RGBA')
                out = io.BytesIO(); preview.save(out, format='PNG')
                value['thumbnail'] = base64.b64encode(out.getvalue()).decode('ascii')
    return value


def run_reference_plan(payload, run):
    store = Store(payload['root']); jobs = store.jobs(payload['ids']); plan, cache = [], {}
    if len(jobs) != len(set(payload['ids'])): raise ValueError('任务已改变，请重新选择。')
    for job in jobs:
        run.check_canceled()
        if job['state'] not in {'draft', 'failed'}: continue
        files = []
        for source in reference_sources(job['payload']):
            if source['kind'] != 'local': continue
            key = os.path.normcase(source['value'])
            if key not in cache:
                if len(cache) >= 256: raise ValueError('单批本地图片超过256张，请分批提交。')
                cache[key] = image_record(source['value'], run, thumbnail=True)
            files.append(cache[key])
        if files: plan.append({'id': job['id'], 'task_id': job['payload']['task_id'], 'digest': fingerprint(job['payload']), 'files': files})
    run.check_canceled()
    return {'upload_plan': plan, 'next': payload['next'], 'ids': list(payload['ids'])}


def _metadata(api, identifier):
    try:
        return api.files().get(fileId=identifier, supportsAllDrives=True,
            fields='id,name,mimeType,parents,size,md5Checksum,trashed,capabilities(canAddChildren)').execute(num_retries=0)
    except Exception as error:
        if getattr(getattr(error, 'resp', None), 'status', None) == 404: return None
        raise


def upload_image(store, api, authorization, parent, approved, run):
    """Use a persistent pre-generated ID so an uncertain create cannot duplicate."""
    account = authorization['account_id']; sha = approved['sha256']
    identifier = store.upload_id(account, parent, sha)
    if not identifier:
        ids = api.files().generateIds(count=1, space='drive', type='files').execute(num_retries=0).get('ids', [])
        if len(ids) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]+', ids[0]): raise ValueError('Drive未返回有效上传文件ID。')
        identifier = store.upload_id(account, parent, sha, ids[0])
    def valid(metadata):
        return (metadata and metadata.get('id') == identifier and not metadata.get('trashed')
            and parent in metadata.get('parents', []) and str(metadata.get('size')) == str(approved['size'])
            and metadata.get('md5Checksum') == approved['md5'])
    # Recheck the exact bytes even when a prior upload can be reused.
    temporary = store.root.path/('reference-' + uuid4().hex + Path(approved['path']).suffix.lower())
    try:
        current = image_record(approved['path'], run, copy_to=temporary)
        if (current['sha256'], current['size']) != (sha, approved['size']):
            raise ValueError('图片在确认后改变，未上传新内容；请重新提交并确认。')
        remote = _metadata(api, identifier)
        if remote is not None:
            if not valid(remote): raise ValueError('已记录的云端素材已变更或移走，未覆盖；请核对素材目录或选择新目录。')
        else:
            from googleapiclient.http import MediaFileUpload
            import mimetypes
            media = MediaFileUpload(str(temporary), mimetype=mimetypes.guess_type(approved['name'])[0] or 'application/octet-stream',
                chunksize=1024 * 1024, resumable=True)
            try:
                request = api.files().create(body={'id': identifier, 'name': approved['name'], 'parents': [parent]},
                    media_body=media, supportsAllDrives=True, fields='id')
                deadline = time.monotonic() + 600
                done = None
                while done is None:
                    run.check_canceled()
                    if time.monotonic() > deadline: raise TimeoutError('图片上传超过10分钟，请稍后重试；原任务未提交。')
                    progress, done = request.next_chunk(num_retries=0)
                    if progress: run.report('上传Agent素材：' + approved['name'], int(progress.progress() * 100))
            finally:
                media.stream().close()
            if not valid(_metadata(api, identifier)): raise ValueError('上传结果尚未校验成功，任务未提交；重试将核对同一文件ID。')
        return 'https://drive.google.com/file/d/' + identifier + '/view'
    finally:
        if temporary.exists(): data_file(temporary).remove()


def prepare_job_references(job, approval, store, run, *, api=None):
    sources = reference_sources(job['payload'])
    if not any(item['kind'] == 'local' for item in sources): return job
    if not approval or approval.get('confirmed') is not True: raise ValueError('本地图片尚未得到本次上传确认，未上传或提交。')
    reviewed = next((item for item in approval['plan'] if item['id'] == job['id']), None)
    if not reviewed or reviewed['digest'] != fingerprint(job['payload']): raise ValueError('任务在上传确认后改变，请重新确认。')
    authorization = approval['authorization']; parent = folder_id(approval['folder'])
    if api is None: api = load_google_service(authorization)
    folder = _metadata(api, parent)
    if not folder or folder.get('mimeType') != 'application/vnd.google-apps.folder' or folder.get('trashed') or folder.get('capabilities', {}).get('canAddChildren') is False:
        raise ValueError('素材上传目录不存在或当前账号不能写入；未提交任务。')
    approved_files = {os.path.normcase(item['path']): item for item in reviewed['files']}
    links = []
    for source in sources:
        run.check_canceled()
        if source['kind'] == 'link': links.append(source['value']); continue
        item = approved_files.get(os.path.normcase(source['value']))
        if item is None: raise ValueError('本地素材不在本次确认清单中，未上传。')
        links.append(upload_image(store, api, authorization, parent, item, run))
    run.check_canceled()
    store.replace_reference(job['id'], reviewed['digest'], '\n'.join(links))
    return store.jobs([job['id']])[0]
