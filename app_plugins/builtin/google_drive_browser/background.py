"""Only ordinary snapshots enter the host queue; no widget-owned threads."""
import copy
import io
from pathlib import Path

from model.AppDataStorage import data_file
from model.DrivePreviewCache import DrivePreviewCache
from model.GoogleDriveBrowser import (authorize, load_snapshot, save_snapshot, service, snapshot_key)
from model.MediaStorage import require_media_output


def image_preview(path):
    from PIL import Image, ImageOps
    with data_file(path).open('rb') as source, Image.open(source) as original:
        if original.width * original.height > 64_000_000: raise ValueError('图片超过6400万像素，未自动解码。')
        original.thumbnail((2400, 2400))
        image = ImageOps.exif_transpose(original).convert('RGB')
        stream = io.BytesIO(); image.save(stream, format='JPEG', quality=92); image.close()
    return stream.getvalue()


def prepared_preview(item, path, cache, run):
    run.check_canceled()
    with cache.pin(path):
        value = {'kind': 'preview', 'item': item, 'local': str(path), 'image': None}
        if item['mimeType'].startswith('image/'): value['image'] = image_preview(path)
        else:
            duration = float(item.get('videoMediaMetadata', {}).get('durationMillis') or 0) / 1000
            if duration <= 0:
                import json, subprocess
                from app_paths import APP_ROOT
                result = subprocess.run([str(APP_ROOT / 'runtime/ffmpeg/ffprobe.exe'), '-v', 'error',
                    '-show_entries', 'format=duration', '-of', 'json', str(path)], capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try: duration = float(json.loads(result.stdout)['format']['duration'])
                except (KeyError, ValueError): raise ValueError('无法读取视频时长。') from None
            value['duration'] = duration
        return value


def run_drive_job(payload, run):
    operation = payload['operation']
    if operation == 'authorize': return authorize(run)
    cache = DrivePreviewCache(payload.get('cache_root'), **payload.get('cache_limits', {}))
    if operation == 'clear_cache':
        for kind in cache.limits: cache.prune(kind, clear=True)
        return {'kind': 'clear_cache', 'usage': cache.usage()}
    if operation == 'save_media':
        destination, source = Path(payload['destination']), Path(payload['source'])
        require_media_output(destination)
        if destination.exists(): raise ValueError('目标文件已经存在，请换一个名称；不会覆盖已有文件。')
        with cache.pin(source): data_file(destination).copy_from(source)
        return {'kind': 'save_media', 'paths': [str(destination)]}
    if operation == 'preview' and payload.get('account'):
        path = cache.path('media', payload['account'], payload['item'])
        if cache.existing(path): return prepared_preview(payload['item'], path, cache, run)
    with service(run) as drive:
        if operation == 'browse':
            value = drive.browse(payload['item'], page=payload.get('page', ''), search=payload.get('search', ''),
                                 order=payload.get('order', 'name_natural'), media=payload.get('media', 'all'))
            if value['kind'] == 'file':
                value = {'kind': 'list', 'folder': value['item'], 'items': [value['item']], 'next': '', 'single_file': True}
            if value['kind'] == 'list':
                key = payload['key']
                if payload.get('page'):
                    previous = load_snapshot(key)
                    if previous:
                        known = {item['id'] for item in previous['items']}
                        value['items'] = previous['items'] + [item for item in value['items'] if item['id'] not in known]
                value.update(account=drive.account, key=key)
                # Preserve thumbnails for unchanged items when refreshing metadata.
                previous = load_snapshot(key)
                prior = {item['id']: item for item in previous.get('items', [])} if previous and previous.get('account') == drive.account else {}
                for item in value['items']:
                    old = prior.get(item['id'], {})
                    if (item.get('version'), item.get('modifiedTime')) == (old.get('version'), old.get('modifiedTime')) and old.get('_thumbnail'):
                        item['_thumbnail'] = old['_thumbnail']
                save_snapshot(key, value)
            return value
        if operation == 'thumbnails':
            snapshot = load_snapshot(payload['key'])
            if not snapshot: raise ValueError('目录缓存已不存在，请刷新目录。')
            if snapshot.get('account') != drive.account: raise ValueError('目录属于另一个Google登录，请刷新目录后再缓存缩略图。')
            selected = set(payload['ids']); updates, errors = {}, {}
            for index, item in enumerate([value for value in snapshot['items'] if value['id'] in selected]):
                run.check_canceled()
                run.report(f'准备完整缩略图 · {index+1}/{len(selected)}', int(index*100/max(1,len(selected))))
                try: updates[item['id']] = drive.thumbnail(item, cache)
                except (ValueError, OSError, TimeoutError) as error: errors[item['id']] = str(error)
            run.check_canceled()
            for item in snapshot['items']:
                if item['id'] in updates: item['_thumbnail'] = updates[item['id']]; item.pop('_thumbnail_error', None)
                elif item['id'] in errors: item['_thumbnail_error'] = errors[item['id']]
            save_snapshot(payload['key'], snapshot)
            return {'kind': 'thumbnails', 'key': payload['key'], 'updates': updates, 'errors': errors}
        if operation == 'preview':
            item, path = drive.media(payload['item'], cache)
            return prepared_preview(item, path, cache, run)
    raise ValueError('未知网盘操作。')
