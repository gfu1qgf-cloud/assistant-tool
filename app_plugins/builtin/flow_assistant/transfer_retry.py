"""Durable, bounded transfer recovery. This module never sends run_tasks."""
import time
import re
from urllib.parse import urlsplit

from .policy import block_for
from .store import fingerprint
from .transfer_policy import DELAYS, LABELS, retry_info, retry_limit, ready_bindings
from .timing import value as timing_value, delay
from .media_identity import generated_bindings
from .setup_support import compatible_extension
from .reference_image import freeze_reference


class TransferRecovery:
    def recover_extension_downloads(self, client):
        """An updated extension automatically recovers its old ASB rejection.

        This is not a general reset of exhausted retries or a new generation.
        Keep the one-time marker so reconnecting cannot create an endless loop.
        """
        if not compatible_extension(client.get('extension_version')): return
        for row in self.store.rows():
            info = dict(retry_info(row, 'download'))
            if (row['status'] != 'download_failed' or row['browser'] != client['id']
                    or info.get('asb_recovery') or '下载域名不受信任' not in row['error']): continue
            bound = False
            for binding in generated_bindings(row['remote']):
                if binding['taskId'] != row['remote'].get('upstream_task'): continue
                for media_id in binding.get('readyIds', []):
                    url = urlsplit(binding.get('urls', {}).get(media_id, ''))
                    match = re.fullmatch(r'/asb/([A-Za-z0-9_-]+)(?:=[A-Za-z0-9_,.-]+)?', url.path)
                    if (url.scheme == 'https' and url.hostname == 'flow.google.com' and not url.username
                            and not url.password and url.port in (None, 443) and match and match[1] == media_id):
                        bound = True
            if bound:
                info.update(count=0, next_at=time.time(), stopped=False, active=False, asb_recovery=True,
                            waiting='扩展下载校验已修复，正在自动恢复原视频；不会重新生成')
                self._save_transfer(row, 'download', info)

    def _save_transfer(self, row, stage, info, *, status=None, error=None):
        remote = {**row['remote'], 'transfer_retries': {
            **row['remote'].get('transfer_retries', {}), stage: {**info, 'attempt': row['attempt']}}}
        if remote != row['remote'] or status and status != row['status'] or error is not None and error != row['error']:
            self.store.update(row['id'], status or row['status'], remote=remote,
                              error=row['error'] if error is None else error)
            return True
        return False

    def _transfer_pending(self, row, stage):
        action = 'upload_images' if stage == 'upload' else 'retry_download'
        return any(request['job'] == row['id'] and request['action'] == action and
                   request['attempt'] == row['attempt'] and request['client'] == row['browser']
                   for request in self.pending.values())

    def transfer_failed(self, row, stage, error, *, permanent=False, stop_code=''):
        if row['status'] in {'completed', 'needs_review', 'cancelled', 'archiving', 'archive_failed', 'download_parked'}:
            return False
        action = 'upload_images' if stage == 'upload' else 'retry_download'
        for key, request in list(self.pending.items()):
            if request['job'] == row['id'] and request['attempt'] == row['attempt'] and request['action'] == action:
                self.pending.pop(key)
        info = dict(retry_info(row, stage)); count = info.get('count', 0)
        limit = retry_limit(self.preferences, stage)
        stopped = permanent or info.get('stopped', False) or count >= limit
        # Duplicate snapshots/error notifications must not postpone a scheduled retry.
        next_at = 0 if stopped else info.get('next_at') or time.time() + delay(self.preferences, stage + '_retry_delays', count)
        info.update(count=count, stopped=stopped, active=False, next_at=next_at,
                    last_error=str(error)[:1000], waiting='', stop_code=stop_code)
        status = ('failed' if stopped else 'uploading') if stage == 'upload' else 'download_failed'
        return self._save_transfer(row, stage, info, status=status, error=str(error)[:1000])

    def _wait_transfer(self, row, stage, reason):
        info = dict(retry_info(row, stage)); info.update(active=False, waiting=reason,
            waiting_since=info.get('waiting_since') or time.time(),
            next_at=info.get('next_at') or time.time() + timing_value(self.preferences, 'transfer_wait_seconds'))
        return self._save_transfer(row, stage, info)

    def _transfer_client(self, row, stage, *, fresh=False):
        client = next((client for client in self.clients.values() if client.get('authorized') and
                       client.get('id') == row['browser']), None)
        if not client:
            self._wait_transfer(row, stage, '等待浏览器重连；提交前超时将自动重新调度，也可完全重置' if stage == 'upload' else
                '等待原浏览器重连，连接后自动继续')
            return None
        if stage == 'upload':
            reason = block_for(client, self.preferences) or client.get('blocked')
            if (reason or client.get('page_ready') is False or client.get('batch_running') or
                    client.get('reset_wait_until', 0) > time.time() or
                    (client.get('connection_recovery') or {}).get('state') == 'checking'):
                self._wait_transfer(row, stage, reason or '等待原 Flow 项目页面恢复／空闲；提交前超时将重新调度')
                return None
            if row['remote'].get('account') and client.get('account') and row['remote']['account'] != client['account']:
                self.transfer_failed(row, stage, '原浏览器登录账号已变化，已停止自动上传；请确认账号。', permanent=True)
                return None
            if fresh and (not client.get('account') or client.get('credits') is None):
                self._wait_transfer(row, stage, '等待本次连接首次读取原账号与积分，完成后自动继续')
                return None
        return client

    def start_upload(self, row, *, retry=False):
        if self._transfer_pending(row, 'upload'): return False
        client = self._transfer_client(row, 'upload', fresh=retry)
        if not client: return False
        cost = row['remote'].get('cost')
        if retry and cost is not None and (client.get('credits') is None or client['credits'] < cost):
            self.store.update(row['id'], 'queued', attempt='', browser='', remote={},
                              error='原账号积分不足，等待可承担本条费用的备用账号；未提交生成。')
            return True
        try: unchanged = fingerprint(row['payload']['image']) == row['payload']['image_identity']
        except OSError: unchanged = False
        if not unchanged:
            return self.transfer_failed(row, 'upload', '参考图片已变化或丢失，已停止自动上传，请重新选择图片。', permanent=True)
        try:
            reference = freeze_reference(row, self.downloads_root)
        except (OSError, ValueError) as error:
            return self.transfer_failed(row, 'upload', str(error), permanent=True)
        self.store.update(row['id'], row['status'], remote={**row['remote'], 'upload_reference': reference}, error=row['error'])
        row = self.store.get(row['id'])
        info = dict(retry_info(row, 'upload'))
        if row['remote'].get('connection_wait_at'):
            self.store.update(row['id'], row['status'], remote={key: value for key, value in row['remote'].items()
                if key != 'connection_wait_at'}, error=row['error'])
            row = self.store.get(row['id'])
        info.update(count=info.get('count', 0) + int(retry), active=True, stopped=False,
                    next_at=0, waiting='', waiting_since=0, last_error='')
        self._save_transfer(row, 'upload', info, status='uploading', error='')
        self.request(client, 'upload_images', {'paths': [reference['path']], 'upload_id': row['attempt'],
            'reference': reference, 'expected_account': client.get('account'),
            'recover_reference': bool(retry),
            'reference_reset': row['payload'].get('reference_reset', '')}, row['id'])
        return True

    def retry_download(self, row, *, automatic=False):
        if self._transfer_pending(row, 'download') or row['id'] in self.archiving: return False
        if row['status'] not in {'download_failed', 'uncertain', 'download_parked', 'downloading'}: return False
        if not automatic:
            self._save_transfer(row, 'download', {'count': 0, 'next_at': time.time(), 'stopped': False, 'active': False},
                                status='download_failed', error='')
            row = self.store.get(row['id'])
        client = self._transfer_client(row, 'download')
        if not client: return False
        info = dict(retry_info(row, 'download'))
        bindings = ready_bindings(row['remote'])
        verified = [b for b in generated_bindings(row['remote']) if b['taskId'] == row['remote'].get('upstream_task')]
        if bindings and not any(b in verified for b in bindings):
            return self.transfer_failed(row, 'download', '旧媒体绑定没有本次生成响应的归属证据，已停止自动下载；不会重新生成或猜测页面视频。', permanent=True)
        bindings = [b for b in bindings if b in verified]
        if not bindings or not row['remote'].get('upstream_task'):
            # No media-ID guessing or regenerated tasks: first ask the original
            # page for its saved snapshot. These probes are bounded too.
            if time.time() >= info.get('probe_at', 0) + timing_value(self.preferences, 'download_probe_seconds'):
                limit = timing_value(self.preferences, 'download_probe_attempts')
                if info.get('probes', 0) >= limit:
                    return self.transfer_failed(row, 'download', '原视频结果核对已达上限；保留原任务，收到准确结果后自动恢复下载。',
                        permanent=True, stop_code='binding_probe_limit')
                if any(request['action'] == 'snapshot' and request['client'] == client['id'] for request in self.pending.values()):
                    return False
                self.request(client, 'snapshot')
                info.update(probe_at=time.time(), probes=info.get('probes', 0) + 1,
                            probe_limit=limit, waiting=('已确认本次视频编号，等待原视频就绪地址' if any(b['mediaIds'] for b in verified)
                                else '尚未取得本次视频编号，正在核对原提交回执'))
                return self._save_transfer(row, 'download', info)
            return False
        params = {'batch_id': row['attempt'], 'restore': {
            'bindings': bindings, 'task_id': row['remote']['upstream_task'], 'download_path': self.download_name(row)}}
        # Always prefer durable bindings, including the current batch. The page
        # may have lost its in-memory tasks even though generation succeeded.
        info.update(count=info.get('count', 0) + 1, next_at=0, active=True, stopped=False,
                    waiting='', last_error='', sent_at=time.time())
        self._save_transfer(row, 'download', info, status='downloading', error='')
        self.request(client, 'retry_download', params, row['id'])
        return True

    def resume_download_binding(self, row):
        """Late, verified results can resume a probe-only stop, never a retry limit."""
        info = dict(retry_info(row, 'download'))
        probe_stop = info.get('stop_code') == 'binding_probe_limit' or (
            info.get('last_error') == '未找到原媒体绑定，已停止自动探测；请核对原项目。')
        if (row['status'] != 'download_failed' or not info.get('stopped') or not probe_stop or
                info.get('count', 0) >= retry_limit(self.preferences, 'download')):
            return False
        verified = generated_bindings(row['remote'])
        if not any(b in verified and b['taskId'] == row['remote'].get('upstream_task') for b in ready_bindings(row['remote'])):
            return False
        info.update(stopped=False, active=False, next_at=time.time(), stop_code='', waiting='已收到原视频准确结果，自动恢复下载')
        return self._save_transfer(row, 'download', info, error='')

    def watch_transfer_retries(self):
        changed = False
        for row in self.store.rows():
            if row['status'] == 'download_failed': stage = 'download'
            elif row['status'] == 'uploading' and retry_info(row, 'upload').get('next_at'): stage = 'upload'
            else: continue
            info = retry_info(row, stage)
            if not info:
                changed |= self.transfer_failed(row, stage, row['error'] or '等待恢复原视频下载')
                continue
            if info.get('stopped') or self._transfer_pending(row, stage): continue
            if stage == 'upload' and (self.safety_paused or self.paused and row['id'] not in self.released):
                changed |= self._wait_transfer(row, stage, '队列已暂停；继续后自动重试上传')
                continue
            if info.get('next_at', 0) > time.time(): continue
            if info.get('count', 0) >= retry_limit(self.preferences, stage):
                changed |= self.transfer_failed(row, stage, info.get('last_error') or '自动重试次数已用尽')
                continue
            changed |= self.start_upload(row, retry=True) if stage == 'upload' else self.retry_download(row, automatic=True)
        return changed
