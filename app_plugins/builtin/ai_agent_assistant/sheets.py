"""Header-based append, no existing-row rewrites and no blind write retries."""
import csv
import io
import json
import re

from model.GoogleAccounts import load_managed_service
from app_plugins.api import load_google_service
from .tasks import normalize, spreadsheet_id, validate_payload
from .store import Store
from .segmentation import split_spoken

FIELDS = [
    ('job_id', '作业ID', ('作业编号',)), ('task_id', '所属任务编号', ('任务编号',)),
    ('title', '任务名称', ('任务标题',)), ('task_type', '任务类型', ()), ('priority', '优先级', ()),
    ('status', '执行状态', ('任务状态',)), ('agent', '执行Agent', ('领取Agent', '执行AI', '执行人')),
    ('language', '口播语言', ('语言',)), ('spoken', '口播台词（原文）', ('口播原文', '口播台词', '原文台词')),
    ('chinese', '中文台词', ('中文对照', '中文文案')), ('requirements', '提示词／制作要求', ('制作要求', '提示词', '提示词/要求')),
    ('reference', '素材Drive链接', ('素材链接', '素材GoogleDrive链接')), ('aspect', '画幅', ()),
    ('duration', '目标时长（秒）', ('目标时长',)), ('resolution', '分辨率', ()),
    ('result', '成品Drive链接', ('成品链接',)), ('archive', '归档状态', ()),
    ('notes', '备注／错误', ('备注', '错误/备注', '处理备注/错误')), ('created', '创建时间', ()),
    ('claimed', '领取时间', ()), ('updated', '最后更新时间', ('更新时间',)),
    ('attempt', '执行批次ID', ()), ('lease', '领取令牌', ()), ('heartbeat', '心跳时间', ()),
    ('expires', '租约到期时间', ()), ('checksum', '成品校验信息', ()),
]
REQUIRED = {'job_id', 'task_id', 'title', 'spoken', 'chinese', 'language', 'status'}
# Only producer-owned fields are written. JSON null skips other cells in the
# values API, preserving template defaults/formulas and customer-added columns.
SUBMISSION_FIELDS = {'job_id', 'task_id', 'title', 'task_type', 'priority', 'status', 'language',
                     'spoken', 'chinese', 'requirements', 'reference', 'archive', 'notes', 'created', 'updated'}


def column(index):
    result = ''
    while index:
        index, digit = divmod(index - 1, 26); result = chr(65 + digit) + result
    return result


def a1(sheet, cells): return "'" + sheet.replace("'", "''") + "'!" + cells


def headers(rows):
    candidates = []
    for number, row in enumerate(rows, 1):
        mapping, duplicates = {}, []
        for key, title, aliases in FIELDS:
            hits = [index for index, value in enumerate(row) if normalize(value) in {normalize(item) for item in (title, *aliases)}]
            if len(hits) > 1: duplicates.append(title)
            if hits: mapping[key] = hits[0]
        if REQUIRED.issubset(mapping):
            if duplicates: raise ValueError('表头存在重复列名：' + '、'.join(duplicates))
            candidates.append((number, mapping, len(row)))
    if len(candidates) != 1:
        raise ValueError('未找到唯一的完整表头。需要：作业ID、所属任务编号、任务名称、口播语言、口播台词（原文）、中文台词、执行状态。未写入。')
    return candidates[0]


def row_values(job, mapping, count, run=None):
    payload = job['payload']
    validate_payload(payload)
    values = {**payload, 'job_id': job['id'], 'status': '待领取', 'priority': '普通',
              'archive': '未下载', 'created': job['created'], 'updated': job['created']}
    segmented = split_spoken(payload['spoken'], run)
    values['spoken'] = segmented['text']
    note = f'外文分为{segmented["count"]}段，每段约10秒；按语速与标点估算，不是精密音频对齐。段号和秒数标签仅用于分组，不朗读。'
    values['notes'] = '\n'.join(item for item in (payload.get('notes', ''), note, segmented['warnings']) if item)
    row = [None] * count
    for key, index in mapping.items():
        if key in SUBMISSION_FIELDS: row[index] = str(values.get(key) or '')
    return row


def clipboard_tsv(jobs, run=None):
    mapping = {key: index for index, (key, _title, _aliases) in enumerate(FIELDS)}
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
    writer.writerow([title for _key, title, _aliases in FIELDS])
    for job in jobs:
        if run is not None: run.check_canceled()
        row = row_values(job, mapping, len(FIELDS), run)
        # Browser paste parses leading '=' as a formula even though API RAW does not.
        writer.writerow(["'" + value if value and value.startswith(('=', '+', '-', '@')) else value for value in row])
    return stream.getvalue()


