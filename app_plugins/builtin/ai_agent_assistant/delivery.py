"""One bounded receipt pass: exact job identity, durable retries, isolated failures."""
from collections import defaultdict
import hashlib
from pathlib import Path
import time
from uuid import uuid4

from app_plugins.api import load_google_service
from .delivery_drive import result_links, collect_files, output_directory, download_file, cached_file, error_message
from .sheets import inspect, a1, column, _execute
from .store import Store
from .tasks import spreadsheet_id
from .notice_data import directory_key
from .sheet_profiles import task_kind, completed as status_completed, archive_value, ARCHIVED,needs_user_segments
from .receipt_plan import delivery_plan, check_parts, completed_files, handoff_files, HANDOFF_POLICY
from .review import cache_review

DONE = {'已完成', '完成', '制作完成', '生成完成', 'completed', 'complete', 'done'}
DISCOVERY_SCOPE = 'recursive-v2-supplemental'
RECEIPT_PROGRESS_PREFIX = 'Agent收取进度：'


class LeasedRun:
    def __init__(self, run, store, token):
        self.run, self.store, self.token, self.renewed = run, store, token, time.monotonic()
        self.progress_count=''
    def check_canceled(self):
        self.run.check_canceled()
        if time.monotonic() - self.renewed > 20:
            self.store.renew_delivery_run(self.token); self.renewed = time.monotonic()
    def report(self, message, percent=None):
        self.check_canceled()
        if self.progress_count and not message.startswith(RECEIPT_PROGRESS_PREFIX):
            message=self.progress_count+' · '+message
        self.run.report(message,percent)


def _cell(row, mapping, key):
    index = mapping.get(key)
    return str(row[index] or '').strip() if index is not None and index < len(row) else ''


def read_rows(api, identifier, name, info, run):
    if info['height'] > 100000: raise ValueError('口播分页超过10万行，请先归档旧记录。')
    rows = {}
    for first in range(info['header'] + 1, info['height'] + 1, 1000):
        run.check_canceled()
        values = _execute(api.spreadsheets().values().get(spreadsheetId=identifier,
            range=a1(name, f'A{first}:{column(info["count"])}{min(first + 999, info["height"])}'),
            valueRenderOption='FORMULA')).get('values', [])
        for offset, row in enumerate(values):
            key = _cell(row, info['mapping'], 'job_id')
            if key: rows.setdefault(key, []).append((first + offset, row))
    return rows


def acknowledge(api, identifier, name, info, job, expected_link, run, expected_plan=None, review_store=None):
    # A user/Agent may have sorted rows or changed the result while downloading.
    # Re-locate the exact ID; do not write to a stale row number.
    rows = read_rows(api, identifier, name, info, run).get(job['id'], [])
    if len(rows) != 1: raise ValueError('成品已在本地；云端作业ID缺失或重复，暂不回写归档状态。')
    number, row = rows[0]; mapping = info['mapping']
    if (_cell(row, mapping, 'result') != expected_link or
            not status_completed(info.get('kind', 'oral'), _cell(row, mapping, 'status')) or
            _cell(row, mapping, 'task_id') != job['payload']['task_id']):
        raise ValueError('成品已在本地；云端任务或链接已变化，未给新结果误标已下载。')
    if expected_plan is not None and delivery_plan(info.get('kind', 'oral'), _cell(row, mapping, 'spoken')) != expected_plan:
        raise ValueError('成品已在本地，但云端分段计划已改变，未给新计划误标归档。')
    if info.get('kind') == 'image' and _cell(row, mapping, 'quality') not in {'通过', '轻微损失已披露'}:
        raise ValueError('成品已在本地，但云端质检状态已改变，未回写下载完成。')
    if review_store is not None:
        cache_review(review_store, job['id'], _cell(row, mapping, 'ai_review'), available='ai_review' in mapping,
            records=review_store.delivery_files(job['id']))
    if _cell(row, mapping, 'archive') in ARCHIVED: return
    target = a1(name, column(mapping['archive'] + 1) + str(number))
    rule = _execute(api.spreadsheets().get(spreadsheetId=identifier, ranges=[target], includeGridData=True,
        fields='sheets(data(rowData(values(dataValidation))))'))
    options = None
    for sheet in rule.get('sheets', []):
        for block in sheet.get('data', []):
            for entry in block.get('rowData', []):
                for cell in entry.get('values', []):
                    condition = cell.get('dataValidation', {}).get('condition', {})
                    if condition.get('type') == 'ONE_OF_LIST':
                        options = {option.get('userEnteredValue') for option in condition.get('values', [])}
                    if condition.get('type') == 'ONE_OF_RANGE':
                        raise ValueError('成品已在本地；范围型归档下拉框需先核对“已下载”选项，暂未回写。')
    run.check_canceled()
    terminal = archive_value(info.get('kind', 'oral'), options)
    _execute(api.spreadsheets().values().update(spreadsheetId=identifier, range=target,
        valueInputOption='RAW', body={'values': [[terminal]]}))


