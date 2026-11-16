"""Durable per-attempt state machine; success, retry and unknown are distinct."""
import base64
import hashlib
import io
import json
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image
from model.AppDataStorage import data_file, data_directory
from model.AIProviderKeys import (KEYS_CONFIG_KEY, key_pool, provider_key_identity,
                                 read_provider_key)
from model.SensitiveData import redact_sensitive_text
from .client import APIError, BatchClient
from .prompts import safe_id
from .store import Store, summary
from .completion import completion_target

TERMINAL = {'completed', 'failed', 'expired', 'cancelled'}
SETTLED = {'done', 'partial', 'failed', 'cancelled'}


def credential_identity(config):
    # Stable across renaming/re-entering the same Key (DPAPI ciphertext is not).
    return 'key-sha256:' + hashlib.sha256(read_provider_key(config, 'openai').encode()).hexdigest()


def credential_config(config, identity):
    """Resume with exactly the submitting credential, even if active key changed."""
    if provider_key_identity(config, 'openai') == identity:
        return config
    try:
        if credential_identity(config) == identity:
            return config
    except (ValueError, RuntimeError, OSError):
        pass
    stored = config.get(KEYS_CONFIG_KEY) or {}
    for row in key_pool(stored.get('openai')).get('items', []):
        candidate = {KEYS_CONFIG_KEY: {'openai': row['encrypted']}}
        if provider_key_identity(candidate, 'openai') == identity:
            return candidate
        try:
            if credential_identity(candidate) == identity:
                return candidate
        except (ValueError, RuntimeError, OSError):
            continue
    raise ValueError('原批次使用的OpenAI Key已移除或更换，无法查询；请恢复原Key，不会用其他账号重提。')


def active_attempt(job):
    return job['attempts'][-1] if job['attempts'] else None


def new_attempt(job, *, retry=False):
    if job['attempts'] and job['status'] not in SETTLED:
        raise ValueError('原批次尚未核对完成，不能重复提交。')
    items = [i for i in job['items'] if i['status'] == ('failed' if retry else 'pending')]
    if not items:
        raise ValueError('没有可提交的图片。')
    attempt = {'token': uuid4().hex, 'created': time.time(), 'state': 'prepared',
               'item_ids': [i['id'] for i in items], 'batch_id': '', 'input_file': '',
               'reference_ids': [], 'counts': {}, 'cloud_status': ''}
    job['attempts'].append(attempt)
    for item in items:
        item.update(status='pending', error='')
    job.update(status='ready', error='', cancel_requested=False)
    return attempt


def batch_lines(job, attempt):
    if job.get('planning') and job['planning']['state'] != 'done':
        raise ValueError('提示词创新尚未完整通过校验，不能提交生图。')
    selected = set(attempt['item_ids'])
    spec = job['spec']
    endpoint = '/v1/images/edits' if job['references'] else '/v1/images/generations'
    lines = []
    for item in job['items']:
        if item['id'] not in selected:
            continue
        body = {'model': spec['model'], 'prompt': item['prompt'], 'n': 1,
                'quality': spec['quality'], 'size': spec['size'], 'output_format': 'png'}
        if job['references']:
            if len(attempt['reference_ids']) != len(job['references']):
                raise ValueError('人物参考图尚未上传完成，不能无参考图提交。')
            body['images'] = [{'file_id': identifier} for identifier in attempt['reference_ids']]
        lines.append(json.dumps({'custom_id': attempt['token'] + ':' + item['id'],
                                'method': 'POST', 'url': endpoint, 'body': body}, ensure_ascii=False))
    raw = ('\n'.join(lines) + '\n').encode('utf-8')
    if len(raw) > 190 * 1024 * 1024:
        raise ValueError('批处理文件过大，请减少数量。')
    return endpoint, raw


