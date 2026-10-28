"""Move completed, task-directory-bound videos with a visible 【存疑】 prefix.

This does not assert generated-media ownership. Persist the verified copy
before deleting its download original so restart can finish the move safely.
"""
import copy
import hashlib
import json
from pathlib import Path
import time
import uuid

from model.AppDataStorage import data_file
from .archive import archive_download, bound_download, digest


def receipt(row):
    value = row['remote'].get('unverified_download') or row['remote'].get('download')
    return value if isinstance(value, dict) and value.get('state') == 'complete' else None


def receipt_key(data):
    return hashlib.sha256(json.dumps({k: data.get(k) for k in
        ('filename', 'downloadId', 'mediaId', 'submissionId', 'taskId', 'download_path')},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_row(row, data):
    return {**row, 'remote': {**row['remote'], 'download': data}}


def output_path(row):
    payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
    remote = row.get('remote') if isinstance(row.get('remote'), dict) else {}
    saved = remote.get('suspicious_delivery') or {}
    path = saved.get('output')
    if saved.get('state') in {'copied', 'moved'} and isinstance(path, str):
        candidate = Path(path)
        if candidate.name.startswith('【存疑】') and payload.get('directory') and candidate.parent.resolve() == Path(payload['directory']).resolve():
            return path
    return payload.get('output')


def copy_video(row, downloads_root):
    from model.node_workflow.render import probe, resolve_tools
    from .policy import media_review
    data = receipt(row)
    candidate = source_row(row, data)
    source = bound_download(candidate, downloads_root)
    with data_file(source).open('rb') as stream:
        header = stream.read(32)
    if len(header) < 16 or header[4:8] != b'ftyp':
        raise ValueError('下载内容不是有效MP4视频，未移动网页或错误文件。')
    metadata = []
    def verify(path):
        info = probe(path, resolve_tools()[1]); metadata.append(info); return info
    destination = row['remote']['suspicious_delivery']['output']
    output = archive_download(candidate, downloads_root, verify=verify, destination=destination)
    return {'output': output, 'sha256': digest(output), 'media': metadata[0],
        'review': ['缺少生成回执，视频已加【存疑】标记'] + media_review(metadata[0], row['payload'])}


def remove_original(row, downloads_root):
    saved = row['remote']['suspicious_delivery']
    destination = Path(saved['output'])
    if output_path(row) != str(destination) or digest(destination) != saved['sha256']:
        raise ValueError('任务目录中的【存疑】副本已变化，保留原下载。')
    source = bound_download(source_row(row, saved['source']), downloads_root)
    if source.exists():
        if source == destination.resolve() or digest(source) != saved['sha256']:
            raise ValueError('原下载内容已变化，保留原下载。')
        data_file(source).remove()
    return {}


def submit(service, row, phase, work):
    identity = row['remote']['suspicious_delivery']['id']
    service.archiving.add(row['id'])
    def completed(future):
        try: result = future.result()
        except Exception as error: result = {'error': str(error)}
        service.archive_result.emit({'id': row['id'], 'attempt': row['attempt'],
            'suspicious_phase': phase, 'delivery_id': identity, **result})
    try: future = service.pool.submit(work, copy.deepcopy(row), service.downloads_root)
    except Exception as error:
        service.archive_result.emit({'id': row['id'], 'attempt': row['attempt'],
            'suspicious_phase': phase, 'delivery_id': identity, 'error': str(error)})
    else: future.add_done_callback(completed)


def preserve(service, row, *, retry=False):
    data = receipt(row)
    if not data or row['status'] in {'completed', 'needs_review', 'cancelled'}: return False
    if data.get('download_path') != service.download_name(row): return False
    try: bound_download(source_row(row, data), service.downloads_root)
    except (OSError, ValueError, KeyError, TypeError): return False
    if row['id'] in service.archiving: return True
    previous = row['remote'].get('suspicious_delivery') or {}
    key = receipt_key(data)
    if previous.get('key') == key and previous.get('state') == 'failed' and not retry: return True
    target = Path(row['payload']['output'])
    target = target.with_name('【存疑】' + target.name)
    if previous.get('key') == key and previous.get('output'):
        target = Path(previous['output'])
    elif target.exists():
        target = target.with_name(target.stem + '_' + str(data.get('downloadId') or key[:8]) + target.suffix)
    if target.parent.resolve() != Path(row['payload']['directory']).resolve(): return False
    saved = {'id': uuid.uuid4().hex, 'key': key, 'state': 'copying', 'output': str(target), 'source': copy.deepcopy(data)}
    service.store.update(row['id'], 'download_parked', remote={**row['remote'], 'suspicious_delivery': saved,
        'download_skipped': True, 'released_at': row['remote'].get('released_at') or time.time()},
        error='正在将已下载视频移动到任务目录，文件名前加【存疑】；不占生成名额。')
    for request_id, request in list(service.pending.items()):
        if request['job'] == row['id'] and request['attempt'] == row['attempt'] and request['action'] == 'retry_download':
            service.pending.pop(request_id, None)
    submit(service, service.store.get(row['id']), 'copy', copy_video)
    return True


def handle_result(service, result):
    phase = result.get('suspicious_phase')
    if not phase: return False
    row = service.store.get(result['id'])
    if not row or row['attempt'] != result['attempt']:
        service.archiving.discard(result['id']); return True
    saved = row['remote'].get('suspicious_delivery') or {}
    if saved.get('id') != result.get('delivery_id'): return True
    error = result.get('error')
    if phase == 'copy' and not error:
        saved = {**saved, **{k: result[k] for k in ('output', 'sha256', 'media', 'review')}, 'state': 'copied'}
        service.store.update(row['id'], 'needs_review', remote={**row['remote'], 'suspicious_delivery': saved,
            'media': result['media'], 'review': result['review']}, error='视频已保存到：' + saved['output'] + '；正在移走下载目录原件。')
        submit(service, service.store.get(row['id']), 'move', remove_original)
    else:
        service.archiving.discard(row['id'])
        saved = {**saved, 'state': ('failed' if phase == 'copy' else 'copied') if error else 'moved'}
        detail = ('存疑视频保存失败：' + str(error) + '；原下载：' + str(saved['source'].get('filename', '')) if phase == 'copy' else
            '已保存到：' + saved['output'] + ('；原下载清理未完成：' + str(error) if error else '；文件已加【存疑】，请核对内容。'))
        service.store.update(row['id'], 'download_parked' if phase == 'copy' else 'needs_review',
            remote={**row['remote'], 'suspicious_delivery': saved}, error=detail)
        if not error and phase == 'move': service.finished_job.emit(saved['output'])
    if not service.closing: service.publish()
    return True


def recover(service):
    """One local pass on startup, including already parked legacy downloads."""
    for row in service.store.rows():
        saved = row['remote'].get('suspicious_delivery') or {}
        if row['status'] == 'cancelled' or row['id'] in service.archiving: continue
        if saved.get('state') == 'copied': submit(service, row, 'move', remove_original)
        elif not saved or saved.get('state') in {'copying', 'failed'}:
            if row['remote'].get('unverified_download') or saved: preserve(service, row, retry=True)
