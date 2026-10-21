"""Recoverable queue operations and watchdogs; never resubmit paid generation."""
import copy
from pathlib import Path
import shutil
import time
import re
from urllib.parse import urlparse, parse_qs

from .policy import block_for, estimated_cost
from .routing import cost_key, lane_key
from .store import make_jobs
from .connection_status import browser_keys
from .pricing import fixed_cost
from .transfer_retry import TransferRecovery
from .task_reset import TaskReset
from .timing import value as timing_value


class FlowOperations(TransferRecovery, TaskReset):
    def commit_preferences(self, preferences):
        # Publish only after SQLite commits; callers keep the previous snapshot
        # when the encrypted volume becomes unavailable or a write fails.
        value = copy.deepcopy(preferences)
        self.store.save_settings(value)
        self.preferences = value

    def recover_fixed_price_blocks(self):
        """Retire only old unreadable-price blocks now covered by user's tariff.

        Startup remains paused. Never clear login, model, duration or risk
        blocks, and never infer a price for an unspecified duration.
        """
        rows = [row for row in self.store.rows() if row['status'] == 'queued' and
                row['remote'].get('environment_error') and fixed_cost(row['payload']) is not None]
        keys = set().union(*(browser_keys({'profile': row['remote'].get('profile'),
                    'id': row['remote'].get('environment_browser')}) for row in rows)) if rows else set()
        blocks = self.preferences.get('browser_blocks', {})
        resolved = {key for key in keys if str(blocks.get(key, '')).startswith((
            '页面费用无法读取，', '已设置整批积分上限，但页面费用未知；'))}
        if not resolved: return
        preferences = {**self.preferences, 'browser_blocks': {key: value for key, value in blocks.items() if key not in resolved}}
        self.store.save_settings(preferences)
        self.preferences = preferences
        for row in rows:
            if browser_keys({'profile': row['remote'].get('profile'), 'id': row['remote'].get('environment_browser')}) & resolved:
                self.store.update(row['id'], 'queued', remote={**row['remote'], 'environment_error': False},
                    error='已启用固定时长积分规则，等待用户开始／继续队列。')

    def media_disks_ready(self, row):
        from model.MediaStorage import require_media_output
        try:
            require_media_output(row['payload']['output'])
            require_media_output(self.downloads_root)
            return True
        except (OSError, ValueError) as error:
            self.paused = True; self.released.clear()
            self.store.update(row['id'], 'queued', attempt='', browser='', remote={}, error=str(error))
            self.failed.emit('Flow 停止新增生成：' + str(error)); self.publish()
            return False

    def quarantine(self, client, row, message, *, status='queued'):
        scope, key = lane_key(client)
        blocks = dict(self.preferences.get('browser_blocks', {}))
        blocks[scope + ':' + key] = str(message)[:1000]
        try:
            self.commit_preferences({**self.preferences, 'browser_blocks': blocks})
        except Exception:
            self.paused = True
            self.released.clear()
            client['blocked'] = '无法保存浏览器暂停状态，队列已停止新增提交'
            raise
        remote = {**row['remote'], 'environment_error': True, 'profile': client.get('profile', ''), 'environment_browser': client['id']}
        self.store.update(row['id'], status, attempt='', browser='', remote=remote,
                          error='浏览器环境已暂停，修正后在浏览器页恢复：' + str(message)[:700])

    def remember_cost(self, job, cost):
        if self.observed_costs.get(cost_key(job)) == cost:
            return
        costs = {**self.observed_costs, cost_key(job): cost}
        self.commit_preferences({**self.preferences, 'observed_costs': [
            {'key': list(key), 'cost': value} for key, value in costs.items()]})
        self.observed_costs = costs

    def resume_browser(self, command):
        keys = set(command.get('keys', ()))
        for client in self.clients.values():
            if browser_keys(client) & keys:
                keys.update(browser_keys(client))
        blocks = dict(self.preferences.get('browser_blocks', {}))
        for key in keys:
            blocks.pop(key, None)
        limits = {key: value for key, value in self.preferences.get('parameter_blocks', {}).items() if key not in keys}
        launch_failures = {key: value for key, value in self.preferences.get('browser_launch_failures', {}).items()
                           if 'profile:' + key not in keys}
        slots = {profile: {key:value for key,value in entry.items() if key != 'project_startup'}
                 if 'profile:' + profile in keys else entry
                 for profile,entry in self.preferences.get('browser_slots', {}).items()}
        self.commit_preferences({**self.preferences, 'browser_blocks': blocks, 'parameter_blocks': limits,
                                  'browser_launch_failures': launch_failures, 'browser_slots':slots,
                                  'browser_probe_retries': {profile:value for profile,value in self.preferences.get('browser_probe_retries',{}).items()
                                      if 'profile:' + profile not in keys}})
        for identity in keys:
            if identity.startswith('profile:'):
                getattr(self, 'launch_attempts', set()).discard(identity.split(':', 1)[1])
                getattr(self, 'small_balance_probes', {}).pop(identity.split(':', 1)[1], None)
        for client in self.clients.values():
            if browser_keys(client) & keys:
                client.pop('prepare_wait', None)
                client.pop('reuse_recovery',None)
                self.connection_recoveries.pop(client.get('profile'),None)
                client.pop('connection_recovery',None)
                client['blocked'] = ''
                from .setup_support import project_block
                client['setup_blocked'] = project_block(client, self.preferences)
                self.request(client, 'get_points')
                self.request(client, 'snapshot')
        for row in self.store.rows():
            remote = row['remote']
            identities = browser_keys({'profile': remote.get('profile'), 'id': remote.get('environment_browser')})
            if remote.get('environment_error') and identities & keys and row['status'] in {'failed', 'queued'}:
                self.store.update(row['id'], 'queued', remote={}, error='环境已恢复，等待重新核对参数')
        self.launched.clear()

    def release_download(self, command):
        selected = [row for row in self.store.rows() if row['id'] in command['ids']]
        for row in selected:
            if row['status'] not in {'download_failed', 'uncertain'}:
                raise ValueError('只能暂存下载失败／等待核对的任务；正在生成或下载的任务不能直接释放。')
            bindings = row['remote'].get('bindings', [])
            if not any(binding and binding.get('mediaIds') for binding in bindings):
                raise ValueError('尚无可恢复的媒体绑定，不能释放；请先核对原任务。')
            if not row['remote'].get('cloud_finished') and not command.get('confirmed_finished'):
                raise ValueError('尚未确认云端生成结束，请先在原浏览器核对，再明确确认。')
        for row in selected:
            remote = {**row['remote'], 'released_at': time.time(), 'cloud_finished': True}
            self.store.update(row['id'], 'download_parked', remote=remote,
                              error='已暂存下载并释放账号；保留媒体绑定，可仅重试下载，不重新生成。')
        for key, request in list(self.pending.items()):
            if request['job'] in command['ids']:
                self.pending.pop(key)

    def watch_stalled(self):
        now = time.time()
        changed = False
        clients = {client.get('id'): client for client in self.clients.values() if client.get('authorized')}
        timeout = timing_value(self.preferences, 'stall_seconds')
        for row in self.store.rows():
            if row['status'] not in {'generating', 'downloading'}:
                continue
            remote = row['remote']
            if row['status'] == 'downloading':
                # Download recovery has its own bounded counter; the generation
                # watchdog must not turn its second retry into "uncertain".
                if now - remote.get('activity_at', row['updated']) >= timing_value(self.preferences, 'download_stall_seconds'):
                    changed |= self.transfer_failed(row, 'download', '下载长时间没有进展，将自动核对并恢复原下载')
                continue
            if now - remote.get('activity_at', row['updated']) < timeout:
                continue
            if now - remote.get('watch_at', 0) < timing_value(self.preferences, 'generation_probe_seconds'):
                continue
            count = int(remote.get('watch_count', 0))
            changed = True
            client = clients.get(row['browser'])
            remote = {**remote, 'watch_at': now, 'watch_count': count + 1}
            if count >= timing_value(self.preferences, 'generation_probe_attempts') - 1:
                remote['watch_attention'] = True
                self.store.update(row['id'], 'uncertain', remote=remote,
                    error='长时间无进展；已自动核对原任务但结果仍不明确。请打开原浏览器检查，未重新生成。')
                continue
            self.store.update(row['id'], row['status'], remote=remote, error='进度停滞，正在核对原任务；不会重新生成。')
            if client:
                self.request(client, 'snapshot')
                if remote.get('cloud_finished') and any(b and b.get('readyIds') for b in remote.get('bindings', [])):
                    self.transfer_failed({**row, 'remote': remote}, 'download', '下载进度停滞，正在自动恢复原视频下载')
        return changed

    def extra_action(self, command):
        action = command['action']
        if action == 'reset_tasks':
            self.reset_tasks(command)
        elif action == 'retry_download_selected':
            for row in self.store.rows():
                if row['id'] in command.get('ids', []):
                    if row['status'] == 'archive_failed' and row['remote'].get('download', {}).get('state') == 'complete':
                        self.archive(row['id'])
                    else:
                        self.retry_download(row)
        elif action == 'delete_records':
            # File workers and in-flight download retries must finish first.
            busy = set(self.archiving)
            for request in self.pending.values():
                if request['job'] and request['action'] in {'run_tasks', 'retry_download', 'upload_images'}:
                    row = self.store.get(request['job'])
                    if row and row['attempt'] == request['attempt'] and row['browser'] == request['client']:
                        busy.add(row['id'])
            result = self.store.delete_records(command.get('ids', []),
                confirmed_finished=command.get('confirmed_finished') is True, busy_ids=busy)
            deleted = set(result['deleted'])
            self.released.difference_update(deleted)
            self.pending = {key: value for key, value in self.pending.items() if value['job'] not in deleted}
            self.records_deleted.emit({'success': True, 'request_id': command.get('request_id'), **result})
        elif action == 'draft_save':
            self.store.save_draft(command['key'], command['value'])
            self.drafts = {**self.drafts, command['key']: copy.deepcopy(command['value'])}
        elif action == 'resume_browser':
            self.resume_browser(command)
        elif action == 'release_download':
            self.release_download(command)
        elif action == 'refresh_download_url':
            row = self.store.get(command['job'])
            if not row or row['status'] not in {'download_failed', 'download_parked'}:
                raise ValueError('只更新已结束生成、等待下载的原媒体地址。')
            url = str(command['url']).strip(); parsed = urlparse(url)
            media = None
            if parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443):
                if parsed.hostname == 'flow-content.google':
                    match = re.fullmatch(r'/video/([a-zA-Z0-9_-]+)', parsed.path)
                    media = match[1] if match else None
                elif parsed.hostname == 'flow.google.com':
                    match = re.fullmatch(r'/asb/([a-zA-Z0-9_-]+)(?:=[a-zA-Z0-9_,.-]+)?', parsed.path)
                    media = match[1] if match else None
                elif parsed.hostname == 'labs.google' and parsed.path == '/fx/api/trpc/media.getMediaUrlRedirect':
                    name = parse_qs(parsed.query).get('name', [''])[0]
                    match = re.search(r'(?:^|/)media/([a-zA-Z0-9_-]+)$', name)
                    media = match[1] if match else name if re.fullmatch('[a-zA-Z0-9_-]+', name) else None
            remote = copy.deepcopy(row['remote'])
            binding = next((b for b in remote.get('bindings', []) if b and b.get('schema') == 2 and media in b.get('mediaIds', [])), None)
            if not media or binding is None:
                raise ValueError('地址必须属于已保存的原 Google 媒体 ID；不能换成其他任务的视频。')
            binding.setdefault('urls', {})[media] = url
            binding['readyIds'] = list(dict.fromkeys(binding.get('readyIds', []) + [media]))
            remote.update(cloud_finished=True, released_at=remote.get('released_at') or time.time())
            self.store.update(row['id'], 'download_failed', remote=remote)
            self.retry_download(self.store.get(row['id']))
        elif action == 'batch_limit':
            self.store.set_batch_limit(command['batch_id'], command['limit'])
        elif action == 'open_browser':
            row = self.store.get(command['job']) if command.get('job') else None
            browser = row['browser'] if row else command.get('browser')
            profile = row['remote'].get('profile') if row else command.get('profile')
            client = next((c for c in self.clients.values() if c.get('authorized') and
                           (browser and c.get('id') == browser or profile and c.get('profile') == profile)), None)
            profile = profile or (client or {}).get('profile')
            if profile:
                from .setup_support import project_identity
                original = row['remote'].get('page') if row else ''
                saved = self.preferences.get('profile_projects', {}).get(profile) or (client or {}).get('saved_project')
                if original and project_identity(original) != project_identity(saved):
                    raise ValueError('原任务的项目与此配置保存的地址不同，请在连接向导核对；没有打开其他项目。')
                if client:
                    self.request(client, 'open_project', {'url': saved})
                    if not client.get('page_ready'):
                        from .connection_recovery import repair_idle
                        repair_idle(self, client)
                else:
                    self.launch_requested.emit(profile)
            elif client:
                # Legacy unlabelled clients can only activate an existing tab.
                self.request(client, 'open_project', {'url': row['remote'].get('page') if row else ''})
            else:
                raise ValueError('原浏览器未连接，请在 Chrome 启动器打开对应配置。')
        elif action in {'priority', 'edit'}:
            planned = []
            for row in self.store.rows():
                if row['id'] not in command['ids']:
                    continue
                if row['status'] != 'queued' or row['attempt']:
                    raise ValueError('只能修改尚未提交的排队任务。')
                payload = dict(row['payload'])
                if action == 'priority':
                    payload['priority'] = max(-100, min(100, int(command['value'])))
                else:
                    values = command['value']
                    target = {'target_dir': payload['directory'], 'task': payload}
                    prompt = payload['prompt'] if values.get('prompt') is None else values['prompt']
                    updated = make_jobs(target, [payload['image']], prompt,
                        **{key: values[key] for key in ('duration', 'aspect', 'resolution', 'budget')})[0]
                    if self.store.duplicates([updated], exclude=[row['id']]) and not command.get('allow_duplicates'):
                        raise ValueError('修改后的内容已有任务正在处理；需要变体请从手动面板明确再次提交。')
                    payload.update({key: updated[key] for key in ('prompt', 'duration', 'aspect', 'resolution', 'budget', 'image_identity')})
                planned.append((row['id'], payload))
            self.store.set_payloads(planned)
        elif action == 'review':
            for row in self.store.rows():
                if row['id'] in command['ids'] and row['status'] in {'completed', 'needs_review'}:
                    accepted = bool(command['accepted'])
                    self.store.update(row['id'], 'completed' if accepted else 'needs_review',
                        remote={**row['remote'], 'review_decision': '采用' if accepted else '待调整'},
                        error='' if accepted else '用户标记待调整，保留现有成品。')
        elif action == 'cleanup':
            from .archive import cleanup_download
            for row in self.store.rows():
                if row['id'] not in command['ids']:
                    continue
                if row['id'] in self.archiving:
                    continue
                def work(current=row):
                    try:
                        return {'id': current['id'], 'attempt': current['attempt'], 'cleanup_only': True,
                                'download_cleaned': cleanup_download(current, self.downloads_root)}
                    except Exception as error:
                        return {'id': current['id'], 'attempt': current['attempt'], 'cleanup_only': True, 'error': str(error)}
                future = self.pool.submit(work)
                self.archiving.add(row['id'])
                def completed(result, job_id=row['id'], attempt=row['attempt']):
                    try:
                        value = result.result()
                    except Exception as error:
                        value = {'id': job_id, 'attempt': attempt, 'cleanup_only': True, 'error': str(error) or '清理任务已取消'}
                    self.archive_result.emit(value)
                future.add_done_callback(completed)
        else:
            return False
        return True

    def space_status(self, rows):
        if time.time() - getattr(self, '_space_at', 0) < timing_value(self.preferences, 'disk_check_seconds'):
            return self._spaces
        paths = [self.downloads_root] + list(dict.fromkeys(row['payload']['directory'] for row in rows))[-8:]
        drives = {}
        for index, label in enumerate(paths):
            anchor = Path(label).anchor or label
            drives.setdefault(anchor, set()).add('下载' if index == 0 else '任务')
        spaces = []
        for anchor, roles in drives.items():
            path = Path(anchor); label = anchor + '（' + '/'.join(sorted(roles)) + '盘）'
            try:
                while not path.exists() and path.parent != path:
                    path = path.parent
                free = shutil.disk_usage(path).free
                spaces.append({'path': label, 'free': free})
            except OSError:
                spaces.append({'path': label, 'free': None})
        self._space_at, self._spaces = time.time(), spaces
        return spaces