def _execute(request):
    try:
        return request.execute(num_retries=0)
    except Exception as error:
        # An API disabled on the OAuth client's Cloud project is not a Sheet
        # sharing failure. Keep diagnostics short and never retry a write here.
        try:
            data = json.loads(error.content)
            disabled = next((item for item in data.get('error', {}).get('details', [])
                             if item.get('reason') == 'SERVICE_DISABLED'
                             and item.get('metadata', {}).get('service') == 'sheets.googleapis.com'), None)
        except (AttributeError, TypeError, ValueError): disabled = None
        if disabled is not None:
            consumer = disabled.get('metadata', {}).get('consumer', '')
            match = re.fullmatch(r'projects/(\d+)', consumer)
            project = '（项目 ' + match.group(1) + '）' if match else ''
            raise ValueError('这套 OAuth 凭据所属项目尚未启用 Google Sheets API' + project + '。'
                             '请在 Google Cloud 控制台启用，等待几分钟后再试；这不是表格共享权限错误，无需先删除凭据或重新登录。') from None
        raise


def inspect(service, identifier, name, run):
    metadata = _execute(service.spreadsheets().get(spreadsheetId=identifier,
        fields='spreadsheetUrl,sheets(properties(sheetId,title,gridProperties))'))
    matches = [sheet['properties'] for sheet in metadata.get('sheets', []) if sheet['properties']['title'] == name]
    if len(matches) != 1: raise ValueError('找不到指定分页“' + name + '”；未改为其他分页。')
    props = matches[0]
    width = min(52, props.get('gridProperties', {}).get('columnCount', 26))
    height = props.get('gridProperties', {}).get('rowCount', 1000)
    run.check_canceled()
    values = _execute(service.spreadsheets().values().get(spreadsheetId=identifier,
        range=a1(name, f'A1:{column(width)}{min(height, 10)}'), valueRenderOption='FORMULA')).get('values', [])
    header, mapping, count = headers(values)
    return {'url': metadata.get('spreadsheetUrl', ''), 'id': props['sheetId'], 'height': height,
            'header': header, 'mapping': mapping, 'count': count}


def remote_ids(service, identifier, name, info, run):
    result = set()
    col = column(info['mapping']['job_id'] + 1)
    height = info['height']
    if height > 100000: raise ValueError('分页超过10万行，请先归档旧任务或使用新的口播分页。未冒险跳过重复检查。')
    for start in range(info['header'] + 1, height + 1, 5000):
        run.check_canceled()
        values = _execute(service.spreadsheets().values().get(spreadsheetId=identifier,
            range=a1(name, f'{col}{start}:{col}{min(start + 4999, height)}'),
            valueRenderOption='UNFORMATTED_VALUE')).get('values', [])
        for row in values:
            if row and str(row[0]).strip():
                value = str(row[0]).strip()
                if value in result: raise ValueError('云端存在重复作业ID，请先核对；本次未新增。')
                result.add(value)
    return result


def validate_options(service, identifier, name, info, run):
    """Respect the live dropdown vocabulary instead of writing invalid statuses."""
    if info['header'] >= info['height']: return
    run.check_canceled()
    result = _execute(service.spreadsheets().get(spreadsheetId=identifier,
        ranges=[a1(name, f'A{info["header"] + 1}:{column(info["count"])}{info["header"] + 1}')],
        includeGridData=True, fields='sheets(data(startColumn,rowData(values(dataValidation))))'))
    defaults = {'status': '待领取', 'priority': '普通', 'archive': '未下载'}
    for sheet in result.get('sheets', []):
        for data in sheet.get('data', []):
            for row in data.get('rowData', []):
                for key, value in defaults.items():
                    index = info['mapping'].get(key, -1) - data.get('startColumn', 0)
                    cells = row.get('values', [])
                    if index < 0 or index >= len(cells): continue
                    rule = cells[index].get('dataValidation', {}).get('condition', {})
                    if rule.get('type') == 'ONE_OF_LIST':
                        options = [item.get('userEnteredValue', '') for item in rule.get('values', [])]
                        if value not in options: raise ValueError(f'云端下拉选项不含“{value}”，请调整对应状态列；未写入。')
                    elif rule.get('type') == 'ONE_OF_RANGE':
                        raise ValueError('状态列使用了范围型下拉验证；请先改为包含程序状态的直接选项列表，未写入。')


