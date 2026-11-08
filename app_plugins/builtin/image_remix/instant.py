"""Standard-price requests with per-image intent and durable response receipts."""
import time
from uuid import uuid4

from model.AppDataStorage import data_directory
from model.SensitiveData import redact_sensitive_text
from .client import APIError


class InstantService:
    def __init__(self, store, client, shared, check=lambda: None, stage=lambda value: None):
        self.store, self.client, self.shared, self.check = store, client, shared, check
        self.stage = stage

    def attempt(self, job, statuses=('pending',)):
        selected = [i for i in job['items'] if i['status'] in statuses]
        if not selected:
            raise ValueError('没有符合条件的图片。')
        attempt = {'token': uuid4().hex, 'created': time.time(), 'state': 'instant',
                   'mode': 'instant', 'item_ids': [i['id'] for i in selected],
                   'batch_id': '', 'reference_ids': [], 'counts': {}, 'requests': {}}
        job['attempts'].append(attempt)
        for item in selected:
            item.update(status='pending', error='')
        job.update(status='ready', error='', cancel_requested=False)
        self.store.save(job)
        return attempt

    def resume(self, job, *, retry=False, include_unknown=False):
        if any(i['status'] in ('received', 'submitting') for i in job['items']):
            raise ValueError('先检查并恢复已接收／未确认结果，再选择重试。')
        statuses = ['pending']
        if retry: statuses.append('failed')
        if include_unknown: statuses.append('unknown')
        self.attempt(job, tuple(statuses))
        job['allow_pending_with_unknown'] = not include_unknown
        self.store.save(job)
        self.store.clear_stop(job['id'])

    def receipt(self, job, attempt, item):
        return data_directory(self.store.root / job['id'] / 'responses' / attempt['token']).file(
            f'{item["index"]:04d}.json')

    def finish(self, job):
        states = [i['status'] for i in job['items']]
        if 'received' in states:
            job['status'] = 'attention'
        elif 'pending' in states and job.get('allow_pending_with_unknown') and not self.store.stop_requested(job['id']):
            job['status'] = 'ready'
        elif 'submitting' in states or 'unknown' in states:
            job['status'] = 'unknown'
        elif self.store.stop_requested(job['id']) and 'pending' in states:
            job['status'] = 'paused'
            job['error'] = '已停止后续图片；已完成结果保留，尚未提交的图片可继续。'
        elif 'pending' in states:
            job['status'] = 'ready'
        else:
            job['status'] = 'done' if all(s == 'done' for s in states) else 'partial' if 'done' in states else 'failed'
            job['attempts'][-1]['state'] = 'finished'
        self.store.save(job)
        return job

    def restore_receipts(self, job):
        """Replay saved bytes only; never fall back from a missing receipt to POST."""
        restored = 0
        for attempt in job['attempts']:
            if attempt.get('mode') != 'instant':
                continue
            for item in job['items']:
                if item['id'] not in attempt['item_ids'] or item['status'] == 'done':
                    continue
                # A newer explicit retry owns the item; don't consume an old reply.
                owner = next((a for a in reversed(job['attempts']) if item['id'] in a['item_ids']), None)
                if owner is not attempt:
                    continue
                receipt = self.receipt(job, attempt, item)
                request = attempt['requests'].get(item['id'], {})
                if receipt.exists():
                    row = receipt.read_json()
                    if row.get('custom_id') != attempt['token'] + ':' + item['id']:
                        raise ValueError('本地响应身份不匹配，拒绝串收图片。')
                    item['status'] = 'received'
                    self.shared.accept_line(job, attempt, row)
                    self.stage('instant:saved:' + str(item['index']))
                    request['state'] = 'done'
                    restored += 1
                    try: receipt.remove(missing_ok=True)
                    except OSError: pass  # Confirmed output remains valid.
                elif request.get('state') in ('submitting', 'received'):
                    item.update(status='unknown', error='上次即时请求没有可恢复的完整响应，可能已经计费；不会自动重新生成。')
                    request['state'] = 'unknown'
        self.store.save(job)
        return restored

    def advance(self, job):
        if job.get('planning') and job['planning']['state'] != 'done':
            raise ValueError('提示词创新尚未完整通过校验，不能即时生图。')
        if self.store.stop_requested(job['id']) and not job['attempts']:
            self.attempt(job)
            return self.finish(job)
        self.store.prepare_snapshot(job)
        if not job['attempts']:
            self.attempt(job)
        attempt = job['attempts'][-1]
        if attempt.get('mode') != 'instant':
            raise ValueError('即时任务不能接管Batch批次。')
        if self.restore_receipts(job):
            return self.finish(job)
        if any(i['status'] in ('received', 'submitting') or
               i['status'] == 'unknown' and not job.get('allow_pending_with_unknown') for i in job['items']):
            return self.finish(job)
        if job.get('cancel_requested') or self.store.stop_requested(job['id']):
            return self.finish(job)
        item = next((i for i in job['items'] if i['status'] == 'pending' and i['id'] in attempt['item_ids']), None)
        if item is None:
            return self.finish(job)
        # Upload references is not a generation. Persist each resulting ID so
        # a restart doesn't needlessly re-upload all reference pictures.
        while len(attempt['reference_ids']) < len(job['references']):
            self.check()
            ref = job['references'][len(attempt['reference_ids'])]
            identifier = self.client.upload(self.store.asset(job, ref), ref['file'], 'user_data')
            attempt['reference_ids'].append(identifier)
            self.store.save(job)
        body = {'model': job['spec']['model'], 'prompt': item['prompt'], 'n': 1,
                'quality': job['spec']['quality'], 'size': job['spec']['size'], 'output_format': 'png'}
        if job['references']:
            body['images'] = [{'file_id': value} for value in attempt['reference_ids']]
        self.check()
        if self.store.stop_requested(job['id']):
            return self.finish(job)
        request = {'state': 'submitting', 'created': time.time()}
        attempt['requests'][item['id']] = request
        item['status'] = 'submitting'
        job.update(status='running', error='')
        self.store.save(job)  # Durable per-image intent BEFORE a billable POST.
        self.stage('instant:running')
        try:
            body = self.client.generate(body, references=bool(job['references']))
        except APIError as error:
            item.update(status='failed' if error.definite else 'unknown', error=redact_sensitive_text(error)[:800])
            request['state'] = item['status']
            job.update(status='paused' if error.definite else 'unknown', error=item['error'])
            # Pause this configuration rather than cascade a quota/auth/prompt
            # error into hundreds of submissions. Other jobs remain independent.
            self.store.request_stop(job['id'])
            self.store.save(job)
            return job
        row = {'custom_id': attempt['token'] + ':' + item['id'], 'error': None,
               'response': {'status_code': 200, 'body': body}}
        # If the process dies during PNG decode/write, restart replays this
        # receipt. If saving the receipt itself fails, intent stays unknown.
        self.receipt(job, attempt, item).write_json(row)
        request['state'] = 'received'
        item['status'] = 'received'
        self.store.save(job)
        self.shared.accept_line(job, attempt, row)
        self.stage('instant:saved:' + str(item['index']))
        request['state'] = 'done'
        result = self.finish(job)
        try: self.receipt(job, attempt, item).remove(missing_ok=True)
        except OSError: pass
        return result