def run_receipts(payload, run, *, sheets=None, drive=None, fetch=download_file, clock=time.time):
    if payload.get('allow_incomplete') and not (payload.get('ids') or payload.get('directories')):
        raise ValueError('提前收取必须明确选择任务；未下载任何未完成成品。')
    if payload.get('force') and not (payload.get('ids') or payload.get('directories')):
        raise ValueError('重新收取必须明确选择任务；未重置任何下载记录。')
    store = Store(payload['root']); token = uuid4().hex
    if not store.claim_delivery_run(token): return {'message': '另一进程正在收取成品。', 'downloaded': 0, 'pending': False}
    leased = LeasedRun(run, store, token)
    try:
        leased.check_canceled()
        if sheets is None: sheets = load_google_service(payload['sheets_authorization'])
        drive_error = ''
        if drive is None and payload.get('drive_authorization'):
            try: drive = load_google_service(payload['drive_authorization'])
            except Exception as error: drive_error = error_message(error)
        groups = defaultdict(list)
        settings = store.settings()
        default = ''
        if settings.get('url') and settings.get('sheet'):
            default = spreadsheet_id(settings['url']) + ':' + settings['sheet']
        jobs = store.jobs(payload.get('ids'), limit=100000)
        if payload.get('directories') is not None:
            selected = {directory_key(path) for path in payload['directories']}
            matched = set(); chosen = []
            # Latest submitted job per original directory; never let an old
            # version or a new local draft re-download an unrelated result.
            for job in reversed(jobs):
                directory = directory_key(job['payload'].get('target_dir') or '')
                identity = (directory, job['payload'].get('source_key')) if task_kind(job['payload']) == 'image' else directory
                if directory in selected and identity not in matched and job['state'] in {'submitted', 'sending', 'uncertain'}:
                    chosen.append(job); matched.add(identity)
            jobs = chosen
        if payload.get('force'): store.retry_deliveries([job['id'] for job in jobs], force=True)
        for job in jobs:
            image_default = (spreadsheet_id(settings.get('image_url') or settings['url']) + ':' + settings.get('image_sheet', '图片转动画')) if (settings.get('image_url') or settings.get('url')) else ''
            destination = job['destination'] or (image_default if task_kind(job['payload']) == 'image' else default)
            if destination: groups[destination].append(job)
        downloaded, failed, pending, budget, ack_budget = 0, 0, False, 8, 8
        partial_count=0
        continue_ids = []
        for destination, jobs in groups.items():
            leased.check_canceled()
            identifier, name = destination.split(':', 1)
            spreadsheet_id('https://docs.google.com/spreadsheets/d/' + identifier + '/edit')
            try:
                info = inspect(sheets, identifier, name, leased)
                if not {'result', 'archive'}.issubset(info['mapping']): raise ValueError('缺少“成品Drive链接”或“归档状态”列；未修改表格。')
                rows = read_rows(sheets, identifier, name, info, leased)
            except Exception as error:
                leased.check_canceled()
                for job in jobs: store.delivery_note(job['id'], '收取待重试', error_message(error))
                failed += len(jobs); continue
            for job in jobs:
                leased.check_canceled()
                matches = rows.get(job['id'], [])
                if not matches:
                    if job['state'] in {'submitted', 'sending', 'uncertain'}:
                        store.delivery_note(job['id'], '未找到云端记录', '请核对原分页；不会生成新任务或下载其他行。')
                    continue
                if len(matches) != 1:
                    store.delivery_note(job['id'], '需核对重复ID', '云端存在多个相同作业ID，未猜测下载。'); continue
                _number, row = matches[0]; mapping = info['mapping']
                kind = info.get('kind', 'oral')
                if kind != task_kind(job['payload']):
                    store.delivery_note(job['id'], '任务类型不符', '原提交任务与云端表头类型不同，未下载或回写。'); continue
                if _cell(row, mapping, 'task_id') != job['payload']['task_id']:
                    store.delivery_note(job['id'], '任务编号不符', '作业ID对应的任务编号与提交快照不同，未下载。'); continue
                records = store.delivery_files(job['id'])
                cache_review(store, job['id'], _cell(row, mapping, 'ai_review'), available='ai_review' in mapping, records=records)
                if job['state'] != 'submitted': store.mark([job['id']], 'submitted', destination, '成品监视已按作业ID核实云端记录')
                cloud_status=_cell(row,mapping,'status')
                cloud_done=status_completed(kind,cloud_status)
                user_fill=needs_user_segments(kind,cloud_status)
                cloud_plan=delivery_plan(kind,_cell(row,mapping,'spoken'))
                cloud_link=_cell(row,mapping,'result')
                cloud={'status':cloud_status,'completed':cloud_done,'needs_user_segments':user_fill,
                    'source':hashlib.sha256(cloud_link.encode()).hexdigest() if cloud_link else '',
                    'expected':cloud_plan['count'] if cloud_plan else None}
                if records.get('@cloud')!=cloud:store.save_delivery(job['id'],'@cloud',cloud)
                records['@cloud']=cloud
                manual_partial=bool(payload.get('allow_incomplete')) and kind=='oral' and cloud_status in {'执行中','已领取','待确认','失败'}
                if not cloud_done and not manual_partial and not user_fill:
                    previous=records.get('@partial',{}); previous_link=_cell(row,mapping,'result')
                    if (kind=='oral' and previous_link and previous.get('source')==hashlib.sha256(previous_link.encode()).hexdigest()
                            and previous.get('versions')):
                        amount=len(previous['versions']); expected=previous.get('total',amount)
                        state='已收取部分' if amount<expected else '已收取·任务未完成'
                        store.delivery_note(job['id'],state,f'已有 {amount}/{expected} 个视频已收取。\n'+previous.get('detail','')+
                            '\n云端执行状态：'+cloud_status+'；可手动检查后续片段，未自动回写整项归档。'+
                            ('\n云端备注：'+_cell(row,mapping,'notes') if _cell(row,mapping,'notes') else ''))
                        continue
                    store.delivery_note(job['id'], '等待制作完成', '云端状态：' + _cell(row, mapping, 'status')); continue
                if kind == 'image' and _cell(row, mapping, 'quality') not in {'通过', '轻微损失已披露'}:
                    store.delivery_note(job['id'], '需核对质检', '已交付但未明确通过画面质检，未自动收取。'); continue
                link_text = _cell(row, mapping, 'result')
                if not link_text:
                    store.delivery_note(job['id'], '等待成品链接', '成品Drive链接为空；需提供该作业正确的成品链接，不会猜测其他目录或改用素材链接。'); continue
                signature = hashlib.sha256(link_text.encode()).hexdigest()
                discovery = records.get('@discovery', {})
                if (discovery.get('source') == signature and discovery.get('scope') == DISCOVERY_SCOPE
                        and bool(discovery.get('handoff'))==user_fill
                        and (not user_fill or discovery.get('handoff_policy') == HANDOFF_POLICY)
                        and discovery.get('retry_at', 0) > clock()):
                    pending = True; continue
                try:
                    directory = output_directory(job)
                    if drive_error: raise ValueError('Drive授权暂不可用：' + drive_error)
                    links = result_links(link_text)
                    if kind == 'image' and (len(links) != 1 or '/folders/' in links[0]):
                        raise ValueError('图片任务需一个直接视频链接，不能使用整批成品文件夹；未猜测收取其他任务。')
                    # The declared result folder is the delivery boundary,
                    # including nested folders. Validate all segment identities
                    # before downloading; never select an arbitrary duplicate.
                    files = collect_files(links, drive, leased)
                    plan = delivery_plan(kind, _cell(row, mapping, 'spoken'))
                    selection_problem = ''
                    selection_note = ''
                    supplemental = []
                    if user_fill:
                        files, selection_problem = handoff_files(files, plan, job['payload']['task_id'], records)
                    elif cloud_done:
                        planned, selection_note = completed_files(files, plan, job['payload']['task_id'], records)
                        selected_ids = {item['id'] for item in planned}
                        supplemental = [item for item in files if item['id'] not in selected_ids]
                        files = planned
                    plan_problem = check_parts(files, plan, job['payload']['task_id'], records)
                    planned_files = list(files)
                    files = planned_files + supplemental
                    supplemental_ids = sorted(item['id'] for item in supplemental)
                    supplemental_set = set(supplemental_ids)
                    supplemental_directory = output_directory(job, supplemental=True) if supplemental else None
                    def item_directory(item):
                        return supplemental_directory if item['id'] in supplemental_set else directory
                    expected_total = max(len(files), (plan['count'] if plan else 0) + len(supplemental))
                    active_ids = sorted(item['id'] for item in files)
                    if (discovery.get('state') != 'checked' or discovery.get('source') != signature or
                            discovery.get('scope') != DISCOVERY_SCOPE or
                            bool(discovery.get('handoff'))!=user_fill or
                            user_fill and discovery.get('handoff_policy') != HANDOFF_POLICY or
                            discovery.get('total') != expected_total or discovery.get('file_ids') != active_ids or
                            discovery.get('supplemental_ids', []) != supplemental_ids):
                        store.save_delivery(job['id'], '@discovery', {'state': 'checked', 'source': signature, 'scope': DISCOVERY_SCOPE,'handoff':user_fill,
                            'handoff_policy': HANDOFF_POLICY if user_fill else None,
                            'total': expected_total, 'file_ids': active_ids, 'selection_note': selection_note,
                            'supplemental_ids': supplemental_ids})
                    # Missing segments are a delivery warning, not a download
                    # embargo. Wrong task IDs/duplicate/out-of-range ordinals
                    # still raise above; every transferred file stays verified.
                    completed, attempted = 0, 0
                    def report_count():
                        leased.progress_count=f'任务 {job["payload"]["task_id"]} · {completed}/{expected_total}'
                        leased.report(RECEIPT_PROGRESS_PREFIX+leased.progress_count)
                    for item in files:
                        leased.check_canceled(); record = records.get(item['id'], {})
                        if not record.get('force_download') and cached_file(record, item_directory(item)) and (not item.get('md5Checksum') or record.get('md5') == item['md5Checksum']):
                            completed += 1; continue
                        if record.get('retry_at', 0) > clock(): pending = True; continue
                        if budget <= 0 or attempted >= 8: pending = True; continue
                        attempted += 1; budget -= 1
                        store.delivery_note(job['id'], '正在下载', f'已收取 {completed}/{expected_total} 个视频')
                        report_count()
                        try:
                            value = fetch(job, item, item_directory(item), drive, leased)
                            if not cached_file(value, item_directory(item)): raise ValueError('下载器未返回完整的原任务目录成品。')
                            if item['id'] in supplemental_set: value['category'] = 'supplemental'
                            store.save_delivery(job['id'], item['id'], value)
                            completed += 1; downloaded += 1
                            report_count()
                        except Exception as error:
                            leased.check_canceled()
                            attempts = int(record.get('attempts', 0)) + 1
                            store.save_delivery(job['id'], item['id'], {**record, 'state': 'retry', 'attempts': attempts,
                                'retry_at': clock() + min(3600, 60 * 2 ** min(attempts - 1, 6)), 'detail': error_message(error)})
                            failed += 1; pending = True
                    if completed == len(files):
                        plan_problem = check_parts(planned_files, plan, job['payload']['task_id'], store.delivery_files(job['id']))
                        plan_problem = '\n'.join(value for value in (plan_problem, selection_problem) if value)
                        if plan_problem or not cloud_done:
                            verified=store.delivery_files(job['id'])
                            store.save_delivery(job['id'],'@partial',{'state':'partial','source':signature,'total':expected_total,
                                'versions':{item['id']:verified[item['id']]['sha256'] for item in files},
                                'detail':plan_problem or '现有片段已收取，等待云端确认制作完成。'})
                            detail=f'已收取现有 {completed}/{expected_total} 个视频到原任务result目录。\n'
                            detail+=plan_problem or '当前已有视频已收取；云端任务尚未确认完成，不回写整项归档。'
                            detail+='\n云端执行状态：'+cloud_status
                            cloud_note=_cell(row,mapping,'notes')
                            if cloud_note: detail+='\n云端备注：'+cloud_note
                            if user_fill:detail+='\nAI已交付现有片段，剩余由用户补段；补完后将执行状态改为“已完成”，再检查并收取。'
                            pending=True; partial_count+=1
                            state='已收取部分' if plan and len(files)<plan['count'] else '收取待核对' if plan_problem else '已收取·任务未完成'
                            store.delivery_note(job['id'],state,detail); continue
                        verified=store.delivery_files(job['id'])
                        proof={'state':'received','source':signature,'total':len(files),
                            'versions':{item['id']:verified[item['id']]['sha256'] for item in files},
                            'archived':_cell(row,mapping,'archive') in ARCHIVED, 'selection_note':selection_note}
                        store.save_delivery(job['id'],'@received',proof)
                        try:
                            if _cell(row, mapping, 'archive') not in ARCHIVED or attempted:
                                if ack_budget <= 0:
                                    store.delivery_note(job['id'], '已下载·待回写', '本轮回写额度已用完，下轮继续；不会重新下载。')
                                    pending = True; continue_ids.append(job['id']); continue
                                ack_budget -= 1
                                acknowledge(sheets, identifier, name, info, job, link_text, leased, expected_plan=plan, review_store=store)
                            cloud_note = _cell(row, mapping, 'notes')
                            store.save_delivery(job['id'],'@received',{**proof,'archived':True})
                            store.delivery_note(job['id'], '已下载', f'{len(planned_files)} 个视频已校验归档到原任务的 result 目录。' +
                                ('\n' + selection_note if selection_note else '') +
                                ('\n云端交付备注（不代表已通过用户终审）：' + cloud_note if cloud_note else ''))
                        except Exception as error:
                            leased.check_canceled(); store.delivery_note(job['id'], '已下载·待回写', error_message(error))
                    else:
                        current = store.delivery_files(job['id'])
                        expected_ids = {item['id'] for item in files}
                        errors = [value for key, value in current.items() if key in expected_ids and value.get('state') == 'retry']
                        detail = f'已收取 {completed}/{expected_total} 个视频。'
                        if errors:
                            error = errors[-1]
                            detail += f'下载尝试累计 {error.get("attempts", 0)} 次，稍后自动重试；' + error.get('detail', '')
                        store.delivery_note(job['id'], '下载待重试' if errors else '等待继续下载', detail)
                        # Continue only known, eligible transfers held by this
                        # pass's budget. Failures/reviews wait for the next poll
                        # or an explicit manual retry, never a tight poll loop.
                        if any((current.get(item['id'], {}).get('force_download') or
                                not cached_file(current.get(item['id'], {}), item_directory(item)) or
                                item.get('md5Checksum') and item['md5Checksum'] != current.get(item['id'], {}).get('md5')) and
                               current.get(item['id'], {}).get('retry_at', 0) <= clock() for item in files):
                            continue_ids.append(job['id'])
                except Exception as error:
                    leased.check_canceled(); failed += 1; pending = True
                    same_scope = (discovery.get('source') == signature and discovery.get('scope') == DISCOVERY_SCOPE
                        and bool(discovery.get('handoff'))==user_fill
                        and (not user_fill or discovery.get('handoff_policy') == HANDOFF_POLICY))
                    attempts = int(discovery.get('attempts', 0)) + 1 if same_scope else 1
                    store.save_delivery(job['id'], '@discovery', {**(discovery if same_scope else {}), 'state': 'retry', 'source': signature, 'scope': DISCOVERY_SCOPE,'handoff':user_fill,
                        'handoff_policy': HANDOFF_POLICY if user_fill else None,
                        'attempts': attempts, 'retry_at': clock() + min(3600, 60 * 2 ** min(attempts - 1, 6)),
                        'total': discovery.get('total') if same_scope else None, 'detail': error_message(error)})
                    store.delivery_note(job['id'], '收取待重试', f'检查尝试累计 {attempts} 次；' + error_message(error))
        from .notice_data import notice_snapshot
        badges=notice_snapshot(store)
        if 'ui_generation' in payload: badges['ui_generation']=payload['ui_generation']
        return {'message': ('所选任务没有匹配的已提交 Agent 作业。' if not groups else f'成品检查完成：本次下载 {downloaded} 个视频。') +
                (f'{partial_count}项已先收现有片段，未标整项归档。' if partial_count else '')+
                ('仍有任务待补、待核对或重试。' if pending or failed else ''),
                'downloaded': downloaded, 'pending': pending, 'failed': failed, 'partial':partial_count, 'continue_ids': continue_ids,
                'task_badges': badges}
    finally: store.release_delivery_run(token)