class Service:
    def __init__(self, store, client, check=lambda: None):
        self.store, self.client, self.check = store, client, check

    def submit(self, job):
        self.store.prepare_snapshot(job)
        attempt = active_attempt(job)
        if attempt is None:
            attempt = new_attempt(job)
            self.store.save(job)
        if attempt['state'] != 'prepared':
            return self.advance(job)
        try:
            while len(attempt['reference_ids']) < len(job['references']):
                self.check()
                ref = job['references'][len(attempt['reference_ids'])]
                identifier = self.client.upload(self.store.asset(job, ref), ref['file'], 'user_data')
                attempt['reference_ids'].append(identifier)
                self.store.save(job)
            endpoint, raw = batch_lines(job, attempt)
            if not attempt['input_file']:
                attempt['input_file'] = self.client.upload(raw, 'remix-' + attempt['token'] + '.jsonl', 'batch')
                self.store.save(job)
            self.check()
            # Durable intent BEFORE POST. A crash/timeout after this is unknown,
            # never "pending" and never an automatic second billable POST.
            attempt['state'] = 'submitting'
            job['status'] = 'submitting'
            self.store.save(job)
            batch = self.client.create(attempt['input_file'],
                {'remix_job': job['id'], 'attempt': attempt['token'], 'endpoint': endpoint})
            attempt['batch_id'] = safe_id(batch.get('id'), 'batch_')
            attempt['state'] = 'cloud'
            job['status'] = 'running'
            job['error'] = ''
            self.store.save(job)
            return self.apply_cloud(job, batch)
        except APIError as error:
            if error.definite:
                return self.fail_attempt(job, str(error))
            if attempt['state'] == 'submitting':
                attempt['state'] = 'unknown'
                job['status'] = 'unknown'
            else:
                job['network_failures'] = job.get('network_failures', 0) + 1
                if job['network_failures'] >= 3:
                    job['status'] = 'attention'
            # Upload-only failure can be safely resumed; no generation POST yet.
            job['error'] = redact_sensitive_text(error)[:800]
            self.store.save(job)
            return job

    def fail_attempt(self, job, message):
        attempt = active_attempt(job)
        attempt['state'] = 'finished'
        selected = set(attempt['item_ids'])
        for item in job['items']:
            if item['id'] in selected and item['status'] != 'done':
                item.update(status='failed', error=message[:800])
        job.update(status='partial' if summary(job)[0] else 'failed', error=message[:800])
        self.store.save(job)
        return job

    def advance(self, job):
        attempt = active_attempt(job)
        if job['status'] in SETTLED:
            return job
        if attempt is None or attempt['state'] == 'prepared':
            if job['cancel_requested']:
                if attempt is None:
                    new_attempt(job)
                self.fail_attempt(job, '已取消，尚未提交云端。')
                job['status'] = 'cancelled'; self.store.save(job)
                return job
            return self.submit(job)
        if not attempt['batch_id']:
            batch = self.client.recover(job['id'], attempt['token'], attempt['created'])
            if batch is None:
                job.update(status='unknown', error='提交结果待核对：尚未查到对应批次。为避免重复扣费，不自动重新生成；可稍后检查或在控制台核对。')
                attempt['state'] = 'unknown'
                self.store.save(job)
                return job
            attempt['batch_id'] = safe_id(batch.get('id'), 'batch_')
            attempt['state'] = 'cloud'
            self.store.save(job)
        if job['cancel_requested'] and not attempt.get('cancel_sent'):
            try:
                self.client.cancel_batch(attempt['batch_id'])
            except APIError as error:
                if not error.definite:
                    raise
                # A race can finish the batch just before cancel. Query the
                # authoritative state rather than getting stuck cancelling.
            attempt['cancel_sent'] = True
            self.store.save(job)
        return self.apply_cloud(job, self.client.retrieve(attempt['batch_id']))

    def apply_cloud(self, job, batch):
        attempt = active_attempt(job)
        if safe_id(batch.get('id'), 'batch_') != attempt['batch_id']:
            raise ValueError('云端批次身份不匹配，拒绝串收结果。')
        attempt['cloud_status'] = batch.get('status', '')
        if attempt['cloud_status'] not in TERMINAL | {'validating', 'in_progress', 'finalizing', 'cancelling'}:
            raise ValueError('云端返回未知批次状态，请检查账号API响应；不会无限冒充生成中。')
        attempt['counts'] = batch.get('request_counts') or {}
        job['last_check'] = time.time()
        if batch.get('status') not in TERMINAL:
            job.update(status='cancelling' if job['cancel_requested'] else 'running', error='')
            self.store.save(job)
            return job
        job['status'] = 'collecting'
        self.store.save(job)
        seen = set()
        local_errors = []
        for field in ('output_file_id', 'error_file_id'):
            if not batch.get(field):
                continue
            for row in self.client.lines(batch[field]):
                self.check()
                identifier = row.get('custom_id', '')
                if identifier in seen:
                    raise ValueError('批次结果出现重复编号，已保存的图片保留，请核对云端结果。')
                seen.add(identifier)
                custom = str(row.get('custom_id', ''))
                prefix = attempt['token'] + ':'
                if not custom.startswith(prefix) or custom[len(prefix):] not in attempt['item_ids']:
                    raise ValueError('结果不属于当前执行批次，拒绝写入。')
                try:
                    self.accept_line(job, attempt, row)
                except (ValueError, OSError) as error:
                    # One corrupt/missing image or output collision must not
                    # prevent collecting the remaining independent results.
                    local_errors.append(redact_sensitive_text(error)[:400])
        if local_errors:
            job['collection_failures'] = job.get('collection_failures', 0) + 1
            job.update(status='attention' if job['collection_failures'] >= 3 else 'collecting',
                       error='结果收取需处理（不会重新生成）：' + '; '.join(local_errors)[:800])
            self.store.save(job)
            return job
        errors = (batch.get('errors') or {}).get('data', [])
        reason = redact_sensitive_text('; '.join(str(e.get('message', '')) for e in errors))[:800]
        for item in job['items']:
            if item['id'] in attempt['item_ids'] and item['status'] != 'done':
                item['status'] = 'failed'
                item['error'] = item['error'] or reason or ('云端批次' + batch['status'] + '，该图片未返回有效结果。')
        attempt['state'] = 'finished'
        done, failed, _ = summary(job)
        job.update(collection_failures=0, network_failures=0, status='done' if done == len(job['items']) else
                   ('cancelled' if batch['status'] == 'cancelled' else 'partial' if done else 'failed'), error=reason)
        self.store.save(job)
        return job

    def accept_line(self, job, attempt, row):
        custom = str(row.get('custom_id', ''))
        prefix = attempt['token'] + ':'
        if not custom.startswith(prefix) or custom[len(prefix):] not in attempt['item_ids']:
            raise ValueError('结果不属于当前执行批次，拒绝写入。')
        item = next(i for i in job['items'] if i['id'] == custom[len(prefix):])
        response = row.get('response') or {}
        body = response.get('body') or {}
        if row.get('error') or response.get('status_code') != 200:
            item.update(status='failed', error=redact_sensitive_text(str(row.get('error') or body.get('error') or '云端图片生成失败'))[:800])
            self.store.save(job)
            return
        data = body.get('data') or []
        if len(data) != 1 or not isinstance(data[0], dict) or not data[0].get('b64_json'):
            raise ValueError('该图片未返回预期的一张base64图片；保留批次，不重新生成。')
        raw = base64.b64decode(data[0]['b64_json'], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'PNG' or image.width * image.height > 40000000:
                raise ValueError('生成结果不是有效PNG或尺寸异常。')
            image.verify()
        destination = Path(job['output']).resolve()
        if destination.name.split('_')[-1] != job['id'][:8]:
            raise ValueError('输出目录身份不正确，不能写入。')
        path = destination / f'{item["index"] + 1:04d}.png'
        managed = data_file(path)
        digest = hashlib.sha256(raw).hexdigest()
        if managed.exists():
            if hashlib.sha256(managed.read_bytes()).hexdigest() != digest:
                raise ValueError('输出位置已有不同内容，保留旧文件，请移动冲突文件后检查结果。')
        else:
            managed.write_bytes(raw)
        # If interrupted after the PNG write, replay compares the content hash.
        data_file(destination / f'{item["index"] + 1:04d}.json').write_json({
            'job_id': job['id'], 'custom_id': custom, 'batch_id': attempt['batch_id'],
            'submission_mode': job['spec'].get('mode', 'batch'),
            'creative_plan': item.get('plan'), 'rewrite_model': job['spec'].get('rewrite_model'),
            'original_prompt': job['spec']['prompt'], 'prompt': item['prompt'],
            'innovation': job['spec']['innovation'], 'model': job['spec']['model'],
            'usage': body.get('usage'), 'sha256': digest})
        item.update(status='done', error='', path=str(path), sha256=digest)
        self.store.save(job)


def run_operation(payload, context):
    store = Store(payload['root'])
    with store.runtime_lock():
        return _run_operation(store, payload, context)


def _run_operation(store, payload, context):
    """Host worker entry: no GUI objects and no full plugin context captured."""
    action = payload.get('action', 'check')
    if action == 'snapshot':
        return {'jobs': store.all(), 'draft': store.draft()}
    if action == 'draft':
        store.draft(payload['draft'])
        return {'jobs': store.all()}
    if action == 'delete':
        store.delete(payload['id'])
        return {'jobs': store.all()}
    config = payload.get('config') or {}
    if action == 'models':
        key = read_provider_key(config, 'openai')
        client = BatchClient(key, context.check_canceled)
        try:
            return {'jobs': store.all(), 'draft': store.draft(), 'models': client.list_models(),
                    'model_credential': credential_identity(config)}
        finally: client.close()
    if action == 'create':
        job = store.create(payload['spec'], payload['credential'], identifier=payload['id'])
        identifiers = [job['id']]
    elif action == 'complete':
        targets = payload.get('targets')
        if not isinstance(targets, list) or not targets:
            raise ValueError('没有确认要补全的Batch批次。')
        confirmed = {}
        for target in targets:
            if not isinstance(target, dict) or not isinstance(target.get('id'), str) or target['id'] in confirmed:
                raise ValueError('补全批次列表无效或重复；未提交。')
            confirmed[target['id']] = target
        identifiers = list(confirmed)
    elif payload.get('id'):
        identifiers = [payload['id']]
    else:
        waiting = sorted((j for j in store.all() if j['status'] not in SETTLED and
                          (j['status'] not in ('attention', 'paused', 'planning_failed') or payload.get('manual')) and
                          (not payload.get('instant_only') or
                           j['spec'].get('mode', 'batch') == 'instant' and j['status'] in ('ready', 'planning'))),
                         key=lambda j: j.get('last_check', 0))
        identifiers = [j['id'] for j in waiting[:8]]
    messages = []
    for identifier in identifiers:
        context.check_canceled()
        try:
            job = store.get(identifier)
            if action == 'complete' and (completion_target(job) is None or
                                         completion_target(job) != confirmed[identifier]):
                messages.append(identifier[:8] + '：状态已变化或原请求尚未核对，跳过补全，未重新生成。')
                continue
        except (ValueError, OSError) as error:
            if action != 'complete':
                raise
            messages.append(identifier[:8] + '：' + redact_sensitive_text(error)[:800])
            continue
        job['last_check'] = time.time()
        store.save(job)
        client = None
        try:
            pinned = credential_config(config, job['credential'])
            key = read_provider_key(pinned, 'openai')
            client = BatchClient(key, context.check_canceled)
            service = Service(store, client, context.check_canceled)
            planning_bound = False
            if action == 'retry_planning' and (not job.get('planning') or job['planning']['state'] == 'done'):
                raise ValueError('该任务不需要重试提示词创新；未触发生图。')
            if job.get('planning') and job['planning']['state'] != 'done':
                from .planning import Planner
                planner = Planner(store, client, context.check_canceled)
                if action == 'retry_planning': planner.retry(job)
                if action == 'bind':
                    planner.bind(job, payload['batch_id']); planning_bound = True
                if action in ('resume', 'retry'):
                    if job['planning']['state'] in ('failed', 'unknown'):
                        raise ValueError('请使用“重试提示词创新”，不能绕过失败方案直接生图。')
                    store.clear_stop(job['id'])
                if action == 'cancel':
                    if job['spec'].get('mode', 'batch') == 'instant': store.request_stop(job['id'])
                    else: job['cancel_requested'] = True; store.save(job)
                context.report('提示词创新 · ' + identifier[:8] + f' · 已完成 {sum(c["state"] == "done" for c in job["planning"]["chunks"])} 组', 0)
                if not planner.ensure(job):
                    if job.get('error'): messages.append(identifier[:8] + '：' + job['error'])
                    continue
            if job['spec'].get('mode', 'batch') == 'instant':
                from .instant import InstantService
                report_stage = getattr(context, 'report_item_stage', lambda identifier, stage: None)
                direct = InstantService(store, client, service, context.check_canceled,
                                        stage=lambda stage: report_stage(job['id'], stage))
                if action == 'bind':
                    raise ValueError('即时任务没有Batch ID，不能关联批次。')
                if action in ('resume', 'retry', 'retry_unknown'):
                    direct.restore_receipts(job)
                    direct.resume(job, retry=action != 'resume', include_unknown=action == 'retry_unknown')
                if action == 'cancel':
                    store.request_stop(job['id'])
                context.report('即时生成 · ' + identifier[:8] + f' · 已保存 {summary(job)[0]}/{len(job["items"])}', 0)
                direct.advance(job)
                continue
            if action == 'bind' and not planning_bound:
                attempt = active_attempt(job)
                if not attempt or attempt.get('batch_id') or job['status'] not in ('unknown', 'submitting'):
                    raise ValueError('只有提交结果不明且尚无批次ID时可以手动关联。')
                batch = client.retrieve(safe_id(payload['batch_id'], 'batch_'))
                metadata = batch.get('metadata') or {}
                if metadata.get('remix_job') != job['id'] or metadata.get('attempt') != attempt['token']:
                    raise ValueError('该批次不属于当前生成记录，拒绝关联。')
                attempt.update(batch_id=batch['id'], state='cloud')
                store.save(job)
            if action in ('retry', 'complete'):
                new_attempt(job, retry=True)
                store.save(job)
            if action == 'cancel':
                job['cancel_requested'] = True
                store.save(job)
            context.report('检查图片批次 ' + identifier[:8], 0)
            service.advance(job)
            if action == 'complete':
                messages.append(identifier[:8] + f'：已处理 {len(confirmed[identifier]["item_ids"])} 张补全申请；'
                                '已成功图片保留，具体进度见列表。')
        except Exception as error:
            # Never discard durable intent or promote pending items to success.
            message = redact_sensitive_text(error)[:800]
            latest = store.get(identifier)
            latest['error'] = message
            latest['network_failures'] = latest.get('network_failures', 0) + 1
            if latest['network_failures'] >= 3 and latest['status'] not in SETTLED | {'unknown', 'submitting'}:
                latest['status'] = 'attention'
            store.save(latest)
            messages.append(identifier[:8] + '：' + message)
            context.check_canceled()
        finally:
            if client is not None:
                client.close()
    context.report('本次图片处理结束；各任务保留独立状态', 100)
    jobs = store.all()
    return {'jobs': jobs, 'messages': messages, 'draft': store.draft(),
            'auto_continue': any(j['spec'].get('mode', 'batch') == 'instant' and
                                 (j['status'] == 'ready' or j['status'] == 'planning' and
                                  j.get('planning', {}).get('state') == 'ready') for j in jobs)}