def run_export(payload, run, *, service=None, upload_service=None):
    if payload.get('operation') not in {'submit', 'check'}: raise ValueError('未知的口播表格操作。')
    identifier = spreadsheet_id(payload['url'])
    name = str(payload.get('sheet') or '').strip()
    if not name: raise ValueError('请填写口播分页名称。')
    store = Store(payload['root'])
    jobs = store.jobs(payload['ids'])
    if len(jobs) != len(set(payload['ids'])): raise ValueError('部分任务已不存在，请重新选择。')
    if not jobs: raise ValueError('请选择要提交或核对的任务。')
    destination = identifier + ':' + name
    if service is None:
        if payload.get('google_authorization'):
            service = load_google_service(payload['google_authorization'])
        else:  # Previously queued requests retain their submission-time account check.
            service = load_managed_service('sheets', profile='ai_agent_assistant', expected_account_id=payload.get('account_id'))
    if service is None:
        raise ValueError('请在“程序设置 → Google账号”登录并分配 Google 表格账号。浏览器登录不等于程序API授权；本地待提交任务已保留，也可先复制表格。')
    run.report('检查口播分页与列名', 10)
    info = inspect(service, identifier, name, run)
    found = remote_ids(service, identifier, name, info, run)
    confirmed, remaining, blocked = [], [], []
    for job in jobs:
        if job['destination'] and job['destination'] != destination:
            blocked.append(job['payload']['task_id']); continue
        if job['id'] in found:
            confirmed.append(job['id'])
        elif job['state'] in {'sending', 'uncertain', 'submitted'}:
            blocked.append(job['payload']['task_id'])
        else: remaining.append(job)
    if confirmed: store.mark(confirmed, 'submitted', destination, '已在云端找到此作业ID，未重复写入')
    if payload.get('operation') == 'check':
        return {'confirmed': len(confirmed), 'added': 0, 'blocked': blocked,
                'message': f'已核对 {len(confirmed)} 条。未找到的历史提交保持待核对，不自动重发。'}
    if remaining: validate_options(service, identifier, name, info, run)
    valid, invalid = [], []
    for job in remaining:
        try:
            validate_payload(job['payload'], allow_local_references=True)
            if job['payload'].get('reference') and 'reference' not in info['mapping']:
                raise ValueError('表格缺少“素材Drive链接”列，未上传或提交有素材的任务。')
            from .references import prepare_job_references
            job = prepare_job_references(job, payload.get('reference_approval'), store, run, api=upload_service)
            validate_payload(job['payload'])
        except Exception as error:
            run.check_canceled()
            invalid.append(job['payload']['task_id'])
            store.fail_draft(job, str(error))
        else: valid.append(job)
    remaining = valid
    # A fresh complete payload is recorded before the only write attempt.
    if remaining:
        prepared, rows = [], []
        for job in remaining:
            run.check_canceled()
            try: row = row_values(job, info['mapping'], info['count'], run)
            except ValueError as error:
                invalid.append(job['payload']['task_id'])
                store.fail_draft(job, str(error))
            else: prepared.append(job); rows.append(row)
        remaining = prepared
    if remaining:
        if sum(len(str(row)) for row in rows) > 1500000: raise ValueError('本批文本超过安全提交大小，请分批选择。')
        run.report('正在写入口播任务；不覆盖已有行', 50)
        request = service.spreadsheets().values().append(spreadsheetId=identifier,
                range=a1(name, f'A{info["header"]}:{column(info["count"])}{info["height"]}'),
                # Fill preformatted blank rows instead of inserting new rows
                # inheriting header style and pushing dropdown templates down.
                valueInputOption='RAW', insertDataOption='OVERWRITE', body={'values': rows})
        run.check_canceled()
        store.begin(remaining, destination)
        ids = [job['id'] for job in remaining]
        try:
            response = _execute(request)
            if response.get('updates', {}).get('updatedRows') != len(rows):
                raise RuntimeError('写入条数未得到完整确认')
        except BaseException:
            store.mark(ids, 'uncertain', destination, '写入结果未确认。请点“核对提交结果”，不会自动重复新增。')
            raise RuntimeError('提交结果尚未确认，任务已保留。请核对提交结果；不自动重发，不影响其他任务。') from None
        store.mark(ids, 'submitted', destination, 'Google已确认新增')
    run.report('口播任务提交完成', 100)
    return {'added': len(remaining), 'confirmed': len(confirmed), 'blocked': blocked, 'invalid': invalid,
            'message': f'新增 {len(remaining)} 条，已存在 {len(confirmed)} 条，待核对 {len(blocked)} 条，资料不全跳过 {len(invalid)} 条。'}


def run_copy(payload, run):
    jobs = Store(payload['root']).jobs(payload['ids'])
    if len(jobs) != len(set(payload['ids'])) or not jobs: raise ValueError('部分任务已不存在，请重新选择。')
    run.report('按约10秒分段并准备复制表格', 0)
    text = clipboard_tsv(jobs, run)
    run.check_canceled()
    return {'clipboard': text, 'message': f'已复制 {len(jobs)} 条任务，外文已按约10秒分段。手动粘贴不会更改本地提交状态。'}
