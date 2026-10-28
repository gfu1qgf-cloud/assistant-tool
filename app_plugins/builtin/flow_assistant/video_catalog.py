"""Read-only video inventories and explicit, download-only user corrections."""
import copy
import re
import time
import uuid

from .setup_support import compatible_extension, project_identity, project_url

STATES = {'download_failed', 'download_parked', 'uncertain', 'generating', 'failed'}


def binding_reason(service, row):
    if not re.fullmatch('[a-f0-9]{32}', row['attempt'] or ''):
        return '本条尚无有效的生成批次；视频列表可查看，暂不能绑定下载。'
    if row['id'] in service.archiving or row['status'] == 'archiving':
        return '此任务正在保存文件；列表可查看，保存结束后刷新再绑定。'
    if service._transfer_pending(row, 'download') or row['status'] == 'downloading':
        return '此任务仍有下载在执行；列表可查看，下载结束后刷新再绑定。'
    review_copy = (row['remote'].get('suspicious_delivery') or {}).get('state') == 'moved'
    if row['status'] not in STATES and not (row['status'] == 'needs_review' and review_copy):
        labels = {'queued':'等待执行', 'collected':'尚未提交', 'preparing':'准备参数', 'uploading':'上传图片',
            'submitting':'提交生成', 'completed':'已经保存成品', 'cancelled':'已取消', 'needs_review':'已保存待复核'}
        return '本条当前为“' + labels.get(row['status'], row['status']) + '”；视频列表可查看，当前不能替换下载绑定。'
    if any(p.get('job') == row['id'] and p.get('attempt') == row['attempt'] and not p.get('retired') and
            p.get('action') in {'prepare','upload_images','run_tasks'} for p in service.pending.values()):
        return '本条页面操作／提交尚未返回；列表可查看，结束后刷新再绑定。'
    return ''


def target(service, job, *, require_bindable=False):
    row = service.store.get(job)
    if not row:
        raise ValueError('原任务记录已不存在；请关闭此窗口，重新选择现有任务。')
    if require_bindable:
        reason = binding_reason(service, row)
        if reason: raise ValueError(reason)
    identity = project_identity(row['remote'].get('page') or service.preferences.get('profile_projects', {}).get(row['remote'].get('profile')))
    if not identity:
        raise ValueError('原任务缺少项目地址，请先在浏览器设置核对原项目。')
    project = 'https://' + identity[0] + identity[1]
    client = next((c for c in service.clients.values() if c.get('authorized') and c.get('id') == row['browser']), None)
    if not client:
        raise ValueError('原浏览器未连接；请打开原账号的 Flow 项目，连接后点击刷新。')
    if not compatible_extension(client.get('extension_version'), minimum_patch=53):
        raise ValueError('读取视频列表需要桥接扩展1.1.53；请在空闲时完成更新后点击刷新。')
    if project_identity(client.get('saved_project') or client.get('project')) != project_identity(project):
        raise ValueError('此浏览器保存的项目与原任务不同，请核对原项目后刷新。')
    if require_bindable and row['remote'].get('account') and row['remote']['account'] != client.get('account'):
        raise ValueError('视频列表可查看，但原账号尚未确认或已变化；确认原账号后刷新，再绑定下载。')
    return row, client, project


def emit_error(service, command, error):
    service.video_catalog_result.emit({'success': False, 'request_id': command.get('request_id'),
        'job': command.get('job'), 'error': str(error)[:600]})


def load(service, command):
    row, client, project = target(service, command['job'])
    # One read per window. A refresh invalidates the preceding inventory even
    # if its reply is still in transit. No task or browser execution slot held.
    request_id = str(command.get('request_id') or '')
    catalogs = getattr(service, 'video_catalogs', {})
    service.video_catalogs = {k: v for k, v in catalogs.items() if v['expires_at'] > time.time() and v['job'] != row['id']}
    pending_before = set(service.pending)
    if not service.request(client, 'list_project_videos', {'project': project}):
        raise ValueError('原浏览器正在关闭或连接未就绪，恢复连接后点击刷新。')
    pending = next(p for k, p in service.pending.items() if k not in pending_before)
    pending['catalog'] = {'job': row['id'], 'attempt': row['attempt'], 'project': project,
        'account': client.get('account'), 'request_id': request_id}
    service.video_catalog_latest = {**getattr(service, 'video_catalog_latest', {}), row['id']: request_id}


