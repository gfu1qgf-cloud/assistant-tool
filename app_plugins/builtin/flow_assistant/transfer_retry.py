"""Durable, bounded transfer recovery. This module never sends run_tasks."""
import time
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .policy import block_for
from .store import fingerprint
from .transfer_policy import DELAYS, LABELS, retry_info, retry_limit, ready_bindings
from .timing import value as timing_value, delay
from .media_identity import generated_bindings
from .setup_support import compatible_extension
from .reference_image import freeze_reference


class TransferRecovery:
    def upload_cycle(self,row,client):
        """One clock/revision per browser, project and image, across retries."""
        from .setup_support import project_identity
        sha=row['payload']['image_identity']['sha256']
        scope=[client.get('profile') or client['id'],client.get('account'),
            project_identity(client.get('saved_project')),sha]
        base=self.store.reference_revision(sha)
        cycles=getattr(self,'upload_cycles',None)
        if cycles is None:self.upload_cycles=cycles={}
        key=tuple(scope);cycle=cycles.get(key)
        saved=row['remote'].get('upload_cycle',{})
        saved_scope=tuple(tuple(v) if isinstance(v,list) else v for v in saved.get('scope',[]))
        if saved_scope==key and saved.get('base')==base and (not cycle or saved.get('at',0)>cycle.get('at',0)):
            cycle=dict(saved)
        if not cycle or cycle.get('base')!=base:
            phase=row['remote'].get('upload_phase',{})
            cycle={'scope':scope,'base':base,'revision':base,'at':phase.get('at') or time.time(),'confirmed':False}
        cycles[key]=cycle
        return cycle

    def redrag_due_upload(self,row):
        """After three minutes without image proof, use the existing reset API."""
        if (row['status'] not in {'uploading','failed'} or not row['attempt'] or
                not row['remote'].get('upload_reference') or retry_info(row,'upload').get('permanent') or
                self.safety_paused or self.paused and row['id'] not in self.released):return False
        from .store import submission_risk
        if submission_risk(row,self.store.attempt_charged(row['attempt'])):return False
        client=next((c for c in self.clients.values() if c.get('authorized') and c.get('id')==row['browser']),None)
        if not client:return False
        if block_for(client,self.preferences) or client.get('blocked'):return False
        cycle=self.upload_cycle(row,client);now=time.time()
        if not row['remote'].get('upload_cycle'):
            self.store.update(row['id'],row['status'],remote={**row['remote'],'upload_cycle':dict(cycle)},error=row['error'])
            return True
        if cycle.get('confirmed') or now-cycle['at']<180:return False
        from .image_upload_limit import remember_failure,abandon,image_key,LIMIT
        if remember_failure(self,row,expired=True)>=LIMIT:return abandon(self,image_key(row))
        if row['status']=='failed':
            from .credit_packs import active_count
            from .browser_slots import limit
            if active_count(self.store.rows())>=limit(self):return False
        requests=[(k,p) for k,p in self.pending.items() if p.get('job')==row['id'] and
            p.get('attempt')==row['attempt'] and p.get('action')=='upload_images']
        live=[(k,p) for k,p in requests if not p.get('retired')]
        if live:
            cycle['cancel_until']=now+5
            for key,request in live:
                request.update(retired=True,cancel_deadline=now+5)
                self.send(client,{'type':'cancel_operation','action':'cancel_operation','target_request_id':key})
            self.store.update(row['id'],row['status'],remote={**row['remote'],'upload_cycle':dict(cycle)},
                error='原图超过3分钟未确认，正在退出旧核对，随后重新拖拽上传。')
            return True
        if requests or now<cycle.get('cancel_until',0):return True
        return self.start_upload(row,retry=False)

    def _release_skipped_wait(self, row):
        """Retire only this attempt's local wait, never cancel cloud work."""
        client = next((c for c in self.clients.values() if c.get('authorized') and c['id'] == row['browser']), None)
        if (not client or 'skip_download_v1' not in client.get('capabilities', []) or
                not client.get('batch_running') or client.get('native_busy') or
                client.get('active_batch') != row['attempt'] and not (row['remote'].get('credit_pack') and
                    'credit_pack_submit_v1' in client.get('capabilities',[]))):
            return False
        previous = client.get('skip_download_waits',{}).get(row['attempt']) or client.get('skip_download_wait') or {}
        if not isinstance(previous, dict) or previous.get('attempt') != row['attempt']: previous = {}
        if (previous.get('count', 0) >= timing_value(self.preferences, 'connection_repair_attempts') or
                previous.get('next_at', 0) > time.time() or
                any(p['client'] == client['id'] and p['action'] == 'skip_download' for p in self.pending.values())):
            return False
        if self.request(client, 'skip_download', {'batch_id': row['attempt']}):
            client['skip_download_wait'] = {'attempt':row['attempt'], 'count':previous.get('count',0)+1,
                'next_at':time.time()+timing_value(self.preferences,'transfer_wait_seconds')}
            client.setdefault('skip_download_waits',{})[row['attempt']]=client['skip_download_wait']
            return True
        return False

    def park_failed_download(self, row):
        """An exhausted download is retained for manual recovery, not dispatch."""
        if row['status'] != 'download_failed' or not retry_info(row, 'download').get('stopped'):
            return False
        remote = {**row['remote'], 'download_skipped': True, 'released_at': time.time()}
        self.store.update(row['id'], 'download_parked', remote=remote,
            error='下载已跳过，不占用后续调度；保留原任务，可手动下载或仅重试下载。原因：' +
                  str(retry_info(row, 'download').get('last_error') or row['error'])[:700])
        for key, request in list(self.pending.items()):
            if request['job'] == row['id'] and request['attempt'] == row['attempt']:
                self.pending.pop(key)
        self._release_skipped_wait(self.store.get(row['id']))
        return True

    def recover_extension_downloads(self, client):
        """An updated extension automatically recovers its old ASB rejection.

        This is not a general reset of exhausted retries or a new generation.
        Keep the one-time marker so reconnecting cannot create an endless loop.
        """
        if not compatible_extension(client.get('extension_version')): return
        for row in self.store.rows():
            info = dict(retry_info(row, 'download'))
            if (row['status']=='download_parked' and row['browser']==client['id'] and
                    info.get('stop_code')=='video_page_bridge_update' and row['remote'].get('download_refresh',{}).get('video_page') and
                    compatible_extension(client.get('extension_version'),minimum_patch=49)):
                self.retry_download(row)
                continue
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
        from .download_backlog import detached
        independent=stage=='download' and detached(row)
        if row['status'] in {'completed', 'needs_review', 'cancelled', 'archiving', 'archive_failed'} or row['status']=='download_parked' and not independent:
            return False
        if stage=='upload':
            from .image_upload_limit import remember_failure,abandon,image_key,LIMIT
            if remember_failure(self,row,error)>=LIMIT:return abandon(self,image_key(row))
        action = 'upload_images' if stage == 'upload' else 'retry_download'
        for key, request in list(self.pending.items()):
            if request['job'] == row['id'] and request['attempt'] == row['attempt'] and request['action'] == action:
                self.pending.pop(key)
        info = dict(retry_info(row, stage)); count = info.get('count', 0)
        limit = retry_limit(self.preferences, stage)
        stopped = permanent or info.get('stopped', False) or count >= limit
        # Duplicate snapshots/error notifications must not postpone a scheduled retry.
        next_at = 0 if stopped else info.get('next_at') or time.time() + delay(self.preferences, stage + '_retry_delays', count)
        cycle=row['remote'].get('upload_cycle',{})
        if (stage=='upload' and cycle and not cycle.get('confirmed') and not permanent and
                any(text in str(error) for text in ('未重复拖放','核对原素材','素材编号'))):
            # Exhausting name checks must not lose the three-minute deadline.
            stopped=False
            next_at=min(next_at or cycle['at']+180,cycle['at']+180)
            if count>=limit:next_at=max(time.time()+1,cycle['at']+180)
        info.update(count=count, stopped=stopped, active=False, next_at=next_at,
                    last_error=str(error)[:1000], waiting='', stop_code=stop_code, permanent=permanent)
        status = ('failed' if stopped else 'uploading') if stage == 'upload' else 'download_failed'
        changed = self._save_transfer(row, stage, info, status=status, error=str(error)[:1000])
        if stage == 'download' and stopped:
            changed |= self.park_failed_download(self.store.get(row['id']))
        if independent:
            from .download_backlog import detach
            changed |= detach(self,self.store.get(row['id']))
        return changed

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
        from .image_upload_limit import blocked,abandon,image_key
        if blocked(self,row):return abandon(self,image_key(row))
        if self._transfer_pending(row, 'upload'): return False
        client = self._transfer_client(row, 'upload', fresh=retry)
        if not client: return False
        if retry:
            from .runtime_controls import recover_upload
            if recover_upload(self, client, row): return False
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
            from .reference_image import supports_image_id, supports_stable_name, valid_filename
            include_image_id=supports_image_id(client)
            stable_image_name=supports_stable_name(client)
            previous=row['remote'].get('upload_reference') or {}
            if (previous.get('upload_id')==row['attempt'] and
                    previous.get('sha256')==row['payload']['image_identity']['sha256'] and valid_filename(previous)):
                # A newly loaded script must not rename an already successful
                # upload midway through the same attempt's mounting retries.
                include_image_id=include_image_id and previous['filename'].startswith('IMG_')
                stable_image_name=stable_image_name and previous['filename']==('IMG_'+previous['sha256']+Path(previous['filename']).suffix)
            reference = freeze_reference(row, self.downloads_root, include_image_id=include_image_id, stable_image_name=stable_image_name)
        except (OSError, ValueError) as error:
            return self.transfer_failed(row, 'upload', str(error), permanent=True)
        cycle=self.upload_cycle(row,client);forced=not cycle.get('confirmed') and time.time()-cycle['at']>=180
        if forced:
            cycle.update(at=time.time(),revision=uuid.uuid4().hex,confirmed=False)
            cycle.pop('cancel_until',None)
            cycle.pop('upload_attempt',None)
        self.store.update(row['id'], row['status'], remote={**row['remote'], 'upload_reference': reference,
            'upload_cycle':dict(cycle)}, error=row['error'])
        row = self.store.get(row['id'])
        info = dict(retry_info(row, 'upload'))
        if forced:info.update(count=0,stopped=False,permanent=False)
        if row['remote'].get('connection_wait_at'):
            self.store.update(row['id'], row['status'], remote={key: value for key, value in row['remote'].items()
                if key != 'connection_wait_at'}, error=row['error'])
            row = self.store.get(row['id'])
        info.update(count=info.get('count', 0) + int(retry), active=True, stopped=False, permanent=False,
                    next_at=0, waiting='原图3分钟未确认，正在重新拖拽上传' if forced else '', waiting_since=0, last_error='')
        self._save_transfer(row, 'upload', info, status='uploading', error='')
        current=self.store.get(row['id'])
        self.store.update(row['id'],current['status'],remote={**current['remote'],
            'upload_phase':{'phase':'requested','at':time.time()}},error=current['error'])
        visual = None
        from .browser_options import snapshot as browser_options
        if browser_options(self.preferences)['visual_upload_fallback']:
            try:
                from .visual_reference import template
                visual = template(reference)
            except Exception:
                pass  # Ordinary exact-name/ID validation stays available.
        self.request(client, 'upload_images', {'paths': [reference['path']], 'upload_id': row['attempt'],
            'reference': reference, 'expected_account': client.get('account'),
            'visual_reference': visual,
            'recover_reference': bool(retry),
            'reference_reset': cycle['revision']}, row['id'])
        return True

    def retry_download(self, row, *, automatic=False):
        if self._transfer_pending(row, 'download') or row['id'] in self.archiving: return False
        if row['status'] not in {'download_failed', 'uncertain', 'download_parked', 'downloading'}: return False
        if not automatic:
            if row['remote'].get('download_backlog'):
                self.store.update(row['id'],row['status'],remote={**row['remote'],'download_backlog':{
                    **row['remote']['download_backlog'],'auto':True,'cancel_sent':False,'since':time.time()}})
                row=self.store.get(row['id'])
            self._save_transfer(row, 'download', {'count': 0, 'next_at': time.time(), 'stopped': False, 'active': False},
                                status='download_failed', error='')
            row = self.store.get(row['id'])
        client = self._transfer_client(row, 'download')
        if not client: return False
        info = dict(retry_info(row, 'download'))
        from .video_catalog import manual_binding
        manual = manual_binding(row)
        bindings = ready_bindings(row['remote'])
        verified = [b for b in generated_bindings(row['remote']) if b['taskId'] == row['remote'].get('upstream_task')]
        if not manual and bindings and not any(b in verified for b in bindings):
            return self.transfer_failed(row, 'download', '旧媒体绑定没有本次生成响应的归属证据，已停止自动下载；不会重新生成或猜测页面视频。', permanent=True)
        bindings = [b for b in bindings if b in verified]
        refresh = row['remote'].get('download_refresh') or {}
        if refresh.get('media_id'):
            bindings = [b for b in verified if refresh['media_id'] in b['mediaIds']]
        if manual:
            selected = row['remote']['manual_download_selection']
            if selected.get('account') and selected['account'] != client.get('account'):
                return self.transfer_failed(row, 'download', '手动选择时的原账号尚未确认或已变化，请切回原账号后重试下载。', permanent=True)
            if not compatible_extension(client.get('extension_version'), minimum_patch=53):
                return self.transfer_failed(row, 'download', '手动视频绑定需要桥接扩展1.1.53；更新后可重试下载。', permanent=True)
            bindings = [manual]
            verified = [manual]
            refresh = {**refresh, 'media_id': manual['mediaIds'][0]}
        if not bindings and compatible_extension(client.get('extension_version'), minimum_patch=40):
            from .result_download import unique_result
            unique = unique_result(row['remote'])
            if unique:
                binding, media = unique
                bindings = [binding]
                refresh = {**refresh, 'media_id':media}
        if bindings and refresh.get('media_id'):
            binding = next((b for b in bindings if refresh['media_id'] in b['mediaIds'] and
                            refresh['media_id'] not in b.get('failedIds', [])), None)
            if not binding:return False
            target = {'attempt':row['attempt'], 'media_id':refresh['media_id'],
                      'task_id':binding['taskId'], 'submission_id':binding['submissionId']}
            self.store.update(row['id'],row['status'],remote={**row['remote'],'download_recovery_target':target},error=row['error'])
            row = self.store.get(row['id'])
        if not bindings or not (manual or row['remote'].get('upstream_task')):
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
            'bindings': bindings, 'task_id': manual['taskId'] if manual else row['remote']['upstream_task'], 'download_path': self.download_name(row),
            'refresh_url': True, 'media_id': refresh.get('media_id'),
            'project': refresh.get('project') or row['remote'].get('page') or
                self.preferences.get('profile_projects', {}).get(row['remote'].get('profile'))}}
        if manual:
            params['restore']['manual_selection'] = row['remote']['manual_download_selection']
        # An automatic continuation after Chrome reopened uses the retained
        # exact media ID in the original project. It must not require an old
        # /edit/ tab to remain open. Explicit pasted-page recovery stays scoped.
        if refresh.get('video_page') and not (automatic and row['remote'].get('download_backlog')):
            if not compatible_extension(client.get('extension_version'),minimum_patch=49):
                return self.transfer_failed(row,'download',
                    '已保存视频页面链接；等待原浏览器空闲更新到桥接扩展1.1.49后自动恢复下载，未重新生成。',
                    permanent=True,stop_code='video_page_bridge_update')
            params['restore']['video_page']=refresh['video_page']
        # Always prefer durable bindings, including the current batch. The page
        # may have lost its in-memory tasks even though generation succeeded.
        info.update(count=info.get('count', 0) + 1, next_at=0, active=True, stopped=False,
                    waiting='', last_error='', sent_at=time.time())
        from .download_backlog import detached
        self._save_transfer(row, 'download', info, status='download_parked' if detached(row) else 'downloading', error='')
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
        from .download_backlog import watch
        changed = watch(self)
        from .image_upload_limit import watch as watch_image_limits
        changed=watch_image_limits(self) or changed
        for row in self.store.rows():
            if self.redrag_due_upload(row):changed=True;continue
            if row['status'] == 'download_parked' and row['remote'].get('download_skipped'):
                changed |= self._release_skipped_wait(row)
                continue
            if row['status'] == 'download_failed': stage = 'download'
            elif row['status'] == 'uploading' and retry_info(row, 'upload').get('next_at'): stage = 'upload'
            else: continue
            info = retry_info(row, stage)
            if not info:
                changed |= self.transfer_failed(row, stage, row['error'] or '等待恢复原视频下载')
                continue
            if info.get('stopped'):
                if stage == 'download': changed |= self.park_failed_download(row)
                continue
            if self._transfer_pending(row, stage): continue
            if stage == 'upload' and (self.safety_paused or self.paused and row['id'] not in self.released):
                changed |= self._wait_transfer(row, stage, '队列已暂停；继续后自动重试上传')
                continue
            if info.get('next_at', 0) > time.time(): continue
            if info.get('count', 0) >= retry_limit(self.preferences, stage):
                changed |= self.transfer_failed(row, stage, info.get('last_error') or '自动重试次数已用尽')
                continue
            changed |= self.start_upload(row, retry=True) if stage == 'upload' else self.retry_download(row, automatic=True)
        return changed
