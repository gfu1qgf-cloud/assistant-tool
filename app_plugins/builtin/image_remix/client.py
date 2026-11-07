"""Small fixed-origin Batch client. Never retries a billable POST."""
import json
import time
import requests

from model.OfflineMode import require_online
from model.SensitiveData import redact_sensitive_text
from .prompts import safe_id


class APIError(RuntimeError):
    def __init__(self, message, *, definite=False):
        super().__init__(message)
        self.definite = definite


class BatchClient:
    def __init__(self, key, cancel=lambda: None, session=None):
        self.cancel = cancel
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + key})

    def close(self):
        self.session.close()

    def request(self, method, path, **kwargs):
        require_online('GPT图片批处理')
        if not path.startswith('/') or '://' in path or '..' in path:
            raise ValueError('拒绝非官方API路径。')
        self.cancel()
        try:
            response = self.session.request(method, 'https://api.openai.com/v1' + path,
                                            timeout=(10, 60), allow_redirects=False, **kwargs)
        except requests.RequestException as error:
            raise APIError('网络响应未确认；已保留原批次，不自动重新生成。' + type(error).__name__) from error
        if not 200 <= response.status_code < 300:
            try:
                body = response.json()
                reason = body.get('error', {}).get('message', '请求失败')
            except (ValueError, AttributeError):
                reason = '请求失败，请核对网络、账户权限及额度。'
            message = f'OpenAI HTTP {response.status_code}：' + redact_sensitive_text(str(reason))[:600]
            status = response.status_code
            response.close()
            # A 5xx / proxy timeout cannot establish that a POST was rejected.
            raise APIError(message, definite=400 <= status < 500 and status not in (408, 409))
        return response

    def json(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as error:
            raise APIError('官方接口返回无法解析的数据；不自动重提。') from error
        finally:
            response.close()

    def upload(self, raw, filename, purpose):
        value = self.json('POST', '/files', data={'purpose': purpose,
            'expires_after[anchor]': 'created_at', 'expires_after[seconds]': '604800'},
            files={'file': (filename, raw, 'application/octet-stream')})
        return safe_id(value.get('id'), 'file-')

    def create(self, file_id, metadata):
        return self.json('POST', '/batches', json={'input_file_id': safe_id(file_id, 'file-'),
            'endpoint': metadata['endpoint'], 'completion_window': '24h',
            'metadata': {k: v for k, v in metadata.items() if k != 'endpoint'}})

    def retrieve(self, identifier):
        return self.json('GET', '/batches/' + safe_id(identifier, 'batch_'))

    def cancel_batch(self, identifier):
        return self.json('POST', '/batches/' + safe_id(identifier, 'batch_') + '/cancel')

    def recover(self, job_id, token, created):
        after = None
        for _ in range(30):
            self.cancel()
            params = {'limit': 100}
            if after:
                params['after'] = after
            page = self.json('GET', '/batches', params=params)
            rows = page.get('data', [])
            matches = [row for row in rows if (row.get('metadata') or {}).get('remix_job') == job_id
                       and (row.get('metadata') or {}).get('attempt') == token]
            if len(matches) > 1:
                raise ValueError('发现重复云端批次，请在OpenAI控制台核对；不会再提交。')
            if matches:
                return matches[0]
            if not page.get('has_more') or not rows:
                return None
            if all(row.get('created_at', time.time()) < created - 300 for row in rows):
                return None
            after = safe_id(rows[-1].get('id'), 'batch_')
        raise ValueError('云端批次过多，尚未查全；保留待核对状态，不重新生成。')

    def lines(self, file_id):
        response = self.request('GET', '/files/' + safe_id(file_id, 'file-') + '/content', stream=True)
        pending = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=65536):
                self.cancel()
                pending.extend(chunk)
                while True:
                    boundary = pending.find(b'\n')
                    if boundary < 0:
                        break
                    line = bytes(pending[:boundary]); del pending[:boundary + 1]
                    if line.strip():
                        yield json.loads(line)
                if len(pending) > 64 * 1024 * 1024:
                    raise ValueError('单张图片结果超过64MB，已停止接收；原批次仍保留。')
            if pending.strip():
                yield json.loads(pending)
        finally:
            response.close()