def response(service, client, request, message):
    context = request.get('catalog')
    if not context or getattr(service, 'video_catalog_latest', {}).get(context['job']) != context['request_id']:
        return
    if not message.get('success'):
        emit_error(service, context, message.get('error') or '视频列表读取失败，请刷新重试。')
        return
    try:
        row, current, project = target(service, context['job'])
        data = message.get('data') or {}
        if not isinstance(data, dict): raise ValueError('网页返回的视频列表无效，请刷新重试。')
        if (current is not client or
                project_identity(data.get('project')) != project_identity(project)):
            raise ValueError('任务、原账号或项目已变化，请刷新列表。')
        entries = []
        if not isinstance(data.get('videos'), list): raise ValueError('网页未返回有效的视频列表，请刷新重试。')
        seen = set()
        for item in (data.get('videos') or [])[:200]:
            if not isinstance(item, dict): continue
            media = item.get('media_id')
            if not isinstance(media, str) or not re.fullmatch('[A-Za-z0-9_-]{1,256}', media) or media in seen: continue
            seen.add(media)
            thumbnail = item.get('thumbnail') or ''
            if not isinstance(thumbnail, str) or len(thumbnail) > 14000 or not re.fullmatch(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+', thumbnail):
                thumbnail = ''
            entries.append({'key': uuid.uuid4().hex, 'media_id': media,
                'prompt': str(item.get('prompt') or '')[:1200], 'created': str(item.get('created') or '')[:100],
                'duration': str(item.get('duration') or '')[:30], 'thumbnail': thumbnail,
                'page': project.rstrip('/') + '/edit/' + media})
        token = uuid.uuid4().hex
        service.video_catalogs[token] = {**context, 'browser': client['id'], 'expires_at': time.time() + 600, 'entries': entries}
        reason = binding_reason(service, row)
        if row['attempt'] != context['attempt']: reason = '读取期间任务已重新调度；列表仍可查看，请刷新后再绑定。'
        elif row['remote'].get('account') and row['remote']['account'] != client.get('account'):
            reason = '原账号尚未确认或已变化；列表可查看，确认原账号后刷新，再绑定下载。'
        service.video_catalog_result.emit({'success': True, 'request_id': context['request_id'], 'job': row['id'],
            'catalog': token, 'videos': entries, 'can_bind': not reason, 'binding_reason': reason,
            'partial': bool(data.get('partial')), 'warning': str(data.get('warning') or '')[:500]})
    except (ValueError, TypeError) as error:
        emit_error(service, context, error)


def bind(service, command):
    if command.get('confirmed') is not True:
        raise ValueError('请选择视频并点击“使用所选视频恢复下载”。')
    listing = getattr(service, 'video_catalogs', {}).get(command.get('catalog'))
    row, client, project = target(service, command['job'], require_bindable=True)
    if (not listing or listing['expires_at'] <= time.time() or listing['job'] != row['id'] or
            listing['attempt'] != row['attempt'] or listing['browser'] != client['id'] or
            listing.get('account') != client.get('account') or project_identity(listing['project']) != project_identity(project)):
        raise ValueError('视频列表已过期或任务、账号已变化，请刷新后重新选择。')
    entry = next((v for v in listing['entries'] if v['key'] == command.get('key')), None)
    if not entry:
        raise ValueError('所选视频已不在本次列表，请刷新后重新选择。')
    # Keep original receipts intact. This record is a user override, never a
    # forged generation response and never permission for another paid submit.
    selection = {'schema': 1, 'confirmed': True, 'attempt': row['attempt'], 'project': project,
        'media_id': entry['media_id'], 'task_id': row['remote'].get('upstream_task') or 'manual_' + uuid.uuid4().hex,
        'submission_id': 'manual_' + uuid.uuid4().hex, 'confirmed_at': time.time(),
        'browser': client['id'], 'account': client.get('account') or row['remote'].get('account') or ''}
    remote = copy.deepcopy(row['remote'])
    if remote.get('suspicious_delivery'):
        remote['suspicious_delivery'] = {**remote['suspicious_delivery'], 'state':'superseded', 'superseded_at':time.time()}
    remote['manual_download_selection'] = selection
    remote['download_refresh'] = {'project': project, 'media_id': entry['media_id']}
    remote.pop('download_recovery_target', None)
    remote.pop('unverified_download', None)
    remote.pop('unverified_download_event', None)
    remote.update(cloud_finished=True, released_at=remote.get('released_at') or time.time())
    service.store.update(row['id'], 'download_failed', remote=remote, error='已按手动选择绑定视频，正在恢复下载。')
    service.retry_download(service.store.get(row['id']))
    service.video_catalogs.pop(command.get('catalog'), None)
    service.video_catalog_result.emit({'success': True, 'bound': True, 'request_id': command.get('request_id'),
        'job': row['id'], 'media_id': entry['media_id']})


def manual_binding(row):
    selected = row['remote'].get('manual_download_selection')
    if not isinstance(selected, dict): return None
    if (selected.get('schema') != 1 or selected.get('confirmed') is not True or
            selected.get('attempt') != row['attempt'] or selected.get('browser') != row['browser'] or
            not re.fullmatch('[A-Za-z0-9_-]{1,256}', str(selected.get('media_id') or '')) or
            not re.fullmatch('manual_[a-f0-9]{32}', str(selected.get('submission_id') or '')) or
            not selected.get('task_id') or not project_identity(selected.get('project')) or
            row['remote'].get('page') and project_identity(selected['project']) != project_identity(row['remote']['page']) or
            row['remote'].get('account') and selected.get('account') != row['remote']['account']):
        return None
    return {'schema': 2, 'source': 'manual_selection', 'taskId': selected['task_id'],
        'submissionId': selected['submission_id'], 'mediaIds': [selected['media_id']],
        'readyIds': [selected['media_id']], 'urls': {}}
