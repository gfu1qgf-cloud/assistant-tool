"""Image job snapshots and explicit binding of cloud jobs to a local task."""
import hashlib
import os
from pathlib import Path
import re

from app_plugins.api import load_google_service
from .tasks import snapshot, validate_payload, spreadsheet_id
from .preparation import prepare_payload
from .references import reference_sources
from .sheets import inspect
from .delivery import read_rows, _cell


def image_snapshot(target, reference, *, title=''):
    value = prepare_payload(snapshot(target, ''))
    value['reference'] = reference
    sources = reference_sources(value)
    if len(sources) != 1: raise ValueError('每张图片请单独建立任务。')
    source = sources[0]['value']
    if sources[0]['kind'] == 'link' and ('/folders/' in source or 'docs.google.com/document/' in source):
        raise ValueError('图片任务需要单张原图链接，不是素材文件夹或文档。')
    identity = hashlib.sha256((value['source_key'] + '\0image\0' + source).encode()).hexdigest()
    parent = value['task_id']
    value.update(task_kind='image', parent_task_id=parent, source_key=identity,
        task_id=parent + '-' + identity[:8], title=title or (Path(source).name if sources[0]['kind'] == 'local' else value['title']),
        spoken='', chinese='', language='', chinese_confirmed=True, subcategory='')
    validate_payload(value, allow_local_references=True)
    return value


def image_sources(target):
    value = prepare_payload(snapshot(target, ''))
    return [item['value'] for item in reference_sources(value)]


def run_image_bind_list(payload, run, *, service=None):
    service = service or load_google_service(payload['authorization'])
    identifier = spreadsheet_id(payload['url']); info = inspect(service, identifier, payload['sheet'], run)
    if info['kind'] != 'image': raise ValueError('目标分页不是图片转动画表头，未绑定。')
    rows = read_rows(service, identifier, payload['sheet'], info, run)
    choices = []
    for key, entries in rows.items():
        run.check_canceled()
        if len(entries) != 1: raise ValueError('图片表存在重复任务ID，请先核对，未猜测绑定。')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', key): raise ValueError('图片任务ID格式不安全，未绑定。')
        _, row = entries[0]; mapping = info['mapping']
        number = _cell(row, mapping, 'task_id')
        if not number: continue
        choices.append({'id': key, 'task_id': number, 'title': _cell(row, mapping, 'reference'),
            'reference': _cell(row, mapping, 'reference'), 'status': _cell(row, mapping, 'status'),
            'quality': _cell(row, mapping, 'quality'), 'user_requirements': _cell(row, mapping, 'user_requirements'),
            'subcategory': _cell(row, mapping, 'subcategory')})
    return {'image_bind_choices': choices, 'binding': {'destination': identifier + ':' + payload['sheet'],
        'target_dir': payload['target_dir'], 'parent_task_id': payload['parent_task_id']}}


def bind_image_jobs(store, choices, binding):
    """Local mapping only. Never adds, claims, regenerates or edits a cloud row."""
    directory = Path(binding['target_dir'])
    if not directory.is_absolute() or not directory.is_dir(): raise ValueError('请选择可用的原任务目录。')
    if len({item['id'] for item in choices}) != len(choices): raise ValueError('所选图片任务ID重复。')
    from .tasks import fingerprint
    from .store import now
    import json
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in choices:
            key = item['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', key): raise ValueError('任务ID无效。')
            old = db.execute('SELECT payload,destination FROM jobs WHERE id=?', (key,)).fetchone()
            if old:
                previous = json.loads(old[0])
                if os.path.normcase(os.path.abspath(previous['target_dir'])) != os.path.normcase(str(directory.absolute())) or old[1] != binding['destination']:
                    raise ValueError('该任务已绑定其他目录或表格，未覆盖；请先明确移除旧本地记录。')
                continue
            source = hashlib.sha256((str(directory.absolute()) + '\0image-cloud\0' + key).encode()).hexdigest()
            value = {**item, 'task_kind': 'image', 'target_dir': str(directory), 'parent_task_id': binding['parent_task_id'],
                'source_key': source, 'spoken': '', 'chinese': '', 'language': '', 'chinese_confirmed': True}
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)', (key, source, json.dumps(value, ensure_ascii=False),
                fingerprint(value), 'submitted', binding['destination'], '用户明确绑定的已有云端图片任务；未重新提交制作。', now()))
    return [item['id'] for item in choices]
