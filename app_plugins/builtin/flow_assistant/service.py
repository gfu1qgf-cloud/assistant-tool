"""Worker-thread coordinator for authenticated extension sessions and saved jobs."""
from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import math
from pathlib import Path
import secrets
import time
import uuid

from qt_compat import QtCore, QtNetwork
from PyQt6.QtWebSockets import QWebSocketServer

from .archive import archive_download
from .store import QueueStore, TRANSIENT, choose_client, fingerprint, make_jobs
from .routing import cost_key, lane_for, lane_key, dispatch_rank, automatic_lane, normalize_lane
from .credit_policy import debit_balance
from .media_identity import owns_download, generated_bindings, merge_snapshot_bindings
from .operations import FlowOperations
from .policy import capable, block_for, estimated_cost, batch_wait, waiting_reason
from .setup_support import EXTENSION_ID, EXTENSION_VERSION, OPTIONS_URL, profile_name, project_url, project_identity, project_block, compatible_extension
from .connection_status import configured_profiles, browser_keys
from .pricing import fixed_cost
from .transfer_policy import retry_info, retry_detail, ready_bindings
from .setup_pairing import confirm_saved_connection
from .page_readiness import defer_prepare, watch_preparation, recover_window_block, retry_initial_credits, missing_window

DEFAULT_PORT = 18189
PROTOCOL = 1


