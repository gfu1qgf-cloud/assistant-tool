"""Worker-thread coordinator for authenticated extension sessions and saved jobs."""
from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import math
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
import weakref

from qt_compat import QtCore, QtNetwork
from PyQt6 import sip
from PyQt6.QtWebSockets import QWebSocketServer

from .archive import archive_download
from .store import QueueStore, TRANSIENT, choose_client, fingerprint, make_jobs, submission_risk
from .routing import cost_key, lane_for, lane_key, dispatch_rank, automatic_lane, normalize_lane
from .credit_policy import debit_balance, credit_order
from .credit_cooldown import credit_hold, credit_record, credit_reason, remember_balance, refresh_expired, MIN_CREDITS
from .credit_cache import restore_balance, refresh_needed
from .timing import value as timing_value, normalize as normalize_timing, snapshot as timing_snapshot, request_timeout, validate as validate_timing, timing_changes
from .media_identity import owns_download, generated_bindings, merge_snapshot_bindings
from .operations import FlowOperations
from .policy import capable, block_for, estimated_cost, batch_wait, waiting_reason, admitted_queue
from .setup_support import EXTENSION_ID, EXTENSION_VERSION, OPTIONS_URL, profile_name, project_url, project_identity, project_block, compatible_extension
from .connection_status import configured_profiles, browser_keys
from .pricing import fixed_cost
from .transfer_policy import retry_info, retry_detail, ready_bindings
from .setup_pairing import confirm_saved_connection
from .page_readiness import defer_prepare, watch_preparation, recover_window_block, recover_preparation_block, retry_initial_credits, missing_window, record_duration_limit, note_readiness
from .account_policy import mark_account, reset_accounts, account_hold, park_unsubmitted_job
from .connection_recovery import repair_idle, watch_connections
from .generation_recovery import confirmed_failure, watch as watch_generation_retries, fresh_clients
from .reference_image import verified_receipt
from .balance_dispatch import prefer_smaller, profile_order, can_fund
from .image_affinity import remember_owner, allows_image, retained, affinity_context, cached_profile, dispatch_order, retire_exhausted
from .prepare_snapshot import remember as remember_prepare, reuse as reuse_prepare
from .browser_slots import request_start as start_browser, can_start as can_start_browser, summary as browser_capacity
from .high_credit import allowed as high_credit_allowed
from .browser_options import normalize as normalize_browser_options, snapshot as browser_options_snapshot

DEFAULT_PORT = 18189
PROTOCOL = 1


def socket_alive(socket):
    return socket is not None and (not isinstance(socket, QtCore.QObject) or not sip.isdeleted(socket))


