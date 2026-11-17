"""Durable OpenAI Batch chat. Submission is never automatically repeated."""
import hashlib
import json
import time

from model.AIProviderKeys import read_provider_key
from model.OpenAIResponses import request_payload, normalize_response
from model.AIChatCapabilities import apply_options
from model.AIChatSources import response_metadata
from model.OfflineMode import require_online
from model.SensitiveData import redact_sensitive_text
# Reuse the application's existing fixed-origin, no-retry Batch transport.
from app_plugins.builtin.image_remix.client import BatchClient, APIError


TERMINAL = {'completed', 'failed', 'expired', 'cancelled'}


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def build_body(payload, store):
    if payload['provider'] != 'openai':
        raise ValueError('Batch聊天目前只支持OpenAI官方接口。')
    options = payload['options']
    if options.get('web_search'):
        raise ValueError('本版Batch聊天暂不支持联网工具；请手动关闭联网或使用即时请求。')
    messages = store.request_messages(payload['conversation'], payload['prompt_id'], options['rounds'],
        text_limit=options.get('context_chars', 0), include_history_images=options.get('history_images', True),
        system=options.get('system', ''))
    body = request_payload(payload['model'], messages, options.get('system', ''), options['max_tokens'], False)
    apply_options('openai', payload['model'], body, options, None)
    return body


def submit_batch(payload, job, *, store, client_factory=None):
    require_online('提交AI聊天Batch')
    client_factory = client_factory or BatchClient
    body = build_body(payload, store)
    key = read_provider_key(payload['config'], 'openai')
    record = store.prepare_batch(payload['conversation'], payload['prompt_id'], fingerprint(key), body)
    if record['state'] != 'prepared':
        # Even after a process crash, returning here never creates a second job.
        return dict(conversation=record['conversation'], batch=record)
    body = json.loads(record['body'])
    client = client_factory(key, cancel=job.check_canceled)
    try:
        job.check_canceled()
        line = dict(custom_id=record['id'], method='POST', url='/v1/responses', body=body)
        raw = (json.dumps(line, ensure_ascii=False) + '\n').encode()
        if len(raw) > 15 * 1024 * 1024:
            raise ValueError('Batch请求超过15MB，请减少文字或图片。')
        if not store.claim_batch(record['id']):
            return dict(conversation=record['conversation'], batch=store.get_batch(record['id']))
        file_id = client.upload(raw, 'ai-chat.jsonl', 'batch')
        store.update_batch(record['id'], file_id=file_id, state='uploaded')
        job.check_canceled()
        store.update_batch(record['id'], state='submitting')
        cloud = client.create(file_id, {'endpoint': '/v1/responses', 'ai_chat': record['id']})
        bind_cloud(store, record['id'], cloud)
    except Exception as error:
        current = store.get_batch(record['id'])
        uncertain = current['state'] == 'submitting' and not (isinstance(error, APIError) and error.definite)
        message = redact_sensitive_text(str(error))[:1200]
        store.update_batch(record['id'], state='unknown' if uncertain else 'failed', error=message)
        if not uncertain:
            store.fail_request(record['conversation'], record['prompt_id'], message)
    finally:
        client.close()
    return dict(conversation=record['conversation'], batch=store.get_batch(record['id']))


def bind_cloud(store, identifier, cloud):
    record = store.get_batch(identifier)
    cloud_id = cloud.get('id')
    import re
    if (not isinstance(cloud_id, str) or not re.fullmatch(r'batch_[A-Za-z0-9_-]{1,180}', cloud_id)
            or (cloud.get('metadata') or {}).get('ai_chat') != identifier
            or cloud.get('endpoint') != '/v1/responses'
            or cloud.get('input_file_id') != record['file_id']):
        raise ValueError('Batch身份与原始提交不一致，未接收结果。')
    if cloud.get('status') not in TERMINAL | {'validating', 'in_progress', 'finalizing', 'cancelling'}:
        raise ValueError('Batch返回未知状态，保留原记录以便核对。')
    store.update_batch(identifier, batch_id=cloud_id, state=cloud.get('status') or 'validating', error='')