class FlowService(QtCore.QObject, FlowOperations):
    changed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    launch_requested = QtCore.pyqtSignal(str)
    running_changed = QtCore.pyqtSignal(bool)
    finished_job = QtCore.pyqtSignal(str)
    submitted = QtCore.pyqtSignal(object)
    submission_failed = QtCore.pyqtSignal(object)
    setup_ready = QtCore.pyqtSignal(object)
    records_deleted = QtCore.pyqtSignal(object)
    stopped = QtCore.pyqtSignal()
    archive_result = QtCore.pyqtSignal(object)

    def __init__(self, namespace, downloads_root):
        super().__init__()
        self.namespace, self.downloads_root = namespace, downloads_root
        self.clients, self.pending = {}, {}
        self.paused, self.closing, self.last_launch = True, False, 0
        self.launched, self.archiving = set(), set()
        self.launching = {}
        # Explicit per-item releases are session-local: restarting never submits
        # a paid request until the user resumes it. The jobs themselves persist.
        self.released, self.observed_costs = set(), {}
        self.safety_paused = False
        self.drafts = {}
        self.setup_requests = {}
        self.credit_debits = {}
        self.archive_result.connect(self._archived)

    @QtCore.pyqtSlot()
    def start(self):
        try:
            self.store = QueueStore(self.namespace)
            self.store.recover()
            self.preferences = self.store.settings()
            self.drafts = self.store.drafts()
            self.observed_costs = {tuple(item['key']): item['cost'] for item in self.preferences.get('observed_costs', [])}
            self.recover_fixed_price_blocks()
            self.downloads_root = self.preferences.get('downloads_root') or self.downloads_root
            pair_file = self.namespace.file('pairing.json')
            pair = pair_file.read_json() if pair_file.exists() else {'token': secrets.token_urlsafe(32), 'port': DEFAULT_PORT}
            if not pair_file.exists():
                pair_file.write_json(pair)
            self.pair = {**pair, 'protocol': PROTOCOL, 'extension_id': EXTENSION_ID}
            self.server = QWebSocketServer('AssistantTool.FlowBridge.v1', QWebSocketServer.SslMode.NonSecureMode, self)
            from model.OfflineMode import is_offline
            if not is_offline() and not self.server.listen(QtNetwork.QHostAddress.SpecialAddress.LocalHost, int(pair['port'])):
                raise RuntimeError(f'Flow 本地端口 {pair["port"]} 无法监听：{self.server.errorString()}')
            self.server.newConnection.connect(self._accept)
            self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='flow-archive')
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(1000)
            self.publish()
        except Exception as error:
            self.failed.emit(str(error))

    def send(self, client, message):
        client['socket'].sendTextMessage(json.dumps(message, ensure_ascii=False))

    def request(self, client, action, params=None, job=None):
        if action == 'refresh_project':
            # A reload can cancel an accepted website generation. Even legacy
            # recovery plans may only query snapshots; never send this command.
            return
        if action == 'get_points' and any(p['action'] == action and p['client'] == client['id'] for p in self.pending.values()):
            return
        request_id = uuid.uuid4().hex
        row = self.store.get(job) if job else None
        self.pending[request_id] = {'action': action, 'job': job, 'client': client['id'], 'at': time.time(),
                                    'attempt': row['attempt'] if row else ''}
        if action == 'get_points':
            self.pending[request_id]['credit_debits'] = dict(self.credit_debits)
            client['credit_check'] = {'state': 'checking', 'at': time.time(), 'message': '正在读取本浏览器当前账号和积分'}
        params = dict(params or {})
        if action in {'prepare', 'upload_images', 'run_tasks'}:
            params['wait_for_ready'] = True
        self.send(client, {'action': action, 'request_id': request_id, 'params': params})

    def _accept(self):
        socket = self.server.nextPendingConnection()
        if socket.origin() != f'chrome-extension://{EXTENSION_ID}':
            socket.abort()
            socket.deleteLater()
            return
        socket.setMaxAllowedIncomingMessageSize(1024 * 1024)
        socket.setMaxAllowedIncomingFrameSize(1024 * 1024)
        self.clients[socket] = {'socket': socket, 'authorized': False, 'connected': time.time()}
        socket.textMessageReceived.connect(lambda text, s=socket: self._message(s, text))
        socket.disconnected.connect(lambda s=socket: self._disconnected(s))

    def _disconnected(self, socket):
        client = self.clients.pop(socket, {})
        if self.closing:
            socket.deleteLater()
            return
        if client.get('id') and any(c.get('authorized') and c.get('id') == client['id'] for c in self.clients.values()):
            # A late disconnect from the replaced socket does not own the
            # new connection's attempt, even though the browser ID is stable.
            socket.deleteLater()
            return
        for row in self.store.rows():
            if row['browser'] == client.get('id') and row['status'] in TRANSIENT:
                if row['status'] in {'uploading', 'downloading'}:
                    self.transfer_failed(row, 'upload' if row['status'] == 'uploading' else 'download', '原浏览器断开，重连后自动恢复传输')
                    continue
                status = 'failed' if row['status'] in {'preparing', 'uploading'} else 'uncertain'
                self.store.update(row['id'], status, error='浏览器断开；已提交的任务会先核对，不自动重复生成。')
        socket.deleteLater()
        if not self.closing:
            self.publish()

    def _message(self, socket, text):
        client = self.clients.get(socket)
        if not client:
            return
        try:
            if len(text) > 1024 * 1024:
                raise ValueError('消息过大')
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ValueError('消息格式无效')
            if not client['authorized']:
                if message.get('type') == 'setup_lookup':
                    from .setup_pairing import lookup_setup
                    lookup_setup(self, client, message)
                    return
                if message.get('type') == 'setup_reload_check':
                    from .setup_pairing import check_reload
                    check_reload(self, client, message)
                    return
                if message.get('type') == 'setup_claim':
                    from .setup_pairing import claim_setup
                    claim_setup(self, client, message)
                    return
                if message.get('type') != 'hello' or message.get('protocol') != PROTOCOL or not hmac.compare_digest(str(message.get('token', '')), self.pair['token']):
                    socket.close()
                    return
                browser_id = str(message.get('browser_id') or '')
                if not browser_id or len(browser_id) > 100:
                    raise ValueError('浏览器标识无效')
                for other, info in list(self.clients.items()):
                    if other is not socket and info.get('id') == browser_id:
                        other.close()
                client.update(authorized=True, id=browser_id, profile=str(message.get('profile') or '')[:100],
                              project=message.get('project') or '', saved_project=message.get('project') or '',
                              extension_version=str(message.get('extension_version') or '')[:30],
                              setup_id=str(message.get('setup_id') or '')[:32],
                              capabilities=[c for c in message.get('capabilities', []) if isinstance(c, str)][:20],
                              page_ready=False, page_at=0, page_error='',
                              account='', credits=None, credits_at=0, blocked='')
                setup_request = self.setup_requests.get(client['profile'], {})
                expected_setup = setup_request.get('id') if setup_request.get('mode') != 'reload_only' else None
                if setup_request.get('mode') == 'reload_only' and client['extension_version'] == EXTENSION_VERSION:
                    self.setup_requests.pop(client['profile'], None)
                client['setup_pending'] = bool(expected_setup and expected_setup != message.get('setup_id'))
                if expected_setup and not client['setup_pending']:
                    self.setup_requests.pop(client['profile'], None)
                if (client['profile'] and project_identity(client['saved_project'])
                        and client['profile'] not in self.preferences.get('profile_projects', {})):
                    profile = profile_name(client['profile'])
                    preferences = {**self.preferences, 'profile_projects': {
                        **self.preferences.get('profile_projects', {}), profile: project_url(client['saved_project'], required=True)}}
                    self.store.save_settings(preferences)
                    self.preferences = preferences
                client['setup_blocked'] = project_block(client, self.preferences)
                self.launching.pop(client['profile'], None)
                self.send(client, {'type': 'welcome', 'protocol': PROTOCOL})
                self.request(client, 'get_points')
                self.request(client, 'snapshot')
                self.recover_extension_downloads(client)
                self.publish()
                return
            if message.get('type') == 'ping':
                self.send(client, {'type': 'pong'})
                return
            if message.get('type') == 'download':
                self._download(client, message)
            elif message.get('type') == 'generation_response':
                from .page_recovery import record_response
                record_response(self, client, message)
            elif message.get('type') == 'binding_missing':
                from .page_recovery import schedule
                schedule(self, client, message)
            elif message.get('type') == 'snapshot':
                self._snapshot(client, message.get('data') or {})
            elif message.get('type') == 'page_status':
                client['project'] = message.get('url') or ''
                client['note'] = message.get('note') or ''
                client['page_loaded'] = bool(project_identity(client['project']))
                client['page_ready'] = client['page_loaded'] and message.get('controls_ready', True) is True
                client['ready_note'] = str(message.get('ready_note') or '')[:300]
                client['page_at'] = time.time()
                client['page_error'] = ''
                client['setup_blocked'] = project_block(client, self.preferences)
                confirm_saved_connection(self, client)
                recover_window_block(self, client)
                retry_initial_credits(self, client)
            elif message.get('type') == 'site_pause':
                self.paused = self.safety_paused = True
                self.released.clear()
                client['blocked'] = 'Flow 网站提示安全限制，后续提交已暂停；保留当前生成页面，不自动刷新。'
            elif message.get('type') in {'hosted_task_status', 'hosted_batch_finished'}:
                self._status(client, message)
            else:
                self._response(client, message)
            self.publish()
        except Exception as error:
            self.failed.emit('Flow 消息处理失败：' + str(error))

    def _response(self, client, message):
        request = self.pending.pop(str(message.get('request_id')), None)
        if not request or request['client'] != client['id']:
            return
        action, job_id = request['action'], request['job']
        data = message.get('data') or {}
        if action == 'get_points':
            client['credit_check'] = {'state': 'failed', 'at': time.time(),
                'message': str(message.get('error') or ('尚未确认当前账号邮箱' if not data.get('email') else '页面未返回有效积分'))[:500]}
            if message.get('code') == 'FLOW_ACCOUNT_NOT_READY' or (message.get('success') and not data.get('email')):
                failures = client.get('credits_ready_failures', 0) + 1
                client.update(account='', credits_local=False, credits_waiting_account=True,
                    credits_waiting_ready=failures < 3, credits_ready_failures=failures,
                    credits_retry_at=time.time() + 5,
                    note=('账号菜单未返回可确认的邮箱，正在自动重试首次读取' if failures < 3 else
                          '账号读取连续3次未完成；请检查 Flow 账号菜单，修正后点击刷新状态'))
                return
            if not message.get('success', True) and (message.get('code') == 'FLOW_PAGE_NOT_READY' or missing_window(message.get('error', ''))):
                client.update(page_ready=False, credits_waiting_ready=True, credits_waiting_account=False,
                    credits_ready_failures=client.get('credits_ready_failures', 0)+1,
                    page_error='页面尚未稳定，控件就绪后自动重试本次首次积分读取')
                return
            client['credits_waiting_ready'] = False
            client['credits_waiting_account'] = False
            client['credits_retry_at'] = 0
            if message.get('success', True) and data.get('email'):
                client['credits_ready_failures'] = 0
            client['account'] = str(data.get('email') or '').strip().casefold()
            points = data.get('remaining_points')
            client['credits'] = points if isinstance(points, int) and not isinstance(points, bool) and points >= 0 else None
            client['credits_at'] = time.time()
            client['credits_local'] = bool(client['account']) and type(client['credits']) is int
            client['credits_estimated'] = False
            if client['credits_local']:
                # A slow observation may have started before a new submission.
                # Do not let its old balance undo an intervening local debit.
                spent = self.credit_debits.get(client['account'], 0) - request.get('credit_debits', {}).get(client['account'], 0)
                balance = max(0, client['credits'] - max(0, spent))
                for current in self.clients.values():
                    if str(current.get('account') or '').casefold() == client['account']:
                        current.update(credits=balance, credits_local=True, credits_at=client['credits_at'],
                                       credits_estimated=spent > 0)
            client['note'] = message.get('error') or ('' if client['account'] else '尚未确认当前 Flow 登录账号')
            if message.get('success', True) and client['credits_local']:
                client['credit_check'] = {'state': 'success', 'at': time.time(),
                    'message': '已读取当前账号和积分；后续按实际提交费用本地扣算'}
            if not message.get('success', True):
                client['page_error'] = str(message.get('error') or 'Flow 页面未响应')[:500]
            if client.get('blocked') == '积分不足':
                client['blocked'] = ''
            return
        if not job_id:
            return
        row = self.store.get(job_id)
        if not row or row['attempt'] != request['attempt'] or row['browser'] != client['id'] or row['status'] not in TRANSIENT | {'download_failed', 'uncertain'}:
            return
        if not message.get('success'):
            if action == 'refresh_project':
                recovery = {**row['remote'].get('page_recovery', {}), 'done': True, 'next_at': 0}
                self.store.update(job_id, 'uncertain', remote={**row['remote'], 'page_recovery': recovery},
                    error='自动刷新未执行：'+str(message.get('error') or '原页面未响应')+'；未重新生成。')
                return
            if action == 'prepare':
                if defer_prepare(self, client, row, message.get('error') or '', message.get('code', '')):
                    return
                self.quarantine(client, row, str(message.get('error') or '页面参数核对失败'))
                return
            if action in {'upload_images', 'retry_download'}:
                detail = message.get('error') or '；'.join(str(value) for value in (data.get('errors') or {}).values()) or '浏览器传输未完成'
                permanent = action == 'upload_images' and any(word in str(detail).casefold() for word in
                    ('captcha', '验证码', '需要登录', 'sign in', 'log in', 'unusual activity'))
                self.transfer_failed(row, 'upload' if action == 'upload_images' else 'download', detail, permanent=permanent)
                return
            status = 'uncertain' if action == 'run_tasks' else 'download_failed' if action == 'retry_download' else 'failed'
            self.store.update(job_id, status, error=str(message.get('error') or '浏览器操作未完成')[:1000])
            return
        if action == 'prepare':
            cost, budget = data.get('cost'), row['payload']['budget']
            rule = fixed_cost(row['payload'])
            cost_source = 'duration_rule' if rule is not None else 'page'
            if rule is not None: cost = rule
            if not data.get('verified') or data.get('model') != 'omni_1_1_flash':
                self.quarantine(client, row, '页面未确认 Omni 参数，未提交生成。', status='failed')
                return
            if ('email' in data and str(data['email']).strip().casefold() != client['account']
                    or row['remote'].get('account') and row['remote']['account'] != client['account']):
                self.quarantine(client, row, '核对参数时登录账号发生变化或无法确认，请刷新账号并恢复。')
                return
            if cost is None:
                batch = self.store.batches().get(row['payload'].get('batch_id'), {})
                if batch.get('limit_total'):
                    self.quarantine(client, row, '已设置整批积分上限，但页面费用未知；请修正费用读取后恢复，不能用预算冒充真实费用。')
                    return
                if not budget:
                    self.quarantine(client, row, '本条未指定可按固定规则计费的时长：请选择 6、8 或 10 秒；其他时长／模型默认需要页面费用或明确的单条预算。')
                    return
                cost = budget
                cost_source = 'user_budget'
            if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= 10000:
                raise ValueError('页面费用无效')
            if cost_source == 'page':
                self.remember_cost(row['payload'], cost)
            if budget and cost > budget:
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error=f'本条费用 {cost} 超出设置的预算 {budget}，等待修改预算；未提交。')
                return
            if client['credits'] is None or cost > client['credits']:
                client['blocked'] = '积分不足'
                self.store.update(job_id, 'queued', attempt='', browser='', remote={}, error='等待有足够积分的账号')
                return
            remote = {key: value for key, value in row['remote'].items() if key != 'prepare_retry'}
            remote.update(cost=cost, cost_source=cost_source)
            self.store.update(job_id, 'uploading', remote=remote)
            self.start_upload(self.store.get(job_id))
        elif action == 'upload_images':
            info = {**retry_info(row, 'upload'), 'active': False, 'next_at': 0, 'waiting': '', 'done': True}
            self._save_transfer(row, 'upload', info)
            row = self.store.get(job_id)
            if not self.media_disks_ready(row):
                return
            if row['remote'].get('account') and row['remote']['account'] != client['account']:
                self.quarantine(client, row, '上传期间登录账号变化，请刷新账号并重新核对。')
                return
            if client.get('credits') is None or row['remote']['cost'] > client['credits']:
                client['blocked'] = '积分不足'
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error='图片上传完成，但原账号积分不足；等待备用账号，未提交生成。')
                return
            if not self.store.reserve(job_id, row['attempt'], row['remote']['cost'], row['remote']['cost_source']):
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error='已达到整批积分上限，停止新增提交；可调整该批上限后继续。')
                return
            self.store.update(job_id, 'uploading', remote={**row['remote'], 'activity_at': time.time()})
            self.store.update(job_id, 'submitting')  # Commit before sending any generation request.
            debit_balance(client, self.clients.values(), row['remote']['cost'])
            account_key = client['account'].casefold()
            self.credit_debits[account_key] = self.credit_debits.get(account_key, 0) + row['remote']['cost']
            job = row['payload']
            params = {'batch_id': row['attempt'], 'clear_existing': True, 'expected_account': client['account'], 'tasks': [{
                'prompt': ' '.join(job['prompt'].split()), 'local_image_path': job['image'],
                'image_name': Path(job['image']).name, 'download_path': self.download_name(row),
                'model': 'omni_1_1_flash', 'videoModel': 'omni_1_1_flash', 'mode': 'VIDEO_FRAMES',
                'resolution': 'VIDEO_RESOLUTION_' + job['resolution'].upper(),
                'aspectRatio': 'ASPECT_RATIO_' + job['aspect'].replace(':', '_'), 'duration': job['duration'], 'count': 1}]}
            self.request(client, 'run_tasks', params, job_id)
        elif action in {'run_tasks', 'retry_download'}:
            remote = row['remote']
            deferred = []
            if action == 'retry_download':
                deferred = remote.get('deferred_download_interrupts', [])
                remote = {key: value for key, value in remote.items() if key != 'deferred_download_interrupts'}
                info = {**retry_info(row, 'download'), 'active': True, 'next_at': 0, 'waiting': ''}
                if data.get('downloadId') is not None: info['download_id'] = data['downloadId']
                remote = {**remote, 'activity_at': time.time(), 'transfer_retries': {
                    **remote.get('transfer_retries', {}), 'download': info}}
            self.store.update(job_id, 'generating' if action == 'run_tasks' else 'downloading', remote=remote)
            # The Chrome download can fail before its start acknowledgement.
            # Resolve queued failures only after that acknowledgement identifies
            # the current download, so an older failure cannot cancel this request.
            for event in deferred:
                self._download(client, event)

    @staticmethod
    def download_name(row):
        return f'FlowBridge/{row["attempt"]}/result.mp4'

    def _snapshot(self, client, data):
        # Snapshots are events, including on older compatible extensions; they
        # do not send a separate request_id response. This page has answered.
        for key, request in list(self.pending.items()):
            if request['action'] == 'snapshot' and request['client'] == client['id']:
                self.pending.pop(key, None)
        if project_identity(data.get('url')):
            client.update(project=data['url'], page_loaded=True,
                page_ready=data.get('controls_ready', True) is True, ready_note=str(data.get('ready_note') or '')[:300],
                page_at=time.time(), page_error='')
            client['setup_blocked'] = project_block(client, self.preferences)
            confirm_saved_connection(self, client)
            recover_window_block(self, client)
            retry_initial_credits(self, client)
        client['active_batch'] = str(data.get('batch_id') or '')
        batch = str(data.get('batch_id') or '')
        row = next((r for r in self.store.rows() if r['attempt'] == batch and r['browser'] == client['id']), None)
        if not row or row['status'] in {'completed', 'cancelled', 'archiving', 'archive_failed', 'download_parked', 'needs_review'}:
            return
        tasks = data.get('tasks') or []
        if not tasks:
            return
        task = next((task for task in tasks if task.get('download_path') == self.download_name(row)), None)
        if not task or not isinstance(task.get('taskId'), str) or not task['taskId']:
            return
        bindings = task.get('mediaBindings') or []
        previous = generated_bindings(row['remote'])
        incoming = generated_bindings({'bindings': bindings})
        identity = lambda b: (b['taskId'], b['submissionId'], tuple(sorted(b['mediaIds'])))
        if (row['remote'].get('upstream_task') and task['taskId'] != row['remote']['upstream_task'] or
                previous and incoming and {identity(b) for b in previous} != {identity(b) for b in incoming}):
            self.store.update(row['id'], 'uncertain', error='原任务的媒体归属发生变化，已停止接收替代结果；保留原绑定，不会重新生成。')
            return
        bindings = merge_snapshot_bindings(row['remote'], bindings)
        # A refreshed signed URL is not generation/download progress.
        progress = {'running': data.get('running'), 'task_id': task.get('taskId'),
                    'status': task.get('status'), 'downloads': task.get('downloadIds'),
                    'bindings': [{key: binding.get(key) for key in ('submissionId', 'mediaIds', 'readyIds', 'failedIds')}
                                 for binding in bindings if isinstance(binding, dict)]}
        stamp = json.dumps(progress, sort_keys=True)
        remote = {**row['remote'], 'bindings': bindings, 'upstream_task': task.get('taskId'), 'page': data.get('url')}
        if remote.get('progress_stamp') != stamp:
            remote.update(progress_stamp=stamp, activity_at=time.time(), watch_count=0, watch_attention=False)
        # Upstream keeps its batch "running" while it waits for downloads.
        # A ready media ID is completion evidence even during that wait.
        remote['cloud_finished'] = bool(remote.get('cloud_finished') or ready_bindings({'bindings': bindings}))
        status = row['status']
        if row['status'] == 'downloading' and retry_info(row, 'download').get('active'):
            status = 'downloading'
        elif row['status'] == 'uncertain' and (remote.get('watch_attention') or
                remote.get('page_recovery') and not incoming):
            status = 'uncertain'
        elif row['status'] == 'download_failed' and retry_info(row, 'download'):
            status = 'download_failed'
        elif ready_bindings({'bindings': bindings}) and row['status'] != 'downloading':
            status = 'download_failed'
        elif data.get('running') and row['status'] != 'downloading':
            status = 'generating'
        elif any(binding and binding.get('mediaIds') for binding in bindings):
            if not (row['status'] == 'downloading' and any(task.get('downloadIds') or []) and 'failed' not in (task.get('status') or [])):
                status = 'download_failed'
        if remote != row['remote'] or status != row['status']:
            self.store.update(row['id'], status, remote=remote, error=row['error'] if status in {'uncertain', 'download_failed'} else '')
        pending = remote.get('unverified_download')
        if pending and owns_download(self.store.get(row['id']), pending):
            self._download(client, {'batch_id': row['attempt'], 'event_id': remote.get('unverified_download_event'), 'data': pending})

    def _download(self, client, message):
        data = message.get('data') or {}
        batch = str(message.get('batch_id') or '')
        row = next((r for r in self.store.rows() if r['attempt'] == batch and r['browser'] == client['id']), None)
        if not row:
            if self.store.was_deleted(batch, client['id']):
                # Drain late notifications for explicitly deleted records;
                # never recreate a job or archive its files behind the user's back.
                self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            return
        if row['status'] in {'completed', 'needs_review', 'cancelled', 'archiving'}:
            self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            return
        if data.get('download_path') != self.download_name(row) or not data.get('mediaId') or not data.get('submissionId'):
            raise ValueError('下载归属标识不完整')
        if not owns_download(row, data):
            self.store.update(row['id'], 'uncertain', remote={**row['remote'], 'unverified_download': data,
                'unverified_download_event': message.get('event_id')},
                error='收到浏览器下载，但缺少本次生成响应的归属证据；未归档为任务成品，正在核对原任务，不会重新生成。')
            self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            self.request(client, 'snapshot')
            return
        events = row['remote'].get('download_events', [])
        if message.get('event_id') and message['event_id'] in events:
            self.send(client, {'type': 'ack', 'event_id': message['event_id']})
            return
        if data.get('state') == 'interrupted' and self._transfer_pending(row, 'download'):
            deferred = row['remote'].get('deferred_download_interrupts', [])
            if message not in deferred:
                self.store.update(row['id'], row['status'], remote={**row['remote'],
                    'deferred_download_interrupts': (deferred + [message])[-8:]}, error=row['error'])
            self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            return
        current_download = retry_info(row, 'download').get('download_id')
        if data.get('state') == 'interrupted' and current_download is not None and data.get('downloadId') not in (None, current_download):
            self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            return
        if row['status'] not in {'completed', 'archiving'}:
            self.store.update(row['id'], 'archive_failed' if data.get('state') == 'complete' else 'download_failed',
                remote={**{k:v for k,v in row['remote'].items() if k not in {'unverified_download', 'unverified_download_event'}}, 'download': data, 'cloud_finished': True, 'activity_at': time.time(),
                        'download_events': (events + [message['event_id']])[-20:] if message.get('event_id') else events}, error=data.get('error') or '')
            if data.get('state') != 'complete':
                self.transfer_failed(self.store.get(row['id']), 'download', data.get('error') or '浏览器下载中断')
        self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
        if data.get('state') == 'complete' and row['status'] not in {'completed', 'archiving'}:
            self.archive(row['id'])

    def _status(self, client, message):
        row = next((r for r in self.store.rows() if r['attempt'] == message.get('batch_id') and r['browser'] == client['id']), None)
        if not row or row['status'] in {'completed', 'archiving', 'archive_failed', 'download_parked', 'needs_review'}:
            return
        if row['status'] == 'downloading' and retry_info(row, 'download').get('active'):
            return
        if message.get('status') == 'failed' or (message.get('type') == 'hosted_batch_finished' and any(r.get('status') == 'failed' for r in message.get('results', []))):
            text = str(message.get('message') or next((r.get('message') for r in message.get('results', []) if r.get('message')), '生成未完成'))
            if any(word in text.casefold() for word in ('captcha', 'unusual', '验证码', '异常活动', 'risk', '风控')):
                client['blocked'], self.paused = '需要网站验证', True
                self.released.clear()
                self.safety_paused = True
                self.failed.emit('Flow 要求网站验证，队列已暂停：' + text)
            bindings = row['remote'].get('bindings') or []
            status = 'download_failed' if any(b and b.get('mediaIds') for b in bindings) else 'uncertain'
            self.store.update(row['id'], status, error=text[:1000])
        self.request(client, 'snapshot')

    def archive(self, job_id):
        if job_id in self.archiving:
            return
        row = self.store.get(job_id)
        if not row or not row['remote'].get('download'):
            return
        if not owns_download(row, row['remote']['download']):
            self.store.update(job_id, 'uncertain', error='下载与本次生成响应的媒体归属不一致，已阻止归档；未覆盖成品。')
            return
        self.archiving.add(job_id)
        self.store.update(job_id, 'archiving')
        from .archive import archive_package
        future = self.pool.submit(archive_package, row, self.downloads_root,
                                  cleanup=self.preferences.get('cleanup_downloads', False))
        def completed(result):
            try:
                package = result.result()
                value = {'id': job_id, **package}
            except Exception as error:
                value = {'id': job_id, 'error': str(error)}
            self.archive_result.emit(value)
        future.add_done_callback(completed)

    @QtCore.pyqtSlot(object)
    def _archived(self, result):
        self.archiving.discard(result['id'])
        row = self.store.get(result['id'])
        if not row:
            if not self.closing: self.publish()
            return
        if result.get('cleanup_only'):
            self.store.update(row['id'], row['status'], remote={**row['remote'],
                'download_cleaned': result.get('download_cleaned', False)}, error=result.get('error', row['error']))
            self.publish()
            return
        review = result.get('review', [])
        self.store.update(result['id'], 'archive_failed' if result.get('error') else 'needs_review' if review else 'completed',
            remote={**row['remote'], **{key: result[key] for key in ('review', 'media', 'download_cleaned', 'cleanup_warning') if key in result}},
            error=result.get('error') or '；'.join(review) or result.get('cleanup_warning', ''))
        if not result.get('error'):
            self.finished_job.emit(result['output'])
        if not self.closing:
            self.publish()

    @QtCore.pyqtSlot(object)
    def execute(self, command):
        try:
            action = command['action']
            if action == 'enqueue':
                previous = self.store.submission(command.get('request_id')) if command.get('request_id') else None
                if previous:
                    rows = [r for r in self.store.rows() if r['payload'].get('batch_id') == previous]
                    self.submitted.emit({'count': len(rows), 'source': command.get('source', 'batch'),
                        'request_id': command.get('request_id'), 'ids': [r['id'] for r in rows], 'batch_id': previous,
                        'held': self.safety_paused or self.paused and not any(r['id'] in self.released for r in rows)})
                    return
                jobs = []
                for item in command['items']:
                    jobs.extend(make_jobs(**item))
                source = command.get('source', 'batch')
                for job in jobs:
                    job['submission_mode'] = source
                duplicates = self.store.duplicates(jobs)
                if duplicates and not command.get('allow_duplicates'):
                    self.submission_failed.emit({'source': source, 'request_id': command.get('request_id'),
                        'error': '相同图片、提示词和参数已有任务等待或生成中。', 'duplicate_ids': duplicates})
                    self.failed.emit('Flow 未重复提交：已有相同任务等待或生成中；可查看原任务或明确生成变体。')
                    return
                batch_id = self.store.enqueue(jobs, limit_total=command.get('batch_limit', 0),
                    label=command.get('batch_label', ''), request_id=command.get('request_id'))
                start_after_save = source == 'manual' or command.get('start_after_save', False)
                if start_after_save and not self.safety_paused:
                    self.released.update(job['id'] for job in jobs)
                    self.launched.clear()
                self.submitted.emit({'count': len(jobs), 'source': source,
                                     'request_id': command.get('request_id'),
                                     'ids': [job['id'] for job in jobs],
                                     'batch_id': batch_id,
                                     'held': self.safety_paused or self.paused and not start_after_save})
            elif action == 'start':
                self.paused = False
                self.safety_paused = False
                self.launched.clear()
            elif action == 'pause':
                self.paused = True
                self.released.clear()
            elif action == 'submit_selected':
                self.released.update(row['id'] for row in self.store.rows()
                                     if row['id'] in command['ids'] and row['status'] == 'queued')
                self.safety_paused = False
                self.launched.clear()
            elif action == 'lane':
                scope, key = command['scope'], str(command['key']).strip()
                if scope not in {'profile', 'browser'} or not key:
                    raise ValueError('浏览器分档标识无效。')
                if command.get('mode') == 'automatic':
                    identity = {'profile': key} if scope == 'profile' else {'id': key}
                    self.set_lane(scope, key, lane_for(identity, self.preferences), automatic=True)
                else:
                    self.set_lane(scope, key, command.get('duration'))
            elif action == 'setup_profile':
                if not self.server.isListening():
                    raise ValueError('Flow 本地连接服务尚未启动，请检查端口或离线模式后重试。')
                profile = profile_name(command.get('profile'))
                project = project_url(command.get('project'), required=True)
                if not command.get('force_pair'):
                    for current in self.clients.values():
                        if current.get('profile') == profile:
                            confirm_saved_connection(self, current)
                existing = next((c for c in self.clients.values() if c.get('authorized') and
                    c.get('profile') == profile and not c.get('setup_pending') and
                    project_identity(c.get('saved_project')) == project_identity(project)), None)
                if existing and not compatible_extension(existing.get('extension_version')):
                    if any((r['status'] in TRANSIENT | {'archiving', 'uncertain'} or
                            r['status'] == 'download_failed' and not r['remote'].get('cloud_finished')) and
                           (r['browser'] == existing['id'] or r['remote'].get('profile') == profile) for r in self.store.rows()):
                        raise ValueError('此配置仍在生成／传输或结果未确认，暂不更新扩展和刷新页面；原任务保持不动。')
                    ticket = uuid.uuid4().hex
                    self.setup_requests[profile] = {'id': ticket, 'request': command.get('request_id'), 'project': project,
                        'expires': time.time()+120, 'mode': 'reload_only'}
                    entry = f'{OPTIONS_URL}#setup={ticket}&port={self.pair["port"]}'
                    project_entry = f'{project}#lzx_setup={ticket}&lzx_port={self.pair["port"]}&lzx_action=options'
                    self.setup_ready.emit({'request_id': command.get('request_id'), 'success': True,
                        'profile': profile, 'project': project, 'claim_url': entry, 'project_claim_url': project_entry,
                        'pairing': {**self.pair, 'profile': profile, 'project': project, 'setup_id': ticket}})
                    return
                if existing and not command.get('force_pair') and compatible_extension(existing.get('extension_version')):
                    preferences = {**self.preferences, 'profile_projects': {
                        **self.preferences.get('profile_projects', {}), profile: project}}
                    self.store.save_settings(preferences)
                    self.preferences = preferences
                    existing['setup_blocked'] = project_block(existing, preferences)
                    # Navigation belongs to the desktop launcher. In particular,
                    # an older worker must not create a hidden project tab here.
                    if not existing.get('credits_local'):
                        self.request(existing, 'get_points')
                    self.request(existing, 'snapshot')
                    self.setup_ready.emit({'request_id': command.get('request_id'), 'success': True,
                                          'connected': True, 'profile': profile, 'project': project,
                                          'pairing': {**self.pair, 'profile': profile, 'project': project,
                                              **({'setup_id': existing['setup_id']} if existing.get('setup_id') else {})},
                                          'needs_update': bool(existing.get('extension_version') and
                                                               existing['extension_version'] != EXTENSION_VERSION)})
                    self.publish()
                    return
                client_ids = {c.get('id') for c in self.clients.values() if c.get('profile') == profile}
                if any(row['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} and
                       (row['browser'] in client_ids or row['remote'].get('profile') == profile) for row in self.store.rows()):
                    raise ValueError('这个浏览器仍在执行生成／下载，当前不能重新设置连接，请等它完成。')
                preferences = {**self.preferences, 'profile_projects': {
                    **self.preferences.get('profile_projects', {}), profile: project}}
                self.store.save_settings(preferences)
                self.preferences = preferences
                previous_setup = self.setup_requests.get(profile, {})
                reuse = (previous_setup.get('mode') != 'reload_only'
                    and previous_setup.get('expires', 0) > time.time()
                    and project_identity(previous_setup.get('project')) == project_identity(project)
                    and (not command.get('force_pair') or command.get('request_id') and previous_setup.get('request') == command['request_id']))
                setup_id = previous_setup['id'] if reuse else uuid.uuid4().hex
                self.setup_requests[profile] = {**(previous_setup if previous_setup.get('id') == setup_id else {}),
                    'id': setup_id, 'request': command.get('request_id'), 'project': project,
                    'expires': previous_setup['expires'] if reuse else time.time() + 120,
                    'force_pair': bool(command.get('force_pair') or reuse and previous_setup.get('force_pair'))}
                for client in self.clients.values():
                    if client.get('profile') == profile:
                        client['setup_pending'] = True
                        client['setup_blocked'] = project_block(client, preferences)
                self.setup_ready.emit({'request_id': command.get('request_id'), 'success': True,
                    'profile': profile, 'project': project,
                    'claim_url': f'{OPTIONS_URL}#setup={setup_id}&port={self.pair["port"]}',
                    'project_claim_url': f'{project}#lzx_setup={setup_id}&lzx_port={self.pair["port"]}',
                    'pairing': {'token': self.pair['token'], 'port': self.pair['port'],
                                'profile': profile, 'project': project, 'extension_id': EXTENSION_ID, 'setup_id': setup_id}})
            elif action == 'preferences':
                self.commit_preferences({**self.preferences, **command['value']})
                self.downloads_root = self.preferences.get('downloads_root') or self.downloads_root
            elif action == 'credits':
                keys = set(command.get('keys') or [])
                selected = [client for client in self.clients.values() if client['authorized']
                            and ('keys' not in command or browser_keys(client) & keys)]
                if command.get('manual') and not selected:
                    raise ValueError('选中的浏览器未连接，请先连接该配置后再检测积分。')
                rows = self.store.rows() if command.get('manual') else []
                for client in selected:
                    busy = any((row['status'] in TRANSIENT or row['status'] == 'uncertain'
                                and not row['remote'].get('cloud_finished'))
                               and (row['browser'] == client['id'] or
                                    client.get('account') and row['remote'].get('account') == client['account'])
                               for row in rows)
                    if busy:
                        client['credit_check'] = {'state': 'deferred', 'at': time.time(),
                            'message': '此账号正在处理任务，完成后可手动检测；已保留当前任务'}
                        continue
                    self.request(client, 'get_points')
                    if command.get('manual'):
                        client['credit_check']['hold_dispatch'] = True
                    self.request(client, 'snapshot')
            elif action == 'cancel':
                for row in self.store.rows():
                    if row['id'] in command['ids'] and row['status'] in {'queued', 'failed'}:
                        self.store.update(row['id'], 'cancelled')
            elif action in {'retry', 'regenerate'}:
                if action == 'retry' and command.get('only_failed'):
                    from .policy import FAILURES
                    command['ids'] = [r['id'] for r in self.store.rows() if r['status'] in FAILURES]
                self.store.retry(command['ids'], regenerate=action == 'regenerate')
                if command.get('only_failed') and not self.safety_paused:
                    self.paused = True
                    self.released = {r['id'] for r in self.store.rows() if r['id'] in command['ids'] and r['status'] == 'queued'}
                for row in self.store.rows():
                    if row['id'] not in command['ids']:
                        continue
                    if row['status'] == 'archive_failed':
                        self.archive(row['id'])
                    elif row['status'] == 'download_failed':
                        self.retry_download(row)
            elif self.extra_action(command):
                pass
            elif action == 'stop':
                self.closing, self.paused = True, True
                self.released.clear()
                timer = getattr(self, 'timer', None)
                if timer: timer.stop()
                for socket in list(self.clients):
                    socket.close()
                server = getattr(self, 'server', None)
                if server: server.close()
                pool = getattr(self, 'pool', None)
                if pool: pool.shutdown(wait=True)
                self.stopped.emit()
                return
            self.publish()
        except Exception as error:
            if command.get('action') == 'delete_records':
                self.records_deleted.emit({'success': False, 'request_id': command.get('request_id'), 'error': str(error)})
            if command.get('action') == 'setup_profile':
                self.setup_ready.emit({'request_id': command.get('request_id'), 'success': False, 'error': str(error)})
            if command.get('action') == 'enqueue':
                self.submission_failed.emit({'source': command.get('source', 'batch'),
                    'request_id': command.get('request_id'), 'error': str(error)})
            self.failed.emit(str(error))

    def set_lane(self, scope, key, duration, *, automatic=False):
        lanes = dict(self.preferences.get(scope + '_lanes', {}))
        lanes[key] = normalize_lane(duration)
        modes = {**self.preferences.get(scope + '_lane_modes', {}), key: 'automatic' if automatic else 'fixed'}
        preferences = {**self.preferences, scope + '_lanes': lanes, scope + '_lane_modes': modes}
        if preferences != self.preferences:
            self.store.save_settings(preferences)
            self.preferences = preferences

    def assign_lane(self, client, job):
        if (automatic_lane(client, self.preferences) and job.get('duration') is not None
                and lane_for(client, self.preferences) != job['duration']):
            scope, key = lane_key(client)
            self.set_lane(scope, key, job['duration'], automatic=True)

    def _tick(self):
        try:
            from model.OfflineMode import is_offline
            if is_offline():
                return
            for socket, client in list(self.clients.items()):
                if not client['authorized'] and time.time() - client['connected'] > 5:
                    socket.close()
                if client.get('page_ready') and time.time() - client.get('page_at', 0) > 45:
                    client['page_ready'] = False
                    client['page_error'] = 'Flow 页面超过 45 秒未响应，请打开浏览器检查或刷新状态'
                if confirm_saved_connection(self, client):
                    self.publish()
            for request_id, request in list(self.pending.items()):
                if time.time() - request['at'] < (120 if request['action'] == 'upload_images' else 60):
                    continue
                self.pending.pop(request_id, None)
                if not request['job'] and request['action'] in {'get_points', 'snapshot'}:
                    for client in self.clients.values():
                        if client.get('id') == request['client']:
                            client['page_error'] = '扩展已连接，但项目页面响应超时；请打开浏览器检查登录和项目'
                            if request['action'] == 'get_points':
                                client['credit_check'] = {'state': 'failed', 'at': time.time(),
                                    'message': '积分检测响应超时，请检查该浏览器的登录和项目页面后重试'}
                if request['job']:
                    row = self.store.get(request['job'])
                    # A disconnected request may outlive an explicit retry.
                    # Its timeout owns only the attempt/browser it was sent to,
                    # just like a late response; never fail a newer submission.
                    if (row and row['attempt'] == request['attempt']
                            and row['browser'] == request['client'] and row['status'] in TRANSIENT):
                        client = next((c for c in self.clients.values() if c.get('authorized') and c.get('id') == request['client']), None)
                        if request['action'] in {'upload_images', 'retry_download'}:
                            self.transfer_failed(row, 'upload' if request['action'] == 'upload_images' else 'download', '浏览器传输响应超时，将自动重试')
                            self.publish()
                            continue
                        if client and request['action'] == 'prepare':
                            self.quarantine(client, row, '浏览器响应超时，请检查页面后恢复；未发送新的生成请求。', status='failed')
                            self.publish()
                            continue
                        status = 'uncertain' if request['action'] in {'run_tasks', 'retry_download'} else 'failed'
                        self.store.update(row['id'], status, error='浏览器响应超时，保留记录。')
                        if client and request['action'] in {'run_tasks', 'retry_download'}:
                            self.request(client, 'snapshot')
                        self.publish()
            if self.watch_stalled():
                self.publish()
            if watch_preparation(self):
                self.publish()
            if self.watch_transfer_retries():
                self.publish()
            from .page_recovery import watch
            if watch(self): self.publish()
            if time.time() - getattr(self, '_last_publish', 0) >= 15:
                self.publish()
            rows = self.store.rows()
            batches = self.store.batches()
            queued_rows = sorted([row for row in rows if row['status'] == 'queued'
                           and row['remote'].get('prepare_retry', {}).get('next_at', 0) <= time.time()
                           and not batch_wait(row['payload'], batches, estimated_cost(row['payload'], self.observed_costs))
                           and not (row['payload'].get('budget') and estimated_cost(row['payload'], self.observed_costs) is not None
                                    and estimated_cost(row['payload'], self.observed_costs) > row['payload']['budget'])
                           and (not self.paused or row['id'] in self.released)],
                           key=lambda row: -row['payload'].get('priority', 0))
            if not queued_rows or self.safety_paused:
                return
            clients = [client for client in self.clients.values() if client['authorized']]
            limit = max(1, min(8, int(self.preferences.get('concurrency', 2))))
            if sum(row['status'] in TRANSIENT for row in rows) >= limit:
                return
            client = queued = None
            # An unavailable 10-second lane must not block a ready 6-second job.
            matches = []
            for index, candidate in enumerate(queued_rows):
                selected = choose_client(clients, rows, limit, candidate['payload'], self.preferences,
                                         estimated_cost(candidate['payload'], self.observed_costs))
                if selected:
                    matches.append((dispatch_rank(selected, candidate['payload'], self.preferences), index, candidate, selected))
            if matches:
                _rank, _index, queued, client = min(matches, key=lambda item: item[:2])
            if client:
                if not self.media_disks_ready(queued):
                    return
                try:
                    unchanged = fingerprint(queued['payload']['image']) == queued['payload']['image_identity']
                except OSError:
                    unchanged = False
                if not unchanged:
                    self.store.update(queued['id'], 'failed', error='参考图片在提交后变化或丢失，请重新添加任务。')
                    self.publish()
                    return
                attempt = uuid.uuid4().hex
                self.assign_lane(client, queued['payload'])
                remote = {'account': client['account'], 'profile': client.get('profile', ''), 'activity_at': time.time()}
                if queued['remote'].get('prepare_retry'):
                    remote['prepare_retry'] = queued['remote']['prepare_retry']
                self.store.update(queued['id'], 'preparing', attempt=attempt, browser=client['id'], remote=remote)
                job = queued['payload']
                self.request(client, 'prepare', {'videoModel': 'omni_1_1_flash', 'duration': job['duration'],
                    'aspectRatio': 'ASPECT_RATIO_' + job['aspect'].replace(':', '_'),
                    'resolution': 'VIDEO_RESOLUTION_' + job['resolution'].upper(), 'footageType': 'VIDEO_FRAMES', 'count': 1}, queued['id'])
                self.publish()
            elif time.time() - self.last_launch > 25:
                profiles = configured_profiles(self.preferences)
                online = {c.get('profile') for c in clients}
                held_profiles = {r['remote'].get('profile') for r in rows if r['status'] in TRANSIENT | {'uncertain', 'download_failed'}}
                available = [p for p in profiles if p not in self.launched and p not in online and p not in held_profiles
                             and not block_for({'profile': p}, self.preferences)]
                ranked = [(dispatch_rank({'profile': profile, 'id': ''}, row['payload'], self.preferences), index, profile)
                          for index, row in enumerate(queued_rows) for profile in available]
                ranked = sorted((rank, index, profile) for rank, index, profile in ranked if rank is not None)
                candidate = ranked[0][2] if ranked else None
                # Busy or unrelated lanes must not prevent starting a useful one.
                held = [r for r in rows if r['status'] in TRANSIENT | {'uncertain', 'download_failed'}]
                busy = {r['remote'].get('account') for r in held}
                useful_accounts = {c['account'] for c in clients if c.get('account') not in busy and any(
                    capable(c, row['payload'], self.preferences, estimated_cost(row['payload'], self.observed_costs)) for row in queued_rows)}
                self.launching = {profile: at for profile, at in self.launching.items() if time.time() - at < 90}
                occupied = sum(r['status'] in TRANSIENT for r in rows) + len(useful_accounts) + len(self.launching)
                if candidate and occupied < limit:
                    target_job = queued_rows[ranked[0][1]]['payload']
                    self.assign_lane({'profile': candidate, 'id': ''}, target_job)
                    self.launched.add(candidate)
                    self.launching[candidate] = time.time()
                    self.last_launch = time.time()
                    self.launch_requested.emit(candidate)
                    self.publish()
        except Exception as error:
            self.paused = True
            self.released.clear()
            self.safety_paused = True
            self.failed.emit('Flow 队列已暂停：' + str(error))

    def publish(self):
        self._last_publish = time.time()
        rows = self.store.rows()
        batches = self.store.batches()
        clients = [{**{key: value for key, value in client.items() if key not in {'socket', 'authorized'}},
                    'duration_lane': lane_for(client, self.preferences),
                    'environment_block': block_for(client, self.preferences)}
                   for client in self.clients.values() if client['authorized']]
        for row in rows:
            row['wait_reason'] = waiting_reason(row, clients, rows, self.preferences, self.observed_costs, batches,
                paused=self.paused, released=self.released)
            detail = retry_detail(row, self.preferences)
            if detail: row['wait_reason'] = detail
        self.changed.emit({'jobs': rows, 'clients': clients, 'paused': self.paused,
                           'pairing': self.pair, 'preferences': self.preferences, 'downloads_root': self.downloads_root,
                           'released': list(self.released), 'safety_paused': self.safety_paused,
                           'drafts': self.drafts, 'batches': batches, 'spaces': self.space_status(rows),
                           'saving': bool(self.archiving)})
        self.running_changed.emit(any(row['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} for row in rows))