def temporary_database_lock(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return (code & 0xff) in {getattr(sqlite3, 'SQLITE_BUSY', 5), getattr(sqlite3, 'SQLITE_LOCKED', 6)}
    return str(error).split(':', 1)[0].strip().casefold() in {
        'database is locked', 'database table is locked', 'database schema is locked', 'database is busy'}


class FlowService(QtCore.QObject, FlowOperations):
    changed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    launch_requested = QtCore.pyqtSignal(str)
    project_open_requested = QtCore.pyqtSignal(object)
    high_credit_confirmation_requested = QtCore.pyqtSignal(object)
    running_changed = QtCore.pyqtSignal(bool)
    finished_job = QtCore.pyqtSignal(str)
    submitted = QtCore.pyqtSignal(object)
    submission_failed = QtCore.pyqtSignal(object)
    setup_ready = QtCore.pyqtSignal(object)
    records_deleted = QtCore.pyqtSignal(object)
    tasks_reset = QtCore.pyqtSignal(object)
    preferences_saved = QtCore.pyqtSignal(object)
    stopped = QtCore.pyqtSignal()
    archive_result = QtCore.pyqtSignal(object)

    def __init__(self, namespace, downloads_root, *, program_timing=None, program_browser_options=None):
        super().__init__()
        self.namespace, self.downloads_root = namespace, downloads_root
        self.program_timing = None if program_timing is None else validate_timing({'timing': normalize_timing(program_timing)})
        self.program_browser_options = None if program_browser_options is None else normalize_browser_options(program_browser_options)
        self.clients, self.pending = {}, {}
        self.browser_snapshot = []
        self.paused, self.closing, self.last_launch = True, False, 0
        self.launched, self.archiving = set(), set()
        self.launching = {}
        self.launch_attempts, self.small_balance_probes = set(), {}
        self._probe_batches, self._database_lock_reported = None, False
        # Explicit per-item releases are session-local: restarting never submits
        # a paid request until the user resumes it. The jobs themselves persist.
        self.released, self.observed_costs = set(), {}
        self.safety_paused = False
        self.drafts = {}
        self.setup_requests = {}
        self.credit_debits = {}
        self.connection_recoveries = {}
        self.project_startup_session = uuid.uuid4().hex
        self.high_credit_pending = None
        self.archive_result.connect(self._archived)

    @QtCore.pyqtSlot()
    def start(self):
        try:
            self.store = QueueStore(self.namespace)
            self.preferences = self.store.settings()
            from .browser_slots import recover_legacy_probe_failures
            self.preferences = recover_legacy_probe_failures(self.preferences)
            # Consent from a previous application process never authorizes a new run.
            from .high_credit import new_session_entries
            entries = new_session_entries(self.preferences)
            self.commit_preferences({**self.preferences, 'high_credit_session': uuid.uuid4().hex,
                                      'high_credit_accounts': entries})
            if self.program_timing is not None:
                self.commit_preferences({**self.preferences, **timing_changes(self.preferences, self.program_timing)})
            if self.program_browser_options is not None:
                self.commit_preferences({**self.preferences, 'browser_options': self.program_browser_options})
            self.store.recover()
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
            self.timer.start(max(1, round(timing_value(self.preferences, 'queue_tick_seconds') * 1000)))
            self.publish()
        except Exception as error:
            self.failed.emit(str(error))

    def send(self, client, message):
        socket = client.get('socket')
        if client.get('retired') or not socket_alive(socket):
            self._disconnected(socket)
            return False
        try:
            sent = socket.sendTextMessage(json.dumps(message, ensure_ascii=False))
        except RuntimeError:
            if socket_alive(socket):
                raise
            self._disconnected(socket)
            return False
        if type(sent) is int and sent <= 0:
            self._close_socket(socket)
            self._disconnected(socket)
            return False
        return True

    def _close_socket(self, socket):
        if not socket_alive(socket):
            self._disconnected(socket)
            return
        try:
            socket.close()
        except RuntimeError:
            if socket_alive(socket):
                raise
            self._disconnected(socket)

    def request(self, client, action, params=None, job=None, *, manual_credit=False, manual_close=False):
        if action == 'close_exhausted':
            return False  # Automatic closure was disabled; never send a native close.
        if action == 'close_browser' and not manual_close:
            return False  # Only the explicit runtime menu may request closure.
        if action != 'close_browser' and client.get('runtime_close', {}).get('state') in {'checking', 'closing', 'closed'}:
            return False
        if action == 'refresh_project':
            # A reload can cancel an accepted website generation. Even legacy
            # recovery plans may only query snapshots; never send this command.
            return
        if action == 'get_points':
            if manual_credit: client.pop('reuse_recovery',None)
            previous = next((p for p in self.pending.values() if p['action'] == action and p['client'] == client['id']), None)
            if previous:
                # A click authorizes a NEW observation, not an automatic probe
                # that may already contain a pre-debit or pre-reset balance.
                if manual_credit and not previous.get('manual_credit'):
                    client['manual_credit_pending'] = True
                return
            if not manual_credit and credit_hold(client, self.preferences):
                client.update(credit_cooldown_waiting=True, credits_waiting_ready=False,
                              credits_waiting_account=False, credits_retry_at=0)
                if (client.get('credit_check') or {}).get('state') != 'success':
                    client['credit_check'] = {'state': 'cooldown', 'at': time.time(),
                        'message': credit_reason(client, self.preferences)}
                self.launching.pop(client.get('profile'), None)
                self.launched.discard(client.get('profile'))
                return False
        request_id = uuid.uuid4().hex
        row = self.store.get(job) if job else None
        self.pending[request_id] = {'action': action, 'job': job, 'client': client['id'], 'at': time.time(),
                                    'attempt': row['attempt'] if row else ''}
        if action == 'get_points':
            self.pending[request_id]['credit_debits'] = dict(self.credit_debits)
            self.pending[request_id]['manual_credit'] = manual_credit
            client['credit_last_request_observed_at'] = client.get('credits_at', 0)
            client['credit_check'] = {'state': 'checking', 'at': time.time(), 'message': '正在读取本浏览器当前账号和积分'}
            if manual_credit:
                client['credit_check']['hold_dispatch'] = True
                client.pop('credit_close', None)
        params = dict(params or {})
        if action in {'prepare', 'upload_images', 'run_tasks'}:
            params['wait_for_ready'] = True
        from .foreground import UI_ACTIONS
        foreground = {}
        if (action in UI_ACTIONS and not (action == 'retry_download' and params.get('restore')) and
                (action == 'focus_window' or browser_options_snapshot(self.preferences)['focus_before_action']) and
                'native_foreground_v1' in client.get('capabilities', [])):
            foreground['foreground_token'] = uuid.uuid4().hex
            self.pending[request_id].update(foreground)
        if not self.send(client, {'action': action, 'request_id': request_id, 'params': params,
                **foreground,
                'browser_options': browser_options_snapshot(self.preferences)}):
            self.pending.pop(request_id, None)
            return False
        return True

    def _accept(self):
        socket = self.server.nextPendingConnection()
        if not socket_alive(socket):
            return
        # Keep the C++ connection owned by this worker until retirement. A
        # Python reference alone does not guarantee a live Qt wrapper.
        socket.setParent(self)
        if socket.origin() != f'chrome-extension://{EXTENSION_ID}':
            socket.abort()
            if socket_alive(socket): socket.deleteLater()
            return
        socket.setMaxAllowedIncomingMessageSize(1024 * 1024)
        socket.setMaxAllowedIncomingFrameSize(1024 * 1024)
        self.clients[socket] = {'socket': socket, 'authorized': False, 'connected': time.time()}
        # Qt can emit destruction signals while cyclic Python closures are
        # being cleared. Do not retain/call a retired coordinator via a cell.
        owner = weakref.ref(self)
        def received(text, s=socket, owner=owner):
            coordinator = owner()
            if socket_alive(coordinator): coordinator._message(s, text)
        def retired(_object=None, s=socket, owner=owner):
            coordinator = owner()
            if socket_alive(coordinator): coordinator._disconnected(s)
        socket.textMessageReceived.connect(received)
        socket.disconnected.connect(retired)
        socket.destroyed.connect(retired)

    def _disconnected(self, socket):
        client = self.clients.pop(socket, None)
        if client is None:
            return
        client['retired'] = True
        client['authorized'] = False
        if self.closing:
            if socket_alive(socket): socket.deleteLater()
            return
        if client.get('id') and any(c.get('authorized') and c.get('id') == client['id'] for c in self.clients.values()):
            # A late disconnect from the replaced socket does not own the
            # new connection's attempt, even though the browser ID is stable.
            if socket_alive(socket): socket.deleteLater()
            return
        from .browser_slots import disconnected as release_disconnected_slot
        release_disconnected_slot(self, client)
        for row in self.store.rows():
            if row['browser'] == client.get('id') and row['status'] in TRANSIENT:
                if row['status'] in {'uploading', 'downloading'}:
                    self.transfer_failed(row, 'upload' if row['status'] == 'uploading' else 'download', '原浏览器断开，重连后自动恢复传输')
                    continue
                status = 'preparing' if row['status'] == 'preparing' and not submission_risk(row, self.store.attempt_charged(row['attempt'])) else 'uncertain'
                self.store.update(row['id'], status, error='浏览器断开；已提交的任务会先核对，不自动重复生成。')
        for request_id, request in list(self.pending.items()):
            if request['client'] == client.get('id'):
                if request['action'] == 'close_browser' and client.get('runtime_close', {}).get('state') != 'closed':
                    from .runtime_close import timeout as close_timeout
                    close_timeout(client, self)
                self.pending.pop(request_id, None)
        if socket_alive(socket): socket.deleteLater()
        if not self.closing:
            self.publish()

    def _message(self, socket, text):
        client = self.clients.get(socket)
        if not client:
            return
        if not socket_alive(socket):
            self._disconnected(socket)
            return
        try:
            if len(text) > 1024 * 1024:
                raise ValueError('消息过大')
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ValueError('消息格式无效')
            if not client['authorized']:
                if message.get('type') == 'window_absence':
                    from .browser_slots import confirm_absence
                    if confirm_absence(self, message): self.publish()
                    self._close_socket(socket)
                    return
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
                    self._close_socket(socket)
                    return
                browser_id = str(message.get('browser_id') or '')
                if not browser_id or len(browser_id) > 100:
                    raise ValueError('浏览器标识无效')
                client.update(authorized=True, id=browser_id, profile=str(message.get('profile') or '')[:100],
                              project=message.get('project') or '', saved_project=message.get('project') or '',
                              extension_version=str(message.get('extension_version') or '')[:30],
                              setup_id=str(message.get('setup_id') or '')[:32],
                              capabilities=[c for c in message.get('capabilities', []) if isinstance(c, str)][:20],
                              page_ready=False, page_at=0, page_error='',
                              account='', credits=None, credits_at=0, blocked='')
                # Register the replacement first: close() may synchronously
                # deliver the old socket's disconnect signal.
                for other, info in list(self.clients.items()):
                    if other is not socket and info.get('id') == browser_id:
                        info['authorized'] = False
                        self._close_socket(other)
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
                from .browser_slots import seen as browser_seen
                browser_seen(self, client)
                # A WebSocket hello is not a ready browser. Keep its startup
                # slot until both the page and initial account probe finish.
                # Restore the held balance as well: it is needed to retire an
                # idle cooled-down profile. The hold still forbids all spending
                # and automatic balance probes; this never proves a new login.
                cached = restore_balance(client, self.preferences)
                if credit_record(client, self.preferences): cached = False
                if not self.send(client, {'type': 'welcome', 'protocol': PROTOCOL, 'timing': timing_snapshot(self.preferences),
                        'browser_options': browser_options_snapshot(self.preferences)}):
                    return
                if not cached and not account_hold(client, self.preferences):
                    self.request(client, 'get_points')
                self.request(client, 'snapshot')
                self.recover_extension_downloads(client)
                self.publish()
                return
            if message.get('type') == 'ping':
                self.send(client, {'type': 'pong'})
                return
            if message.get('type') == 'windows_closed':
                if ('window_inventory_v1' not in client.get('capabilities', []) or
                        message.get('profile') != client.get('profile') or
                        project_identity(message.get('project')) != project_identity(client.get('saved_project'))):
                    return
                client['credit_close'] = {**client.get('credit_close', {}), 'state': 'closed',
                    'message': '已确认此配置所有浏览器窗口退出，启动名额已释放'}
                for key, pending in list(self.pending.items()):
                    if pending['client'] == client['id'] and pending['action'] in {'close_exhausted', 'close_browser'}:
                        self.pending.pop(key)
                if client.get('runtime_close'):
                    from .runtime_close import response as close_response
                    close_response(self, client, {'success': True, 'data': {'closed': True}})
                from .browser_slots import closed as browser_closed
                browser_closed(self, client.get('profile'))
                self.publish()
                return
            if message.get('type') in {'foreground_request', 'foreground_cancel'}:
                from .foreground import receive
                receive(self, client, message)
            elif message.get('type') == 'download':
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
                client['page_instance'] = str(message.get('page_instance') or '')[:100]
                client['project'] = message.get('url') or ''
                client['note'] = message.get('note') or ''
                client['page_loaded'] = bool(project_identity(client['project']))
                client['page_ready'] = client['page_loaded'] and message.get('controls_ready', True) is True
                from .project_startup import ready as project_ready
                project_ready(self, client)
                note_readiness(client)
                client['ready_note'] = str(message.get('ready_note') or '')[:300]
                client['page_at'] = time.time()
                client['page_error'] = ''
                client['setup_blocked'] = project_block(client, self.preferences)
                if client.get('page_ready') and client.get('credits_local'):
                    self.launching.pop(client.get('profile'), None)
                confirm_saved_connection(self, client)
                recover_window_block(self, client)
                retry_initial_credits(self, client)
            elif message.get('type') == 'generation_failed':
                confirmed_failure(self, client, message)
                if message.get('event_id'):
                    self.send(client, {'type': 'ack', 'event_id': message['event_id']})
            elif message.get('type') == 'site_pause':
                if 'site_pause_evidence_v1' in client.get('capabilities', []) and (
                        message.get('source') != 'visible_site_alert' or message.get('code') != 'FLOW_SITE_UNUSUAL_ACTIVITY' or
                        not project_identity(message.get('url')) or
                        project_identity(message.get('url')) != project_identity(client.get('saved_project')) or
                        not message.get('page_instance') or client.get('page_instance') and
                        message['page_instance'] != client['page_instance']):
                    return
                self.paused = self.safety_paused = True
                self.released.clear()
                client['blocked'] = 'Flow 网站提示安全限制，后续提交已暂停；保留当前生成页面，不自动刷新。'
            elif message.get('type') == 'account_issue':
                if (project_identity(client.get('saved_project')) and
                        project_identity(message.get('url')) == project_identity(client.get('saved_project'))):
                    mark_account(self, client, message.get('error', ''), message.get('code', ''))
            elif message.get('type') in {'hosted_task_status', 'hosted_batch_finished'}:
                self._status(client, message)
            else:
                self._response(client, message)
            self.publish()
        except Exception as error:
            self.failed.emit('Flow 消息处理失败：' + str(error))

    def _response(self, client, message):
        request_id = str(message.get('request_id'))
        request = self.pending.get(request_id)
        if not request or request['client'] != client['id']:
            return
        if request['action'] == 'close_browser':
            from .runtime_close import response as close_response
            if close_response(self, client, message): self.pending.pop(request_id, None)
            return
        if (request['action'] == 'close_exhausted' and message.get('success') and
                (message.get('data') or {}).get('closing') and not (message.get('data') or {}).get('closed')):
            client['credit_close'] = {**client.get('credit_close', {}), 'state': 'closing',
                'message': '下载已核对完成，正在关闭此浏览器；窗口退出前继续占用启动名额'}
            return
        self.pending.pop(request_id)
        if request.get('retired'):
            if request['action'] in {'prepare', 'upload_images'}:
                self.store.finish_reset_hold(request['attempt'], request['client'])
                self.refresh_reset_holds()
            return
        action, job_id = request['action'], request['job']
        data = message.get('data') or {}
        if action == 'close_exhausted':
            state = dict(client.get('credit_close') or {})
            busy = message.get('code') == 'FLOW_CLOSE_BUSY'
            state.update(state='closed' if message.get('success') and data.get('closed') else 'waiting' if busy else 'failed',
                next_at=time.time() + timing_value(self.preferences, 'credit_close_retry_seconds'),
                count=max(0, state.get('count', 1) - int(busy)),
                message='下载已核对完成，正在关闭此浏览器' if message.get('success') else
                    str(message.get('error') or '浏览器关闭未完成')[:500])
            client['credit_close'] = state
            if state['state'] == 'closed':
                from .browser_slots import closed as browser_closed
                browser_closed(self, client.get('profile'))
            return
        if action == 'repair_project':
            client['runtime_note'] = ('F5／页面恢复已完成，等待重新连接和积分确认。' if message.get('success') else
                'F5／页面恢复未执行：'+str(message.get('error') or '扩展未确认成功'))
            current = dict(client.get('connection_recovery') or {})
            current.update(state='waiting' if message.get('success') else 'failed',
                message='页面已恢复，正在重新确认连接和积分' if message.get('success') else
                    str(message.get('error') or '页面修复未完成')[:500])
            client['connection_recovery'] = current
            if message.get('code') == 'FLOW_PAGE_REPAIR_BUSY':
                client['batch_running'] = True
            if message.get('success'):
                client.update(credits_ready_failures=0, credits_waiting_ready=True,
                    credits_waiting_account=False, credits_retry_at=time.time() + timing_value(self.preferences, 'credit_after_repair_seconds'))
            else:
                from .project_startup import note_missing
                note_missing(self, client, message.get('error', ''), message.get('code', ''))
            self.request(client, 'snapshot')
            return
        if action == 'focus_window':
            client['runtime_note'] = ('窗口激活操作已返回；'+str((message.get('data') or {}).get('state') or '') if message.get('success') else
                '窗口激活失败：'+str(message.get('error') or '扩展未确认成功'))
            return
        if action == 'get_points':
            if client.pop('manual_credit_pending', False):
                self.request(client, 'get_points', manual_credit=True)
                return
            if request['at'] < client.get('account_reset_at', 0):
                self.request(client, 'get_points', manual_credit=True)
                client['credit_check']['hold_dispatch'] = True
                return
            client['credits_retry_manual'] = bool(request.get('manual_credit'))
            keep_balance = (bool(client.get('account')) and client.get('credits_local') is True
                            and type(client.get('credits')) in (int, float) and math.isfinite(client['credits']))
            if not message.get('success', True):
                mark_account(self, client, message.get('error', ''), message.get('code', ''))
                if keep_balance: client.update(identity_unconfirmed=True, credits_cached=True)
            client['credit_check'] = {'state': 'failed', 'at': time.time(),
                'message': str(message.get('error') or ('尚未确认当前账号邮箱' if not data.get('email') else '页面未返回有效积分'))[:500]}
            if message.get('code') == 'FLOW_ACCOUNT_NOT_READY' or (message.get('success') and not data.get('email')):
                failures = client.get('credits_ready_failures', 0) + 1
                if not keep_balance: client.update(account='', credits=None, credits_local=False)
                limit = timing_value(self.preferences, 'credit_read_attempts')
                client.update(identity_unconfirmed=True, credits_waiting_account=True,
                    credits_waiting_ready=failures < limit, credits_ready_failures=failures,
                    credits_retry_at=time.time() + timing_value(self.preferences, 'credit_retry_seconds'),
                    note=('账号菜单未返回可确认的邮箱，保留上次余额并重试读取' if failures < limit else
                          f'账号读取连续{limit}次未完成；上次余额已保留，请检查登录后手动检测'))
                return
            if not message.get('success', True) and (message.get('code') == 'FLOW_PAGE_NOT_READY' or missing_window(message.get('error', ''))):
                client.update(page_ready=False, credits_waiting_ready=True, credits_waiting_account=False,
                    credits_ready_failures=client.get('credits_ready_failures', 0)+1,
                    page_error='页面尚未稳定，控件就绪后自动重试本次首次积分读取')
                return
            if not message.get('success', True):
                if not keep_balance: client.update(credits=None, credits_local=False)
                client.update(credits_waiting_ready=False,
                    credits_waiting_account=False, credits_retry_at=0,
                    page_error=str(message.get('error') or '账号和积分读取失败')[:500])
                return
            client['credits_waiting_ready'] = False
            client['credits_waiting_account'] = False
            client['credits_retry_at'] = 0
            if message.get('success', True) and data.get('email'):
                client['credits_ready_failures'] = 0
            account = str(data.get('email') or '').strip().casefold()
            points = data.get('remaining_points')
            if (not isinstance(points, int) or isinstance(points, bool) or points < 0) and keep_balance and account == client['account']:
                client['credit_check']['message'] = '本次未返回有效积分，保留上次成功余额；可再次手动检测'
                return
            client['account'] = account
            client['credits'] = points if isinstance(points, int) and not isinstance(points, bool) and points >= 0 else None
            client['credits_at'] = time.time()
            client['credits_local'] = bool(client['account']) and type(client['credits']) is int
            client['credits_estimated'] = False
            if client['credits_local']:
                from .credit_refresh import observe as observe_credit_refresh
                observed = observe_credit_refresh(self, client, data, started_at=request['at'])
                if observed is not None: client['credits_at'] = min(client['credits_at'], observed)
                from .high_credit import with_marker
                marked = with_marker(self.preferences, client)
                if marked != self.preferences: self.commit_preferences(marked)
                # A slow observation may have started before a new submission.
                # Do not let its old balance undo an intervening local debit.
                spent = self.credit_debits.get(client['account'], 0) - request.get('credit_debits', {}).get(client['account'], 0)
                balance = max(0, client['credits'] - max(0, spent))
                hold = credit_hold(client, self.preferences)
                if hold and not request.get('manual_credit'):
                    balance = min(balance, hold['balance'])
                for current in self.clients.values():
                    if str(current.get('account') or '').casefold() == client['account']:
                        current.update(credits=balance, credits_local=True, credits_at=client['credits_at'],
                                       credits_estimated=spent > 0, credits_cached=False, identity_unconfirmed=False)
                        current.pop('credit_last_request_observed_at', None)
                        self.small_balance_probes.pop(current.get('profile'), None)
                remember_balance(self, client, manual=bool(request.get('manual_credit')))
                client['credits_retry_manual'] = False
            client['note'] = message.get('error') or ('' if client['account'] else '尚未确认当前 Flow 登录账号')
            if message.get('success', True) and client['credits_local']:
                if client.get('page_ready'): self.launching.pop(client.get('profile'), None)
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
        if action == 'prepare' and row['status'] != 'preparing':
            return
        if action == 'prepare' and message.get('success'):
            from .high_credit import submission_allowed
            if not submission_allowed(self, client, row):
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error='千分账号尚未获准或小额账号已可用，重新调度；未提交生成。')
                return
        if action == 'upload_images' and row['status'] != 'uploading':
            return
        if action == 'run_tasks' and (row['status'] not in {'submitting', 'generating', 'uncertain'}
                or row['remote'].get('page_recovery') or row['remote'].get('watch_attention')):
            return
        if not message.get('success'):
            if action in {'prepare', 'run_tasks'}:
                client.pop('prepare_snapshot', None)
            isolated = mark_account(self, client, message.get('error', ''), message.get('code', ''))
            if isolated and action in {'prepare', 'upload_images'}:
                park_unsubmitted_job(self, row)
                return
            if action == 'refresh_project':
                recovery = {**row['remote'].get('page_recovery', {}), 'done': True, 'next_at': 0}
                self.store.update(job_id, 'uncertain', remote={**row['remote'], 'page_recovery': recovery},
                    error='自动刷新未执行：'+str(message.get('error') or '原页面未响应')+'；未重新生成。')
                return
            if action == 'prepare':
                client.pop('prepare_snapshot', None)
                if message.get('code') == 'FLOW_DURATION_UNSUPPORTED':
                    if record_duration_limit(self, client, row, message.get('error') or '当前时长不可用', data):
                        return
                    defer_prepare(self, client, row, '时长选项尚未稳定，等待后重新核对；未提交生成。', 'FLOW_SETTINGS_NOT_READY')
                    return
                if defer_prepare(self, client, row, message.get('error') or '', message.get('code', '')):
                    return
                self.quarantine(client, row, str(message.get('error') or '页面参数核对失败'),
                    recovery_code='FLOW_SETTINGS_MISMATCH' if message.get('code') == 'FLOW_SETTINGS_MISMATCH' else '')
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
        if action in {'prepare', 'upload_images'} and account_hold(client, self.preferences):
            park_unsubmitted_job(self, row)
            return
        if action == 'prepare':
            if not request.get('snapshot_reuse'):
                client.pop('prepare_snapshot', None)
            cost, budget = data.get('cost'), row['payload']['budget']
            rule = fixed_cost(row['payload'])
            cost_source = 'duration_rule' if rule is not None else 'page'
            if rule is not None: cost = rule
            if not data.get('verified') or data.get('model') != 'omni_1_1_flash':
                self.quarantine(client, row, '页面未确认 Omni 参数，未提交生成。', recovery_code='FLOW_SETTINGS_MISMATCH')
                return
            if ('email' in data and str(data['email']).strip().casefold() != client['account']
                    or row['remote'].get('account') and row['remote']['account'] != client['account']):
                self.quarantine(client, row, '核对参数时登录账号发生变化或无法确认，请刷新账号并恢复。')
                return
            client.pop('prepare_wait', None)
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
            if client['credits'] is None or client['credits'] < MIN_CREDITS or cost > client['credits'] or credit_hold(client, self.preferences):
                self.store.update(job_id, 'queued', attempt='', browser='', remote={}, error='等待有足够积分的账号')
                return
            remote = {key: value for key, value in row['remote'].items() if key != 'prepare_retry'}
            remote.update(cost=cost, cost_source=cost_source)
            if not request.get('snapshot_reuse'):
                remember_prepare(self, client, row, data)
            self.store.update(job_id, 'uploading', remote=remote)
            self.start_upload(self.store.get(job_id))
        elif action == 'upload_images':
            reference = row['remote'].get('upload_reference')
            if not verified_receipt(reference, data.get('reference')) or reference['upload_id'] != row['attempt']:
                self.transfer_failed(row, 'upload', '扩展未返回本次参考图的准确首帧凭据；未提交生成。请在浏览器空闲时加载当前桥接扩展后完全重置此任务。', permanent=True)
                return
            self.store.update(job_id, row['status'], remote={**row['remote'], 'reference_receipt': data['reference']}, error=row['error'])
            row = self.store.get(job_id)
            info = {**retry_info(row, 'upload'), 'active': False, 'next_at': 0, 'waiting': '', 'done': True}
            self._save_transfer(row, 'upload', info)
            row = self.store.get(job_id)
            if not self.media_disks_ready(row):
                return
            if row['remote'].get('account') and row['remote']['account'] != client['account']:
                self.quarantine(client, row, '上传期间登录账号变化，请刷新账号并重新核对。')
                return
            from .high_credit import submission_allowed
            if not submission_allowed(self, client, row):
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error='千分账号尚未获准或小额账号已可用，已跳过；图片已上传但未提交生成。')
                return
            if client.get('credits') is None or client['credits'] < MIN_CREDITS or row['remote']['cost'] > client['credits'] or credit_hold(client, self.preferences):
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error='图片上传完成，但原账号积分不足；等待备用账号，未提交生成。')
                return
            if not self.store.reserve(job_id, row['attempt'], row['remote']['cost'], row['remote']['cost_source']):
                self.store.update(job_id, 'queued', attempt='', browser='', remote={},
                    error='已达到整批积分上限，停止新增提交；可调整该批上限后继续。')
                return
            self.store.update(job_id, 'uploading', remote={**row['remote'], 'activity_at': time.time()})
            self.store.update(job_id, 'submitting')  # Commit before sending any generation request.
            from .high_credit import note_normal_spend
            note_normal_spend(self, client)
            debit_balance(client, self.clients.values(), row['remote']['cost'])
            account_key = client['account'].casefold()
            self.credit_debits[account_key] = self.credit_debits.get(account_key, 0) + row['remote']['cost']
            remember_balance(self, client, source='local_reservation')
            job = row['payload']
            params = {'batch_id': row['attempt'], 'clear_existing': True, 'expected_account': client['account'], 'tasks': [{
                'prompt': ' '.join(job['prompt'].split()), 'local_image_path': reference['path'],
                'image_name': reference['filename'], 'reference': data['reference'], 'download_path': self.download_name(row),
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
            client['page_instance'] = str(data.get('page_instance') or '')[:100]
            if (type(data.get('running')) is bool and
                    project_identity(data['url']) == project_identity(client.get('saved_project'))):
                client['batch_running'] = data['running']
                client['native_busy'] = data.get('native_busy') is True
            client.update(project=data['url'], page_loaded=True,
                page_ready=data.get('controls_ready', True) is True, ready_note=str(data.get('ready_note') or '')[:300],
                page_visibility=str(data.get('page_visibility') or 'unknown')[:20], page_focused=data.get('page_focused') is True,
                page_at=time.time(), page_error='')
            note_readiness(client)
            client['setup_blocked'] = project_block(client, self.preferences)
            from .project_startup import ready as project_ready
            project_ready(self, client)
            if client.get('page_ready') and client.get('credits_local'):
                self.launching.pop(client.get('profile'), None)
            confirm_saved_connection(self, client)
            recover_window_block(self, client)
            if data.get('running') is False:
                recover_preparation_block(self, client)
            retry_initial_credits(self, client)
        client['active_batch'] = str(data.get('batch_id') or '')
        batch = str(data.get('batch_id') or '')
        row = next((r for r in self.store.rows() if r['attempt'] == batch and r['browser'] == client['id']), None)
        if not row or row['status'] in {'completed', 'cancelled', 'archiving', 'archive_failed', 'needs_review'} or (
                row['status'] == 'download_parked' and not row['remote'].get('download_skipped')):
            return
        if row['remote'].get('generation_retry', {}).get('failed_attempt') == row['attempt']:
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
        identity = lambda b: (b['taskId'], b['submissionId'])
        previous_ids = {identity(b): set(b['mediaIds']) for b in previous}
        if (row['remote'].get('upstream_task') and task['taskId'] != row['remote']['upstream_task'] or
                previous and incoming and ({identity(b) for b in previous} != {identity(b) for b in incoming}
                    or any(previous_ids.get(identity(b)) and b['mediaIds'] and
                           previous_ids[identity(b)] != set(b['mediaIds']) for b in incoming))):
            self.store.update(row['id'], 'download_parked' if row['status'] == 'download_parked' else 'uncertain',
                error='原任务的媒体归属发生变化，已停止接收替代结果；保留原绑定，不会重新生成。')
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
        if row['status'] == 'download_parked':
            status = 'download_parked'
        elif row['status'] == 'downloading' and retry_info(row, 'download').get('active'):
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
            self.store.update(row['id'], status, remote=remote,
                error=row['error'] if status in {'uncertain', 'download_failed', 'download_parked'} else '')
        self.resume_download_binding(self.store.get(row['id']))
        pending = remote.get('unverified_download')
        if pending and owns_download(self.store.get(row['id']), pending):
            self._download(client, {'batch_id': row['attempt'], 'event_id': remote.get('unverified_download_event'), 'data': pending})

    def _download(self, client, message):
        data = message.get('data') or {}
        batch = str(message.get('batch_id') or '')
        row = next((r for r in self.store.rows() if r['attempt'] == batch and r['browser'] == client['id']), None)
        if not row:
            if self.store.was_deleted(batch, client['id']):
                # Drain late notifications for deleted or explicitly reset attempts;
                # never recreate a job or archive its files behind the user's back.
                self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            return
        if row['status'] in {'completed', 'needs_review', 'cancelled', 'archiving'}:
            self.send(client, {'type': 'ack', 'event_id': message.get('event_id')})
            return
        if data.get('download_path') != self.download_name(row) or not data.get('mediaId') or not data.get('submissionId'):
            raise ValueError('下载归属标识不完整')
        if not owns_download(row, data):
            self.store.update(row['id'], 'download_parked' if row['status'] == 'download_parked' else 'uncertain',
                remote={**row['remote'], 'unverified_download': data,
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
        if row['status'] == 'download_parked' and data.get('state') != 'complete':
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
        if not row or row['status'] in {'completed', 'cancelled', 'archiving', 'archive_failed', 'download_parked', 'needs_review'}:
            return
        if row['remote'].get('generation_retry', {}).get('failed_attempt') == row['attempt']:
            return
        if row['status'] == 'downloading' and retry_info(row, 'download').get('active'):
            return
        if message.get('status') == 'failed' or (message.get('type') == 'hosted_batch_finished' and any(r.get('status') == 'failed' for r in message.get('results', []))):
            text = str(message.get('message') or next((r.get('message') for r in message.get('results', []) if r.get('message')), '生成未完成'))
            mark_account(self, client, text, message.get('code', ''))
            if (account_hold(client, self.preferences).get('kind') not in {'disabled', 'manual'} and
                    any(word in text.casefold() for word in ('captcha', 'unusual', '验证码', '异常活动', 'risk', '风控'))):
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
        try:
            future = self.pool.submit(archive_package, row, self.downloads_root,
                                      cleanup=self.preferences.get('cleanup_downloads', False))
        except Exception as error:
            self.archiving.discard(job_id)
            self.store.update(job_id, 'archive_failed', error='归档工作线程未能启动：' + str(error))
            return
        def completed(result):
            try:
                package = result.result()
                value = {'id': job_id, 'attempt': row['attempt'], **package}
            except Exception as error:
                value = {'id': job_id, 'attempt': row['attempt'], 'error': str(error)}
            self.archive_result.emit(value)
        future.add_done_callback(completed)

    @QtCore.pyqtSlot(object)
    def _archived(self, result):
        self.archiving.discard(result['id'])
        row = self.store.get(result['id'])
        if not row:
            if not self.closing: self.publish()
            return
        if result.get('attempt', row['attempt']) != row['attempt']:
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
                self.browser_snapshot = []
            elif action == 'pause':
                self.paused = True
                self.released.clear()
            elif action == 'browser_launch_result':
                from .browser_slots import launch_result
                launch_result(self, command)
            elif action == 'browser_runtime':
                from .runtime_controls import execute as runtime_execute
                runtime_execute(self, command)
            elif action == 'close_low_credit_browsers':
                from .runtime_close import close_all_low
                close_all_low(self)
            elif action == 'project_open_result':
                from .project_startup import result
                result(self, command)
            elif action == 'high_credit_decision':
                from .high_credit import decide
                decide(self, command)
            elif action == 'high_credit_prompt_shown':
                from .high_credit import prompt_shown
                prompt_shown(self, command)
            elif action == 'retry_high_credit':
                from .high_credit import retry_selected
                retry_selected(self, command.get('keys', []))
            elif action == 'set_high_credit_permission':
                from .high_credit import set_permission
                set_permission(self, command)
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
                if command.get('automatic'):
                    current = [c for c in self.clients.values() if c.get('authorized') and c.get('profile') == profile]
                    accounts = {str(c.get('account') or '').strip().casefold() for c in current} - {''}
                    cooldown = credit_reason({'profile': profile}, self.preferences) or next((
                        credit_reason(c, self.preferences) for c in current if credit_hold(c, self.preferences)), '')
                    if cooldown:
                        self.setup_ready.emit({'request_id': command.get('request_id'), 'success': False,
                            'account_blocked': True, 'error': cooldown})
                        return
                    if account_hold({'profile': profile}, self.preferences) or any(account_hold(c, self.preferences) for c in current):
                        self.setup_ready.emit({'request_id': command.get('request_id'), 'success': False,
                            'account_blocked': True, 'error': '此账号已隔离，等待用户处理后手动重置；停止自动配置。'})
                        return
                    held = any(r['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} and
                        (r['remote'].get('profile') == profile or r['browser'] in {c['id'] for c in current} or
                         str(r['remote'].get('account') or '').strip().casefold() in accounts) for r in self.store.rows())
                    if held or any(c.get('batch_running') for c in current):
                        self.setup_ready.emit({'request_id': command.get('request_id'), 'success': False,
                            'deferred': True, 'error': '账号仍在执行／核对原任务，空闲后自动重试连接。'})
                        return
                    saved = self.preferences.get('profile_projects', {}).get(profile)
                    if saved and project_identity(saved) != project_identity(project):
                        raise ValueError('项目地址已经变化，停止旧连接的自动重试，请确认当前配置。')
                if not command.get('force_pair'):
                    for current in self.clients.values():
                        if current.get('profile') == profile:
                            confirm_saved_connection(self, current)
                existing = next((c for c in self.clients.values() if c.get('authorized') and
                    c.get('profile') == profile and not c.get('setup_pending') and
                    project_identity(c.get('saved_project')) == project_identity(project)), None)
                if existing and not compatible_extension(existing.get('extension_version')):
                    if existing.get('batch_running') or any((r['status'] in TRANSIENT | {'archiving', 'uncertain'} or
                            r['status'] == 'download_failed' and not r['remote'].get('cloud_finished')) and
                           (r['browser'] == existing['id'] or r['remote'].get('profile') == profile) for r in self.store.rows()):
                        raise ValueError('此配置仍在生成／传输或结果未确认，暂不更新扩展和刷新页面；原任务保持不动。')
                    ticket = uuid.uuid4().hex
                    self.setup_requests[profile] = {'id': ticket, 'request': command.get('request_id'), 'project': project,
                        'expires': time.time()+timing_value(self.preferences, 'setup_ticket_seconds'), 'mode': 'reload_only'}
                    entry = f'{OPTIONS_URL}#setup={ticket}&port={self.pair["port"]}'
                    project_entry = f'{project}#lzx_setup={ticket}&lzx_port={self.pair["port"]}&lzx_action=options'
                    self.setup_ready.emit({'request_id': command.get('request_id'), 'success': True,
                        'profile': profile, 'project': project, 'claim_url': entry, 'project_claim_url': project_entry,
                        'pairing': {**self.pair, 'profile': profile, 'project': project, 'setup_id': ticket}})
                    return
                if existing and not command.get('force_pair') and compatible_extension(existing.get('extension_version')):
                    if command.get('automatic') and not existing.get('page_ready'):
                        repair_idle(self, existing)
                        self.setup_ready.emit({'request_id': command.get('request_id'), 'success': True,
                            'connected': True, 'repaired_pending': True, 'profile': profile, 'project': project})
                        self.publish()
                        return
                    preferences = {**self.preferences, 'profile_projects': {
                        **self.preferences.get('profile_projects', {}), profile: project}}
                    self.store.save_settings(preferences)
                    self.preferences = preferences
                    existing['setup_blocked'] = project_block(existing, preferences)
                    # This client already owns a project window. Activate that
                    # exact page and use the idle repair path when necessary.
                    self.request(existing, 'open_project', {'url': existing['saved_project']})
                    repairing = not existing.get('page_ready') and bool(repair_idle(self, existing))
                    if not existing.get('credits_local'):
                        self.request(existing, 'get_points')
                    self.request(existing, 'snapshot')
                    self.setup_ready.emit({'request_id': command.get('request_id'), 'success': True,
                                          'connected': True, 'profile': profile, 'project': project,
                                          'repaired_pending': repairing,
                                          'pairing': {**self.pair, 'profile': profile, 'project': project,
                                              **({'setup_id': existing['setup_id']} if existing.get('setup_id') else {})},
                                          'needs_update': bool(existing.get('extension_version') and
                                                               existing['extension_version'] != EXTENSION_VERSION)})
                    self.publish()
                    return
                client_ids = {c.get('id') for c in self.clients.values() if c.get('profile') == profile}
                if any(c.get('profile') == profile and c.get('batch_running') for c in self.clients.values()):
                    raise ValueError('网页队列仍在收尾，空闲后再更换连接。')
                if any(row['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} and
                       (row['browser'] in client_ids or row['remote'].get('profile') == profile) for row in self.store.rows()):
                    raise ValueError('这个浏览器仍在执行生成／下载，当前不能重新设置连接，请等它完成。')
                preferences = {**self.preferences, 'profile_projects': {
                    **self.preferences.get('profile_projects', {}), profile: project}}
                self.store.save_settings(preferences)
                self.preferences = preferences
                previous_setup = self.setup_requests.get(profile, {})
                reuse = (previous_setup.get('mode') != 'reload_only'
                    and not (command.get('automatic') and previous_setup.get('claimed'))
                    and previous_setup.get('expires', 0) > time.time()
                    and project_identity(previous_setup.get('project')) == project_identity(project)
                    and (not command.get('force_pair') or command.get('request_id') and previous_setup.get('request') == command['request_id']))
                setup_id = previous_setup['id'] if reuse else uuid.uuid4().hex
                self.setup_requests[profile] = {**(previous_setup if previous_setup.get('id') == setup_id else {}),
                    'id': setup_id, 'request': command.get('request_id'), 'project': project,
                    'expires': previous_setup['expires'] if reuse else time.time() + timing_value(self.preferences, 'setup_ticket_seconds'),
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
                changes = dict(command['value'])
                if 'timing' in changes:
                    changes.update(timing_changes(self.preferences, changes['timing']))
                elif 'stall_seconds' in changes and 'timing' in self.preferences:
                    changes['timing'] = {**self.preferences['timing'], 'stall_seconds': changes['stall_seconds']}
                    normalize_timing(changes['timing'])
                if 'timing' in changes: validate_timing(changes)
                if 'browser_options' in changes:
                    changes['browser_options'] = normalize_browser_options(changes['browser_options'])
                self.commit_preferences({**self.preferences, **changes})
                self.downloads_root = self.preferences.get('downloads_root') or self.downloads_root
                self.timer.setInterval(max(1, round(timing_value(self.preferences, 'queue_tick_seconds') * 1000)))
                if command.get('request_id'):
                    self.preferences_saved.emit({'request_id': command['request_id'], 'success': True,
                        'timing': timing_snapshot(self.preferences)})
                for current in list(self.clients.values()):
                    if current.get('authorized'):
                        self.send(current, {'type': 'timing_settings', 'timing': timing_snapshot(self.preferences),
                            'browser_options': browser_options_snapshot(self.preferences)})
            elif action == 'reset_account_state':
                reset_accounts(self, command.get('keys', []))
            elif action == 'mark_account_unavailable':
                keys = set(command.get('keys', []))
                if not keys: raise ValueError('请先选择需要停用的账号。')
                selected = [c for c in self.clients.values() if browser_keys(c) & keys]
                for key in keys:
                    if key.startswith('profile:') and not any('profile:' + c.get('profile', '') == key for c in selected):
                        selected.append({'profile': key[len('profile:'):], 'id': ''})
                for client in selected:
                    mark_account(self, client, '用户手动标记账号不可用', manual=True)
            elif action == 'credits':
                keys = set(command.get('keys') or [])
                selected = [client for client in self.clients.values() if client['authorized']
                            and ('keys' not in command or browser_keys(client) & keys)]
                if command.get('manual') and not selected:
                    raise ValueError('选中的浏览器未连接，请先连接该配置后再检测积分。')
                rows = self.store.rows()
                for client in selected:
                    if command.get('manual'):
                        client['credits_ready_failures'] = 0
                    busy = client.get('batch_running') or any((row['status'] in TRANSIENT or row['status'] == 'uncertain'
                                and not row['remote'].get('cloud_finished'))
                               and (row['browser'] == client['id'] or
                                    client.get('account') and row['remote'].get('account') == client['account'])
                               for row in rows)
                    if busy:
                        client['credit_check'] = {'state': 'deferred', 'at': time.time(),
                            'message': '此账号正在处理任务，完成后可手动检测；已保留当前任务'}
                        continue
                    self.request(client, 'get_points', manual_credit=bool(command.get('manual')))
                    if command.get('manual'):
                        client['credit_check']['hold_dispatch'] = True
                    self.request(client, 'snapshot')
            elif action == 'cancel':
                for row in self.store.rows():
                    if row['id'] in command['ids'] and row['status'] in {'queued', 'failed'}:
                        self.store.update(row['id'], 'cancelled')
            elif action == 'retry_now':
                from .retry_now import request_now
                request_now(self, command)
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
                    self._close_socket(socket)
                    self._disconnected(socket)
                server = getattr(self, 'server', None)
                if server: server.close()
                pool = getattr(self, 'pool', None)
                if pool: pool.shutdown(wait=True)
                self.stopped.emit()
                return
            self.publish()
        except Exception as error:
            if command.get('action') == 'preferences' and command.get('request_id'):
                self.preferences_saved.emit({'request_id': command['request_id'], 'success': False, 'error': str(error)})
            if command.get('action') == 'delete_records':
                self.records_deleted.emit({'success': False, 'request_id': command.get('request_id'), 'error': str(error)})
            if command.get('action') == 'reset_tasks':
                self.tasks_reset.emit({'success': False, 'request_id': command.get('request_id'), 'error': str(error)})
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
        tick_ok = True
        try:
            from model.OfflineMode import is_offline
            if is_offline():
                return
            for socket, client in list(self.clients.items()):
                if not socket_alive(socket):
                    self._disconnected(socket)
                    continue
                if client.get('authorized') and client.get('blocked') == '积分不足':
                    from .credit_policy import balance_ready
                    if balance_ready(client, preferences=self.preferences) and client['credits'] >= MIN_CREDITS and not credit_hold(client, self.preferences):
                        client['blocked'] = ''  # Old per-task affordability failures are not account bans.
                if not client['authorized'] and time.time() - client['connected'] > timing_value(self.preferences, 'handshake_timeout_seconds'):
                    self._close_socket(socket)
                    continue
                if client.get('page_ready') and time.time() - client.get('page_at', 0) > timing_value(self.preferences, 'page_stale_seconds'):
                    client['page_ready'] = False
                    note_readiness(client)
                    client['page_error'] = f'Flow 页面超过 {timing_value(self.preferences, "page_stale_seconds"):g} 秒未响应；已保留上次余额，等待页面恢复'
                if confirm_saved_connection(self, client):
                    self.publish()
            for request_id, request in list(self.pending.items()):
                # The page may first wait 45 seconds for controls, then open,
                # rerender and verify settings. Do not time out its preflight
                # at 60 seconds while the read-only preparation is still alive.
                owner = next((c for c in self.clients.values() if c.get('id') == request['client']), None)
                if time.time() - request['at'] < request_timeout(self.preferences, request['action'], owner):
                    continue
                self.pending.pop(request_id, None)
                if request.get('retired'):
                    continue
                if request['action'] == 'repair_project':
                    if owner:
                        owner['connection_recovery'] = {**owner.get('connection_recovery', {}), 'state': 'failed',
                            'message': '当前项目自动修复响应超时，将按设置继续恢复；未提交的任务可自动重新调度'}
                if not request['job'] and request['action'] in {'get_points', 'snapshot'}:
                    for client in self.clients.values():
                        if client.get('id') == request['client']:
                            client['page_error'] = '扩展已连接，但项目页面响应超时；请打开浏览器检查登录和项目'
                            if request['action'] == 'get_points':
                                if client.pop('manual_credit_pending', False):
                                    self.request(client, 'get_points', manual_credit=True)
                                    continue
                                client['credits_retry_manual'] = bool(request.get('manual_credit'))
                                client['credit_check'] = {'state': 'failed', 'at': time.time(),
                                    'message': '积分检测响应超时，请检查该浏览器的登录和项目页面后重试'}
                                failures = client.get('credits_ready_failures', 0) + 1
                                client.update(credits_ready_failures=failures,
                                    credits_waiting_ready=failures < timing_value(self.preferences, 'credit_read_attempts'),
                                    credits_waiting_account=False, credits_retry_at=time.time() + timing_value(self.preferences, 'credit_retry_seconds'))
                if request['action'] == 'close_exhausted':
                    client = next((c for c in self.clients.values() if c.get('id') == request['client']), None)
                    if client:
                        client['credit_close'] = {**client.get('credit_close', {}), 'state': 'failed',
                            'next_at': time.time() + timing_value(self.preferences, 'credit_close_retry_seconds'), 'message': '关闭浏览器响应超时，保留当前窗口'}
                if request['action'] == 'close_browser':
                    client = next((c for c in self.clients.values() if c.get('id') == request['client']), None)
                    if client:
                        from .runtime_close import timeout as close_timeout
                        close_timeout(client, self)
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
                            if row['status'] != 'preparing':
                                continue
                            defer_prepare(self, client, row, '参数准备响应超时，自动等待后重新核对；未提交生成。', 'FLOW_PAGE_NOT_READY')
                            self.publish()
                            continue
                        status = 'uncertain' if request['action'] in {'run_tasks', 'retry_download'} else 'failed'
                        self.store.update(row['id'], status, error='浏览器响应超时，保留记录。')
                        if client and request['action'] in {'run_tasks', 'retry_download'}:
                            self.request(client, 'snapshot')
                        self.publish()
            self.refresh_reset_holds()
            from .credit_refresh import watch as watch_credit_refresh
            if watch_credit_refresh(self): self.publish()
            if self.watch_stalled():
                self.publish()
            if watch_preparation(self):
                self.publish()
            from .environment_recovery import watch as watch_environment
            if watch_environment(self): self.publish()
            if watch_connections(self):
                self.publish()
            from .project_startup import watch as watch_project_startup
            if watch_project_startup(self): self.publish()
            if self.watch_unsubmitted():
                self.publish()
            if watch_generation_retries(self):
                self.publish()
            if self.watch_transfer_retries():
                self.publish()
            from .page_recovery import watch
            if watch(self): self.publish()
            if time.time() - getattr(self, '_last_publish', 0) >= timing_value(self.preferences, 'status_publish_seconds'):
                self.publish()
            rows = self.store.rows()
            batches = self.store.batches()
            retire_exhausted(self)
            admitted = admitted_queue(rows, batches, self.observed_costs, paused=self.paused, released=self.released)
            from .balance_dispatch import refresh_probe_cycle
            refresh_probe_cycle(self, rows)
            from .ordered_dispatch import queue_head, snapshot_clients, take, browser_order
            queued_rows = queue_head(self, rows, batches)
            if queued_rows and queued_rows[0]['remote'].get('prepare_retry', {}).get('next_at', 0) > time.time():
                return
            if queued_rows and not self.safety_paused:
                from .browser_slots import watch_probe_retries
                if watch_probe_retries(self): self.publish()
            from .high_credit import watch as watch_high_credit
            before_high_credit = self.preferences, self.high_credit_pending
            watch_high_credit(self, queued_rows)
            if before_high_credit != (self.preferences, self.high_credit_pending): self.publish()
            if not queued_rows or self.safety_paused:
                return
            clients = [client for client in self.clients.values() if client['authorized']]
            limit = max(1, min(8, int(self.preferences.get('concurrency', 2))))
            if sum(row['status'] in TRANSIENT for row in rows) >= limit:
                return
            if refresh_expired(self, queued_rows):
                self.publish()
            if refresh_needed(self, queued_rows): self.publish()
            client = queued = None
            # Strict FIFO: only the earliest admitted row may acquire a browser.
            affinity = affinity_context(clients, rows, self.preferences, {row['id'] for row in admitted})
            candidate = queued_rows[0]
            ready = snapshot_clients(self, fresh_clients(self, candidate, clients))
            selected = choose_client(ready, rows, limit, candidate['payload'], self.preferences,
                estimated_cost(candidate['payload'], self.observed_costs), affinity=affinity,
                selection_order={c['id']:index for index,c in enumerate(ready)})
            if selected and not prefer_smaller(self, candidate, selected, affinity=affinity):
                queued, client = candidate, selected
            if client:
                self.browser_reuse_wait = {}
                client.pop('reuse_recovery',None)
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
                from .browser_slots import seen as browser_seen
                browser_seen(self, client)
                remember_owner(self, queued, client)
                take(self, client)
                self.assign_lane(client, queued['payload'])
                remote = {'account': client['account'], 'profile': client.get('profile', ''), 'activity_at': time.time()}
                if queued['remote'].get('prepare_retry'):
                    remote['prepare_retry'] = queued['remote']['prepare_retry']
                if queued['remote'].get('generation_retry'):
                    remote['generation_retry'] = {**queued['remote']['generation_retry'], 'state': 'running'}
                if queued['remote'].get('upload_recovery'):
                    remote['upload_recovery'] = queued['remote']['upload_recovery']
                self.store.update(queued['id'], 'preparing', attempt=attempt, browser=client['id'], remote=remote)
                job = queued['payload']
                if not reuse_prepare(self, client, self.store.get(queued['id'])):
                    self.request(client, 'prepare', {'videoModel': 'omni_1_1_flash', 'duration': job['duration'],
                    'aspectRatio': 'ASPECT_RATIO_' + job['aspect'].replace(':', '_'),
                    'resolution': 'VIDEO_RESOLUTION_' + job['resolution'].upper(), 'footageType': 'VIDEO_FRAMES', 'count': 1}, queued['id'])
                self.publish()
            else:  # Actual Chrome opens share the GUI's single-shot spacing timer.
                from .browser_reuse import defer_launch
                if defer_launch(self,queued_rows[0]['payload']): return
                profiles = configured_profiles(self.preferences)
                online = {c.get('profile') for c in clients}
                held_profiles = {r['remote'].get('profile') for r in rows if r['status'] in TRANSIENT | {'uncertain', 'download_failed'}}
                held_profiles.update(hold['profile'] for hold in self.store.reset_holds() if hold['profile'])
                available = [p for p in profiles if p not in self.launched and p not in online and p not in held_profiles
                             and not block_for({'profile': p}, self.preferences) and can_start_browser(self, p)]
                cached = {profile: cached_profile(profile, self.preferences) for profile in available}
                held_accounts = {str(row['remote'].get('account') or '').strip().casefold() for row in rows
                    if row['status'] in TRANSIENT | {'uncertain', 'download_failed'}}
                ranked = [(dispatch_rank({'profile': profile, 'id': ''}, row['payload'], self.preferences), index, profile)
                          for index, row in enumerate(queued_rows) for profile in available
                          if (not cached[profile].get('account') or str(cached[profile]['account']).strip().casefold() not in held_accounts) and
                          can_fund(cached[profile], row['payload'], self.preferences, self.observed_costs) and
                          high_credit_allowed(cached[profile], self.preferences) and
                          allows_image(cached[profile], row['payload'], rows, self.preferences, affinity['images']) and
                          (str(cached[profile].get('account') or '').strip().casefold() in affinity['pool'] or
                           len(affinity['pool']) < limit)]
                ranked = sorted((((*browser_order(cached[profile], self.preferences), rank), index, profile)
                    for rank, index, profile in ranked if rank is not None),
                    key=lambda item:(item[0],item[1],available.index(item[2])))
                candidate = ranked[0][2] if ranked else None
                # Busy or unrelated lanes must not prevent starting a useful one.
                held = [r for r in rows if r['status'] in TRANSIENT | {'uncertain', 'download_failed'}]
                busy = {r['remote'].get('account') for r in held}
                useful_accounts = {c['account'] for c in clients if c.get('account') not in busy and any(
                    allows_image(c, row['payload'], rows, self.preferences, affinity['images']) and
                    capable(c, row['payload'], self.preferences, estimated_cost(row['payload'], self.observed_costs)) for row in queued_rows)}
                self.launching = {profile: at for profile, at in self.launching.items() if time.time() - at < timing_value(self.preferences, 'browser_startup_seconds')}
                # Cold connected pages and extension hello packets do not
                # justify opening the entire backup group while four pages
                # are still initializing or undergoing bounded recovery.
                from .credit_cache import pending_browser_checks
                starting = {c.get('profile') for c in clients if c.get('profile') and
                    c.get('profile') not in held_profiles and c.get('account') not in useful_accounts and
                    not block_for(c, self.preferences) and not c.get('blocked') and
                    (not c.get('page_ready') or not c.get('credits_local')) and
                    (time.time() - c.get('connected', 0) < timing_value(self.preferences, 'browser_startup_seconds') or
                     (c.get('connection_recovery') or {}).get('state') == 'checking' or
                     0 < (c.get('connection_recovery') or {}).get('count', 0) < timing_value(self.preferences, 'connection_repair_attempts'))}
                starting.update(profile for profile in pending_browser_checks(self, queued_rows, affinity)
                    if profile not in held_profiles)
                starting.update(p for p in self.launching if p not in held_profiles and
                    not any(c.get('profile') == p and c.get('account') in useful_accounts for c in clients))
                occupied = sum(r['status'] in TRANSIENT for r in rows) + len(useful_accounts) + len(starting)
                if candidate and occupied < limit:
                    target_job = queued_rows[ranked[0][1]]['payload']
                    if start_browser(self, candidate, target_job): self.publish()
        except Exception as error:
            tick_ok = False
            if temporary_database_lock(error):
                if not self._database_lock_reported:
                    self._database_lock_reported = True
                    self.failed.emit('Flow 本地队列暂时被占用，下次调度自动重试；原任务与手动放行项保持不变。')
                return
            self.paused = True
            self.released.clear()
            self.safety_paused = True
            self.failed.emit('Flow 队列已暂停：' + str(error))
        finally:
            if tick_ok:
                self._database_lock_reported = False

    def publish(self):
        self._last_publish = time.time()
        rows = self.store.rows()
        with self.store.connection() as db:
            charged = {row[0] for row in db.execute('SELECT attempt FROM charges')}
        batches = self.store.batches()
        clients = [{**{key: value for key, value in client.items() if key not in {'socket', 'authorized'}},
                    'duration_lane': lane_for(client, self.preferences),
                    'environment_block': block_for(client, self.preferences)}
                   for client in self.clients.values() if client['authorized']]
        for client in clients:
            client['account_state'] = account_hold(client, self.preferences)
            client['credit_cooldown'] = credit_hold(client, self.preferences)
        admitted = admitted_queue(rows, batches, self.observed_costs, paused=self.paused, released=self.released)
        affinity = affinity_context(clients, rows, self.preferences, {row['id'] for row in admitted})
        from .ordered_dispatch import queue_head
        fifo = queue_head(self,rows,batches)
        first = fifo[0]['id'] if fifo else None
        for row in rows:
            row['reset_risk'] = submission_risk(row, row['attempt'] in charged)
            row['wait_reason'] = waiting_reason(row, clients, rows, self.preferences, self.observed_costs, batches,
                paused=self.paused, released=self.released, affinity=affinity)
            detail = retry_detail(row, self.preferences)
            if detail: row['wait_reason'] = detail
            reuse_wait = getattr(self,'browser_reuse_wait',{})
            if (row['status'] == 'queued' and row['id'] == reuse_wait.get('job') and not self.safety_paused and
                    (not self.paused or row['id'] in self.released)):
                row['wait_reason'] = reuse_wait['message']
            if row['status'] == 'queued' and first and row['id'] != first and (not self.paused or row['id'] in self.released):
                row['wait_reason'] = '按提交顺序等待前序任务派发；'+row['wait_reason']
        from .runtime_close import batch_summary
        self.changed.emit({'jobs': rows, 'clients': clients, 'paused': self.paused,
                           'runtime_close_batch': batch_summary(self),
                           'browser_capacity': browser_capacity(self),
                           'high_credit_pending': dict(self.high_credit_pending) if self.high_credit_pending else None,
                           'pairing': self.pair, 'preferences': self.preferences, 'downloads_root': self.downloads_root,
                           'released': list(self.released), 'safety_paused': self.safety_paused,
                           'drafts': self.drafts, 'batches': batches, 'spaces': self.space_status(rows),
                           'saving': bool(self.archiving)})
        self.running_changed.emit(any(row['status'] in TRANSIENT | {'archiving', 'uncertain', 'download_failed'} for row in rows)
                                  or any(client.get('batch_running') for client in clients))