def recover(client, record):
    after = None
    for _ in range(30):
        params = {'limit': 100}
        if after:
            params['after'] = after
        page = client.json('GET', '/batches', params=params)
        rows = page.get('data') or []
        matches = [row for row in rows if (row.get('metadata') or {}).get('ai_chat') == record['id']]
        if len(matches) > 1:
            raise ValueError('发现重复Batch记录，请在官方后台核对，未再次提交。')
        if matches:
            return matches[0]
        if not page.get('has_more') or not rows:
            return None
        if all(row.get('created_at', time.time()) < record['created'] - 300 for row in rows):
            return None
        after = rows[-1].get('id')
    raise ValueError('尚未查全云端Batch记录，保留待核对状态。')


def collect_result(client, store, record, cloud):
    received = []
    for field in ('output_file_id', 'error_file_id'):
        if cloud.get(field):
            for value in client.lines(cloud[field]):
                received.append(value)
                if len(received) > 1:
                    raise ValueError('Batch返回重复或多余结果，未导入。')
    # One local request per Batch; verify all bindings before writing a reply.
    if len(received) != 1 or received[0].get('custom_id') != record['id']:
        if not received:
            store.fail_request(record['conversation'], record['prompt_id'], 'Batch结束但未返回本条回答：' + str(cloud.get('status')))
            return
        raise ValueError('Batch结果重复、缺失或不属于本条请求，未导入。')
    row = received[0]
    response = row.get('response') or {}
    if row.get('error') or response.get('status_code') != 200:
        message = redact_sensitive_text(str(row.get('error') or response.get('body') or 'Batch请求失败'))[:1200]
        store.fail_request(record['conversation'], record['prompt_id'], message)
        return
    body = response.get('body') or {}
    normalized = normalize_response(body)
    text = normalized['choices'][0]['message']['content']
    if not text.strip():
        raise ValueError('Batch未返回可显示的文字，未自动重新生成。')
    result = dict(text=text, provider='openai', model=normalized.get('model') or record['model'],
        requested_model=record['model'], reported_model=normalized.get('model') or '',
        usage={'input': normalized['usage']['prompt_tokens'], 'output': normalized['usage']['completion_tokens']},
        elapsed=round(time.time() - record['created'], 2),
        warning='Batch优惠请求；具体费用以官方账单为准。' + ('回答达到输出上限，可能未完。' if normalized['choices'][0]['finish_reason'] == 'length' else ''),
        **response_metadata('openai', normalized))
    store.complete_request(record['conversation'], record['prompt_id'], result)


def check_batches(payload, job, *, store, client_factory=None):
    require_online('检查AI聊天Batch')
    client_factory = client_factory or BatchClient
    key = read_provider_key(payload['config'], 'openai')
    client = client_factory(key, cancel=job.check_canceled)
    changed = 0
    try:
        for record in store.batches(payload['conversation']):
            if record['state'] in TERMINAL and record['applied']:
                continue
            if record['key_fingerprint'] != fingerprint(key):
                raise ValueError('请先在连接设置选回提交Batch时使用的OpenAI Key，再检查结果。')
            job.check_canceled()
            if not record['batch_id']:
                if record['state'] not in {'submitting', 'unknown'}:
                    # A crash during file upload cannot have created a Batch.
                    store.fail_request(record['conversation'], record['prompt_id'], 'Batch尚未提交到云端；可手动重试这条消息。')
                    store.update_batch(record['id'], state='failed', applied=1, error='提交过程已中断。')
                    continue
                cloud = recover(client, record)
                if cloud is None:
                    store.update_batch(record['id'], state='unknown', error='尚未查到原Batch，不会自动重新提交。')
                    continue
                bind_cloud(store, record['id'], cloud)
                record = store.get_batch(record['id'])
            cloud = client.retrieve(record['batch_id'])
            bind_cloud(store, record['id'], cloud)
            if payload.get('cancel') and cloud.get('status') not in TERMINAL | {'cancelling'}:
                cloud = client.cancel_batch(record['batch_id'])
                bind_cloud(store, record['id'], cloud)
            if cloud.get('status') in TERMINAL:
                collect_result(client, store, record, cloud)
                store.update_batch(record['id'], applied=1)
                changed += 1
    finally:
        client.close()
    return dict(conversation=payload['conversation'], checked=changed)
