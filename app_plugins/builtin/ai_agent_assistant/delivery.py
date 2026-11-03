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

DONE = {'已完成', '完成', '制作完成', '生成完成', 'completed', 'complete', 'done'}


class LeasedRun:
    def __init__(self, run, store, token):
        self.run, self.store, self.token, self.renewed = run, store, token, time.monotonic()
    def check_canceled(self):
        self.run.check_canceled()
        if time.monotonic() - self.renewed > 20:
            self.store.renew_delivery_run(self.token); self.renewed = time.monotonic()
    def report(self, *args): self.check_canceled(); self.run.report(*args)


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


def acknowledge(api, identifier, name, info, job, expected_link, run):
    # A user/Agent may have sorted rows or changed the result while downloading.
    # Re-locate the exact ID; do not write to a stale row number.
    rows = read_rows(api, identifier, name, info, run).get(job['id'], [])
    if len(rows) != 1: raise ValueError('成品已在本地；云端作业ID缺失或重复，暂不回写归档状态。')
    number, row = rows[0]; mapping = info['mapping']
    if (_cell(row, mapping, 'result') != expected_link or
            _cell(row, mapping, 'status').casefold() not in DONE or
            _cell(row, mapping, 'task_id') != job['payload']['task_id']):
        raise ValueError('成品已在本地；云端任务或链接已变化，未给新结果误标已下载。')
    if _cell(row, mapping, 'archive') == '已下载': return
    target = a1(name, column(mapping['archive'] + 1) + str(number))
    rule = _execute(api.spreadsheets().get(spreadsheetId=identifier, ranges=[target], includeGridData=True,
        fields='sheets(data(rowData(values(dataValidation))))'))
    for sheet in rule.get('sheets', []):
        for block in sheet.get('data', []):
            for entry in block.get('rowData', []):
                for cell in entry.get('values', []):
                    condition = cell.get('dataValidation', {}).get('condition', {})
                    if condition.get('type') == 'ONE_OF_LIST' and '已下载' not in {
                            option.get('userEnteredValue') for option in condition.get('values', [])}:
                        raise ValueError('成品已在本地；归档下拉框不含“已下载”，未改写格式或验证。')
                    if condition.get('type') == 'ONE_OF_RANGE':
                        raise ValueError('成品已在本地；范围型归档下拉框需先核对“已下载”选项，暂未回写。')
    run.check_canceled()
    _execute(api.spreadsheets().values().update(spreadsheetId=identifier, range=target,
        valueInputOption='RAW', body={'values': [['已下载']]}))


def run_receipts(payload, run, *, sheets=None, drive=None, fetch=download_file, clock=time.time):
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
        for job in store.jobs(payload.get('ids'), limit=100000):
            destination = job['destination'] or default
            if destination: groups[destination].append(job)
        downloaded, failed, pending, budget, ack_budget = 0, 0, False, 8, 8
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
                if _cell(row, mapping, 'task_id') != job['payload']['task_id']:
                    store.delivery_note(job['id'], '任务编号不符', '作业ID对应的任务编号与提交快照不同，未下载。'); continue
                if job['state'] != 'submitted': store.mark([job['id']], 'submitted', destination, '成品监视已按作业ID核实云端记录')
                if _cell(row, mapping, 'status').casefold() not in DONE:
                    store.delivery_note(job['id'], '等待制作完成', '云端状态：' + _cell(row, mapping, 'status')); continue
                link_text = _cell(row, mapping, 'result')
                if not link_text:
                    store.delivery_note(job['id'], '等待成品链接', '已完成但成品Drive链接为空，稍后自动检查。'); continue
                signature = hashlib.sha256(link_text.encode()).hexdigest()
                records = store.delivery_files(job['id'])
                discovery = records.get('@discovery', {})
                if discovery.get('source') == signature and discovery.get('retry_at', 0) > clock():
                    pending = True; continue
                try:
                    directory = output_directory(job)
                    if drive_error: raise ValueError('Drive授权暂不可用：' + drive_error)
                    files = collect_files(result_links(link_text), drive, leased)
                    if discovery.get('state') != 'checked' or discovery.get('source') != signature or discovery.get('total') != len(files):
                        store.save_delivery(job['id'], '@discovery', {'state': 'checked', 'source': signature, 'total': len(files)})
                    completed, attempted = 0, 0
                    for item in files:
                        leased.check_canceled(); record = records.get(item['id'], {})
                        if cached_file(record, directory) and (not item.get('md5Checksum') or record.get('md5') == item['md5Checksum']):
                            completed += 1; continue
                        if record.get('retry_at', 0) > clock(): pending = True; continue
                        if budget <= 0 or attempted >= 8: pending = True; continue
                        attempted += 1; budget -= 1
                        store.delivery_note(job['id'], '正在下载', f'已收取 {completed}/{len(files)} 个视频')
                        try:
                            value = fetch(job, item, directory, drive, leased)
                            if not cached_file(value, directory): raise ValueError('下载器未返回完整的原任务目录成品。')
                            store.save_delivery(job['id'], item['id'], value)
                            completed += 1; downloaded += 1
                        except Exception as error:
                            leased.check_canceled()
                            attempts = int(record.get('attempts', 0)) + 1
                            store.save_delivery(job['id'], item['id'], {'state': 'retry', 'attempts': attempts,
                                'retry_at': clock() + min(3600, 60 * 2 ** min(attempts - 1, 6)), 'detail': error_message(error)})
                            failed += 1; pending = True
                    if completed == len(files):
                        try:
                            if _cell(row, mapping, 'archive') != '已下载' or attempted:
                                if ack_budget <= 0:
                                    store.delivery_note(job['id'], '已下载·待回写', '本轮回写额度已用完，下轮继续；不会重新下载。')
                                    pending = True; continue
                                ack_budget -= 1
                                acknowledge(sheets, identifier, name, info, job, link_text, leased)
                            store.delivery_note(job['id'], '已下载', f'{completed} 个视频已校验归档到原任务的 result 目录。')
                        except Exception as error:
                            leased.check_canceled(); store.delivery_note(job['id'], '已下载·待回写', error_message(error))
                    else:
                        current = store.delivery_files(job['id'])
                        expected_ids = {item['id'] for item in files}
                        errors = [value for key, value in current.items() if key in expected_ids and value.get('state') == 'retry']
                        detail = f'已收取 {completed}/{len(files)} 个视频。'
                        if errors:
                            error = errors[-1]
                            detail += f'下载尝试累计 {error.get("attempts", 0)} 次，稍后自动重试；' + error.get('detail', '')
                        store.delivery_note(job['id'], '下载待重试' if errors else '等待继续下载', detail)
                except Exception as error:
                    leased.check_canceled(); failed += 1; pending = True
                    attempts = int(discovery.get('attempts', 0)) + 1 if discovery.get('source') == signature else 1
                    store.save_delivery(job['id'], '@discovery', {'state': 'retry', 'source': signature,
                        'attempts': attempts, 'retry_at': clock() + min(3600, 60 * 2 ** min(attempts - 1, 6)),
                        'total': discovery.get('total'), 'detail': error_message(error)})
                    store.delivery_note(job['id'], '收取待重试', f'检查尝试累计 {attempts} 次；' + error_message(error))
        from .notice_data import notice_snapshot
        return {'message': f'成品检查完成：本次下载 {downloaded} 个视频。' + ('仍有任务等待重试或继续收取。' if pending or failed else ''),
                'downloaded': downloaded, 'pending': pending, 'failed': failed, 'task_badges': notice_snapshot(store)}
    finally: store.release_delivery_run(token)
