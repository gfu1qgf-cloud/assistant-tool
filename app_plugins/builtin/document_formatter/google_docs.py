"""Official Docs formatting requests, UTF-16 API ranges, revision-safe restoration."""
import copy
import json
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from model.AppDataStorage import data_directory
from model.OfflineMode import require_online
from .rules import normalize_rules, paragraph_roles, MAX_PARAGRAPHS

PAGE_FIELDS = ['pageSize', 'marginTop', 'marginBottom', 'marginLeft', 'marginRight', 'flipPageOrientation', 'documentFormat']
PARA_FIELDS = ['indentStart', 'indentEnd', 'indentFirstLine', 'lineSpacing']


def document_target(value):
    raw = str(value or '').strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{10,200}', raw): return raw, ''
    parsed = urlsplit(raw)
    match = re.fullmatch(r'/document/d/([A-Za-z0-9_-]{10,200})(?:/[^/]*)?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname != 'docs.google.com' or parsed.username or parsed.password or parsed.port or not match:
        raise ValueError('请输入Google Docs文字文档链接；不接受Google表格或其他网站地址。')
    tab_id = parse_qs(parsed.query).get('tab', [''])[0]
    if tab_id and not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', tab_id): raise ValueError('文档分页标识无效。')
    return match.group(1), tab_id


def tabs(document):
    result = []
    def visit(items):
        for tab in items:
            properties = tab.get('tabProperties', {}); content = tab.get('documentTab')
            if content is not None:
                result.append({'id': properties['tabId'], 'title': properties.get('title', '文档分页'), 'content': content})
            visit(tab.get('childTabs', []))
    visit(document.get('tabs', []))
    if not result and document.get('body') is not None: result.append({'id': '', 'title': '正文', 'content': document})
    if not result: raise ValueError('Google文档没有可读取的正文分页。')
    return result


def body_paragraphs(content):
    result = []
    def visit(items, structured=False):
        for item in items:
            if 'paragraph' in item:
                paragraph = item['paragraph']; runs = paragraph.get('elements', [])
                result.append({'start': item.get('startIndex', 1), 'end': item.get('endIndex', 1),
                               'text': ''.join(run.get('textRun', {}).get('content', '') for run in runs),
                               'heading': paragraph.get('paragraphStyle', {}).get('namedStyleType', '').startswith(('TITLE', 'HEADING')),
                               'structured': structured or bool(paragraph.get('bullet')), 'raw': paragraph})
            for row in item.get('table', {}).get('tableRows', []):
                for cell in row.get('tableCells', []): visit(cell.get('content', []), True)
            visit(item.get('tableOfContents', {}).get('content', []), True)
    visit(content.get('body', {}).get('content', []))
    if len(result) > MAX_PARAGRAPHS: raise ValueError('文档段落过多，请拆分后处理。')
    for index, item in enumerate(result): item['id'] = f'p{index}'
    return result


def inspect_document(service, document_id, tab_id=''):
    require_online('Google Docs 读取')
    value = service.documents().get(documentId=document_id, includeTabsContent=True).execute(num_retries=0)
    if len(json.dumps(value).encode('utf-8')) > 24*1024*1024: raise ValueError('Google文档数据过大，请拆分后处理。')
    available = tabs(value)
    selected = next((tab for tab in available if tab['id'] == tab_id), None) if tab_id else available[0]
    if selected is None: raise ValueError('链接中的分页不存在，请重新选择文档分页。')
    if not value.get('revisionId'): raise ValueError('无法取得文档修订号；请确认当前账号具有编辑权限。未提交格式修改。')
    return {'kind': 'google', 'document_id': document_id, 'title': value.get('title', 'Google文档'),
            'tab_id': selected['id'], 'tab_title': selected['title'],
            'tabs': [{'id': tab['id'], 'title': tab['title']} for tab in available],
            'revision': value['revisionId'], 'document_style': copy.deepcopy(selected['content'].get('documentStyle', value.get('documentStyle', {}))),
            'sections': [copy.deepcopy(item) for item in selected['content'].get('body', {}).get('content', []) if 'sectionBreak' in item],
            'paragraphs': body_paragraphs(selected['content'])}


def pt(value): return {'magnitude': round(value * 72 / 2.54, 6), 'unit': 'PT'}


def requests_for(snapshot, rules, overrides=None):
    r = normalize_rules(rules); roles = paragraph_roles(snapshot['paragraphs'], r, overrides)
    tab = {'tabId': snapshot['tab_id']} if snapshot.get('tab_id') else {}
    page = {'pageSize': {'width': pt(r['page_width_cm']), 'height': pt(r['page_height_cm'])},
            'marginTop': pt(r['margin_top_cm']), 'marginBottom': pt(r['margin_bottom_cm']),
            'marginLeft': pt(r['margin_left_cm']), 'marginRight': pt(r['margin_right_cm']),
            'flipPageOrientation': False, 'documentFormat': {'documentMode': 'PAGES'}}
    updates = [{'updateDocumentStyle': {'documentStyle': page, 'fields': ','.join(PAGE_FIELDS), **tab}}]
    restore = [{'updateDocumentStyle': {'documentStyle': {field: snapshot['document_style'][field] for field in PAGE_FIELDS if field in snapshot['document_style']},
                                       'fields': ','.join(PAGE_FIELDS), **tab}}]
    formatted = 0
    for item in snapshot['paragraphs']:
        role = roles[item['id']]
        if role == 'keep': continue
        start, end = int(item['start']), int(item['end']) - 1
        if end <= start: continue
        target_range = {'startIndex': start, 'endIndex': end, **tab}
        fields = PARA_FIELDS + (['spaceAbove', 'spaceBelow'] if r['reset_spacing'] else [])
        paragraph = {'indentStart': pt(r['indent_left_cm']), 'indentEnd': pt(r['indent_right_cm']),
                     'indentFirstLine': pt(r['first_line_cm'] + r['indent_left_cm']), 'lineSpacing': r['line_spacing'] * 100}
        if r['reset_spacing']: paragraph.update(spaceAbove=pt(0), spaceBelow=pt(0))
        updates.append({'updateParagraphStyle': {'range': target_range, 'paragraphStyle': paragraph, 'fields': ','.join(fields)}})
        original = item['raw'].get('paragraphStyle', {})
        restore.append({'updateParagraphStyle': {'range': target_range, 'paragraphStyle': {field: original[field] for field in fields if field in original}, 'fields': ','.join(fields)}})
        style = {'fontSize': {'magnitude': r['title_pt'] if role == 'title' else r['body_pt'], 'unit': 'PT'}}
        text_fields = ['fontSize']
        if role == 'title': style['bold'] = True; text_fields.append('bold')
        updates.append({'updateTextStyle': {'range': target_range, 'textStyle': style, 'fields': ','.join(text_fields)}})
        for run in item['raw'].get('elements', []):
            run_start, run_end = max(start, run.get('startIndex', start)), min(end, run.get('endIndex', end))
            if run_end <= run_start: continue
            original_style = run.get('textRun', {}).get('textStyle', {})
            restore.append({'updateTextStyle': {'range': {'startIndex': run_start, 'endIndex': run_end, **tab},
                            'textStyle': {field: original_style[field] for field in text_fields if field in original_style}, 'fields': ','.join(text_fields)}})
        formatted += 1
    # Page-margin updates clear section overrides. Restore those after the page.
    for section in snapshot.get('sections', []):
        original = section['sectionBreak'].get('sectionStyle', {})
        fields = ['marginTop', 'marginBottom', 'marginLeft', 'marginRight']
        restore.append({'updateSectionStyle': {'range': {'startIndex': section.get('startIndex', 0),
                        'endIndex': section.get('endIndex', 1), **tab},
                        'sectionStyle': {field: original[field] for field in fields if field in original}, 'fields': ','.join(fields)}})
    if not formatted: raise ValueError('没有需要格式化的段落，未提交任何修改。')
    if len(restore) > 12000: raise ValueError('文档样式过于复杂，请拆分后处理。')
    return updates, restore, formatted


def apply_document(service, snapshot, rules, overrides, private_root, check=lambda: None):
    require_online('Google Docs 格式化'); check()
    current = inspect_document(service, snapshot['document_id'], snapshot['tab_id'])
    if current['revision'] != snapshot['revision']: raise ValueError('预览后Google文档已被修改，请重新读取。没有提交格式修改。')
    updates, restore, count = requests_for(snapshot, rules, overrides)
    root = data_directory(private_root); backup_name = f'google-history/{uuid4().hex}.json'
    record = {'version': 1, 'document_id': snapshot['document_id'], 'tab_id': snapshot['tab_id'],
              'title': snapshot['title'], 'before': snapshot, 'restore': restore, 'after_revision': None, 'status': 'prepared'}
    if len(json.dumps(record, ensure_ascii=False).encode('utf-8')) > 24*1024*1024: raise ValueError('恢复记录过大，请拆分文档。')
    backup = root.file(backup_name); backup.write_json(record)
    check()
    root.storage.check(root.path, write=True)
    try:
        result = service.documents().batchUpdate(documentId=snapshot['document_id'], body={
            'requests': updates, 'writeControl': {'requiredRevisionId': snapshot['revision']}}).execute(num_retries=0)
    except Exception as error:
        status = getattr(getattr(error, 'resp', None), 'status', None)
        if status in {400, 401, 403, 404, 429}:
            hints = {400: '请刷新文档；修订号或格式参数可能已失效。', 401: '请重新Google授权。',
                     403: '请确认Docs API已启用，且账号具有文档编辑权限。', 404: '请确认文档仍存在、链接和账号正确。',
                     429: '接口限流或配额不足，请稍后手动重试。'}
            raise RuntimeError(f'Google拒绝格式化请求（HTTP {status}），没有应用本次更新。{hints[status]}') from None
        raise RuntimeError('Google格式化请求结果未确认。请先查看在线文档，不要直接重试；恢复记录已保存，必要时可用Google版本历史恢复。') from None
    record.update(status='applied', after_revision=result.get('writeControl', {}).get('requiredRevisionId'))
    try:
        backup.write_json(record); root.file('google-last.json').write_json({'backup': backup_name})
    except OSError:
        raise RuntimeError('Google请求已完成，但保存恢复状态失败。请查看在线文档；不要重复执行。原恢复记录在加密目录中。') from None
    return {'kind': 'google', 'document_id': snapshot['document_id'], 'output': f'https://docs.google.com/document/d/{snapshot["document_id"]}/edit',
            'paths': [], 'formatted': count, 'backup': str(backup.path)}


def restore_document(service, private_root, check=lambda: None):
    require_online('Google Docs 恢复'); root = data_directory(private_root)
    latest = root.file('google-last.json')
    if not latest.exists(): raise ValueError('还没有可以恢复的Google格式化记录。')
    record = root.file(latest.read_json()['backup']).read_json()
    document_target(record.get('document_id'))
    if any(not isinstance(request, dict) or len(request) != 1 or next(iter(request)) not in
           {'updateDocumentStyle', 'updateParagraphStyle', 'updateTextStyle', 'updateSectionStyle'} for request in record.get('restore', [])):
        raise ValueError('恢复记录包含非格式化操作，已拒绝执行。')
    if record.get('status') != 'applied' or not record.get('after_revision'): raise ValueError('上次请求状态不明确，不能自动恢复；请使用Google文档版本历史。')
    current = inspect_document(service, record['document_id'], record['tab_id'])
    if current['revision'] != record['after_revision']: raise ValueError('格式化后文档又被编辑过，自动恢复已停止；请使用Google版本历史，避免覆盖后来修改。')
    check()
    root.storage.check(root.path, write=True)
    service.documents().batchUpdate(documentId=record['document_id'], body={'requests': record['restore'],
        'writeControl': {'requiredRevisionId': record['after_revision']}}).execute(num_retries=0)
    record['status'] = 'restored'; root.file(latest.read_json()['backup']).write_json(record)
    return {'kind': 'restore', 'paths': [], 'output': f'https://docs.google.com/document/d/{record["document_id"]}/edit'}
