"""Plain task snapshots; no widgets, credentials or current-selection lookups."""
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


def normalize(value):
    return re.sub(r'[\s／/（）()·_\-]', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def spreadsheet_id(value):
    text = str(value or '').strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{20,160}', text): return text
    parts = urlsplit(text)
    match = re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]{20,160})(?:/.*)?', parts.path)
    if parts.scheme != 'https' or parts.netloc != 'docs.google.com' or not match:
        raise ValueError('请输入有效的 Google 表格链接或表格 ID。')
    return match.group(1)


def snapshot(target, language=''):
    task = target.get('task')
    if task is None: raise ValueError('任务已失效，请重新导入。')
    directory = str(target.get('target_dir') or '').strip()
    if not directory or not Path(directory).is_absolute(): raise ValueError('任务目录无效，请重新加载主界面任务。')
    identifier = str(getattr(task, 'task_id', '') or '').strip()
    if not identifier: raise ValueError('任务没有编号。')
    raw = getattr(task, 'raw', {})
    chinese_aliases = {normalize(item) for item in ('中文台词', '中文文案', '中文正文', '中文', '中文口播', '口播中文台词')}
    candidates = {str(value).strip() for key, value in raw.items()
                  if normalize(key) in chinese_aliases and str(value or '').strip()}
    if len(candidates) > 1: raise ValueError(f'任务 {identifier} 存在多列不同的中文台词，请先在任务表统一。')
    chinese = next(iter(candidates), '')
    chinese_source = '任务表中文列'
    if not chinese:
        chinese = str(getattr(task, '_full_task_name', '') or '').strip()
        chinese_source = '完整任务名称（请核对是否为中文台词）'
    spoken = str(getattr(task, 'task_audio_text', '') or '').strip()
    title = str(getattr(task, '_full_task_name', '') or getattr(task, 'task_name', '') or identifier).strip()
    # Do not publish a local path: it is retained only in the private local queue.
    source_key = os.path.normcase(os.path.abspath(directory)) + '\0' + identifier
    value = {
        'source_key': hashlib.sha256(source_key.encode()).hexdigest(), 'target_dir': directory,
        'task_id': identifier, 'title': title, 'task_type': str(getattr(task, 'task_type', '') or '口播视频'),
        'language': '' if language in ('unknown', 'und', None) else str(language).strip(),
        'spoken': spoken, 'chinese': chinese, 'chinese_source': chinese_source,
        'chinese_confirmed': bool(candidates),
        'requirements': '\n'.join(text for text in [str(getattr(task, 'task_requirements', '') or '').strip(),
             str(getattr(task, 'task_video_prompt', '') or '').strip()] if text),
        'reference': str(getattr(task, 'task_reference_link', '') or '').strip(),
        'notes': str(getattr(task, 'task_notes', '') or '').strip(),
        'agent': str(getattr(task, 'task_agent', '') or raw.get('执行Agent') or raw.get('执行AI') or '').strip(),
        'user_requirements': str(getattr(task, 'user_requirements', '') or raw.get('用户需求') or '').strip(),
    }
    return value


def validate_payload(value, *, allow_local_references=False):
    if value.get('task_kind') == 'image':
        if not str(value.get('task_id') or '').strip() or not str(value.get('reference') or '').strip():
            raise ValueError('图片任务缺少任务编号或原图素材。')
        from .references import reference_sources
        sources = reference_sources(value)
        if len(sources) != 1: raise ValueError('每条图片转动画任务必须且只能对应一张原图。')
        if sources[0]['kind'] == 'link' and ('/folders/' in sources[0]['value'] or 'docs.google.com/' in sources[0]['value']):
            raise ValueError('图片任务需要单张原图文件链接，不是文件夹或Google文档。')
        if sources[0]['kind'] == 'local' and not allow_local_references: raise ValueError('本地图片需先确认上传。')
        if any(len(str(value.get(key) or '')) > 45000 for key in ('requirements', 'reference', 'user_requirements')):
            raise ValueError('图片任务内容超过单元格安全长度。')
        return
    missing = [label for key, label in [('task_id', '任务编号'), ('spoken', '口播原文'),
                                      ('chinese', '中文台词'), ('language', '口播语言')]
               if not str(value.get(key) or '').strip()]
    if missing: raise ValueError('缺少：' + '、'.join(missing))
    if not value.get('chinese_confirmed'):
        raise ValueError('中文暂取自任务名称，请双击中文台词核对保存；也可确认来源后重新导入。')
    reference = str(value.get('reference') or '').strip()
    if reference and allow_local_references:
        from .references import reference_sources
        reference_sources(value)
        reference = ''
    if reference:
        for link in reference.splitlines():
            parts = urlsplit(link.strip())
            if parts.scheme != 'https' or parts.netloc not in {'drive.google.com', 'docs.google.com'}:
                raise ValueError('素材不是 Google Drive 链接，请修正；不会把本地路径上传到共享表。')
    for key in ('title', 'spoken', 'chinese', 'requirements', 'notes', 'reference', 'agent', 'user_requirements'):
        if len(str(value.get(key) or '')) > 45000: raise ValueError('任务内容过长，请先拆分，未截断台词。')


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
