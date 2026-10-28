from qt_compat import QtCore
from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand, PluginSettingsPage, PluginMainWidget
from .timing import CONFIG_KEY, normalize, snapshot as timing_snapshot, validate
from .browser_options import CONFIG_KEY as BROWSER_CONFIG_KEY, normalize as normalize_browser_options, snapshot as browser_options_snapshot
import json
import time
import uuid
import random

CANCELLED_LAUNCH = '已取消待启动浏览器'


class FlowAssistantPlugin(QtCore.QObject):
    command = QtCore.pyqtSignal(object)
    plugin_id = 'flow_assistant'
    display_name = 'Chrome 与 Flow 助手'
    version = '0.6.30'
    upstream_version = '1.1.6'
    ui_api_version = 4
    manual_credit_check_api = 1
    account_state_api = 1
    high_credit_api = 1
    high_credit_permission_api = 1
    video_catalog_api = 1
    force_browser_reset_api = 1
    timing_settings_api = 1
    retry_now_api = 1
    force_delete_api = 1
    required_api_version = 1

    def __init__(self):
        super().__init__()
        self.context = self.dialog = self.thread = self.service = None
        self.snapshot = {}
        self.mutex_snapshot = {}
        self.busy = False
        self.launch_probes, self.stopping = {}, False
        self.browser_launches = {}
        self.pending_browser_launches = {}
        self.launch_timer = QtCore.QTimer(self)
        self.launch_timer.setSingleShot(True)
        self.launch_timer.timeout.connect(self._next_browser_launch)
        self.timing_request = ''
        self.pending_timing = None
        self.high_credit_box = None
        from ..chrome_launcher import ChromeLauncherPlugin
        self.browser_launcher = ChromeLauncherPlugin()
        self.browser_launcher.flow_controller = self
        self.browser_launcher.profile_skip_factory = self.iterator_skip_filter
        self.skip_exhausted_iterator = True
        from .launch_ui import LaunchUiGate
        self.launch_ui = LaunchUiGate(self)

    def register(self, context):
        self.context = context
        from .browser_manager import LauncherContext, BrowserManagerSettings
        self.browser_launcher.context = LauncherContext(context)
        context.register_command(PluginCommand('open', 'Chrome 与 Flow 助手…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=30, tooltip='统一管理Chrome配置、分组、书签和Flow视频生成；共用账号积分'))
        context.register_command(PluginCommand('generate', '生成 Flow 视频…', self.open_tasks,
            frozenset({TASK_CONTEXT_MENU}), order=34, enabled=lambda rows: bool(rows)))
        context.register_settings_page(PluginSettingsPage('timing', 'Flow 视频助手 · 时间与重试', self.create_timing_settings, order=212))
        context.register_settings_page(PluginSettingsPage('browser', 'Flow 视频助手 · 浏览器操作', self.create_browser_settings, order=213))
        context.register_settings_page(PluginSettingsPage('chrome', 'Chrome启动与快捷键', BrowserManagerSettings, order=210))
        context.register_main_widget(PluginMainWidget('quick_actions', self.browser_launcher.create_quick_actions,
            order=20, title=self.display_name))

    def iterator_skip_filter(self):
        from .browser_manager import saved_preferences, skip_reason
        preferences = self.snapshot.get('preferences')
        if not isinstance(preferences, dict): preferences = saved_preferences(self.context)
        clients = self.snapshot.get('clients', [])
        skip_exhausted=self.skip_exhausted_iterator
        return lambda profile: skip_reason(profile['directory'], preferences, clients,skip_exhausted=skip_exhausted)

    def open_browser_manager(self):
        dialog = self.open_dialog(refresh=False)
        dialog.show_chrome()
        return dialog

    def open_flow_queue(self):
        dialog = self.open_dialog()
        dialog.show_flow_page(0)
        return dialog

    def create_browser_settings(self, parent=None):
        from .browser_options_ui import BrowserOptionsSettings
        return BrowserOptionsSettings(parent, preferences=self.snapshot.get('preferences'))

    def create_timing_settings(self, parent=None):
        from .timing_ui import TimingSettings
        preferences = self.snapshot.get('preferences')
        if preferences is None:
            # Read legacy preferences without creating tables or touching the queue.
            preferences = {}
            saved = self.context.storage.file('queue.sqlite3')
            if saved.exists():
                db = saved.connect()
                try:
                    row = db.execute('SELECT value FROM settings WHERE key="preferences"').fetchone()
                    if row: preferences = json.loads(row[0])
                finally: db.close()
        return TimingSettings(parent, preferences=preferences)

    def start(self):
        self.stopping = False
        try:
            from .bridge_install import prepare
            if prepare():self.context.log('Flow桥接扩展文件已切换到当前版本；旧浏览器将在空闲时自动重载。')
        except Exception as error:
            self.context.log('Flow扩展文件自动准备失败，已保留原文件：'+str(error))
        from .service import FlowService
        self.thread = QtCore.QThread(self)
        downloads = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.DownloadLocation)
        config = self.context.load_config()
        from .browser_manager import skip_setting
        self.skip_exhausted_iterator = skip_setting(config)
        self.browser_launcher.start()
        timing = validate({'timing': normalize(config[CONFIG_KEY])}) if CONFIG_KEY in config else None
        browser = normalize_browser_options(config[BROWSER_CONFIG_KEY]) if BROWSER_CONFIG_KEY in config else None
        self.service = FlowService(self.context.storage, downloads, program_timing=timing, program_browser_options=browser)
        self.service.native_ui_result.connect(self.launch_ui.result)
        self.service.moveToThread(self.thread)
        self.thread.started.connect(self.service.start)
        self.command.connect(self.service.execute)
        self.service.changed.connect(self._changed)
        self.service.ui_lock_changed.connect(self._ui_lock_changed)
        self.service.failed.connect(self._failed)
        self.service.scheduler_attention.connect(self._scheduler_attention)
        self.service.launch_requested.connect(self._launch)
        self.service.project_open_requested.connect(self._open_missing_project)
        self.service.high_credit_confirmation_requested.connect(self._confirm_high_credit)
        self.service.running_changed.connect(self._running)
        self.service.finished_job.connect(lambda path: self.context.log('Flow 视频已保存：' + path))
        self.service.submitted.connect(self._submitted)
        self.service.submission_failed.connect(self._submission_failed)
        self.service.setup_ready.connect(self._setup_ready)
        self.service.records_deleted.connect(self._records_deleted)
        self.service.tasks_reset.connect(self._tasks_reset)
        self.service.preferences_saved.connect(self._preferences_saved)
        self.service.video_catalog_result.connect(self._video_catalog_result)
        self.service.tab_reused.connect(self._tab_reused)
        self.service.stopped.connect(self.thread.quit, QtCore.Qt.ConnectionType.DirectConnection)
        self.thread.finished.connect(self.service.deleteLater)
        self.thread.start()

    def _scheduler_attention(self, report):
        self.context.notify('Flow 队列需要处理',report['message'])

    def _ui_lock_changed(self, value):
        self.mutex_snapshot=value
        if self.dialog:
            self.dialog.ui_lock_monitor.apply(value)
            self.dialog.queue_tools.apply_ui_lock(self.dialog.ui_lock_monitor.data)

    def _changed(self, value):
        self.snapshot = value
        box = self.high_credit_box
        if box and (value.get('high_credit_pending') or {}).get('nonce') != box.request['nonce']:
            box.reject()
        connected = {client.get('profile') for client in value.get('clients', [])}
        self.browser_launches = {key: at for key, at in self.browser_launches.items() if key[0] not in connected}
        if self.dialog:
            self.dialog.update_state(value)

    def _confirm_high_credit(self, request):
        from .timing import value as timing_value
        if self.stopping or time.time() - request.get('issued_at', time.time()) >= timing_value(self.snapshot.get('preferences', {}), 'request_timeout_seconds'):
            self.command.emit({'action':'high_credit_decision', 'nonce':request['nonce'], 'approved':False, 'source':'not_shown'})
            return
        if self.high_credit_box:
            self.high_credit_box.reject()
        from .high_credit_ui import HighCreditConfirmation
        from qt_compat import QtWidgets
        parent = self.dialog or self.context.parent_widget
        box = HighCreditConfirmation(request, parent)
        box.shown.connect(self.command.emit)
        self.high_credit_box = box
        def finished(result):
            chose_yes = result == int(QtWidgets.QMessageBox.StandardButton.Yes)
            decided_at = time.time()
            approved = chose_yes and box.expires_at is not None and decided_at < box.expires_at
            if self.high_credit_box is box: self.high_credit_box = None
            self.command.emit({'action':'high_credit_decision', 'nonce':request['nonce'], 'approved':approved,
                               'decided_at':decided_at,
                               'source':'dismissed' if self.stopping else 'timeout' if box.timed_out or chose_yes and not approved else 'user'})
            box.deleteLater()
        box.finished.connect(finished)
        box.open()

    def _running(self, value):
        self.busy = value
        guard = getattr(self.context.parent_widget, 'flow_guard_thread', None)
        if guard is not None and hasattr(guard, 'set_bridge_busy'):
            guard.set_bridge_busy(value)

    def _tab_reused(self, value):
        from .tab_reuse import result
        result(self, value)

    def _video_catalog_result(self, result):
        if self.dialog:
            picker = getattr(self.dialog.queue_tools, 'video_dialog', None)
            if picker: picker.receive(result)

    def _failed(self, text):
        self.context.log(text)
        if self.dialog:
            self.dialog.note.setText(text)

    def _submission_failed(self, result):
        if self.dialog:
            if result.get('source') == 'manual':
                self.dialog.manual.failed(result)
                return
            self.dialog.batch.failed(result)

    def _submitted(self, result):
        if self.dialog:
            if result.get('source') == 'manual':
                self.dialog.manual.acknowledged(result)
                return
            self.dialog.batch.acknowledged(result)

    def _setup_ready(self, result):
        from .bridge_recovery import prepared
        if prepared(self,result):return
        if self.dialog:
            self.dialog.setup.prepared(result)

    def _records_deleted(self, result):
        if self.dialog:
            self.dialog.records_deleted(result)

    def _tasks_reset(self, result):
        if self.dialog:
            self.dialog.tasks_reset(result)

    def _preferences_saved(self, result):
        if result.get('request_id') != self.timing_request: return
        self.timing_request, self.pending_timing = '', None
        if not result.get('success'):
            self.context.log('Flow 设置已写入程序设置，但本次应用失败：' + str(result.get('error') or '未知错误') +
                '；已保留当前运行参数，下次正常启动会重新应用。')

    def launch_browser(self, directory, urls, *, completed=None, guard=None, bootstrap_existing=False):
        """Coalesce pending opens; Flow may reuse Chrome's existing window."""
        from .timing import value as timing_value
        from .browser_manager import launcher_for
        launcher = launcher_for(self)
        dialog = getattr(launcher, 'dialog', None)
        profile = next((p for p in getattr(dialog, 'profiles', []) if p['directory'] == directory), None)
        if not profile:
            if completed: completed(False, 'Chrome 配置已不可用：' + directory)
            return False, 'Chrome 配置已不可用：' + directory
        targets = tuple(str(url).strip() for url in urls if str(url).strip())
        if not targets or any(not url.startswith('https://') for url in targets):
            if completed: completed(False, '没有有效的 Flow 网页地址。')
            return False, '没有有效的 Flow 网页地址。'
        now = time.monotonic()
        wait = timing_value(self.snapshot.get('preferences', {}), 'browser_startup_seconds')
        self.browser_launches = {key: at for key, at in self.browser_launches.items() if now - at < wait}
        key = directory, targets
        if key in self.browser_launches:
            if completed: completed(True, None)
            return True, None
        if key in self.pending_browser_launches:
            if completed: self.pending_browser_launches[key]['callbacks'].append(completed)
            return True, None
        active=self.launch_ui.active
        if active and (active['profile'],active['targets'])==key:
            if completed:
                if active.get('reported'):completed(active['opened'],active.get('error'))
                else:active['callbacks'].append(completed)
            return True,None
        entry = {'profile':directory, 'targets':targets, 'guard':guard, 'callbacks':[completed] if completed else [],
                 'ticket':uuid.uuid4().hex, 'bootstrap_existing':bootstrap_existing}
        self.launch_ui.track(entry)
        if self.launch_timer.isActive() or self.pending_browser_launches or active:
            self.pending_browser_launches[key] = entry
            return True, None
        return self._perform_browser_launch(entry)

    def _perform_browser_launch(self, entry):
        if self.launch_ui.enabled():return self.launch_ui.request(entry)
        self.launch_ui.untrack(entry)
        return self._launch_now(entry)

    def _launch_report(self, entry, ok, error):
        if entry.get('reported'):return
        entry.update(reported=True,opened=ok,error=error)
        for callback in entry['callbacks']:
            try:callback(ok,error)
            except Exception as failure:self.context.log('启动浏览器回执处理失败：'+str(failure))
        if not ok and not entry['callbacks'] and error != CANCELLED_LAUNCH:self._failed(error)

    def _launch_now(self, entry):
        directory, targets = entry['profile'], entry['targets']
        from .browser_manager import launcher_for
        launcher = launcher_for(self)
        dialog = getattr(launcher, 'dialog', None)
        profile = next((p for p in getattr(dialog, 'profiles', []) if p['directory'] == directory), None)
        try:
            if self.stopping or entry['guard'] and not entry['guard']():
                ok, error = False, CANCELLED_LAUNCH
            elif not profile:
                ok, error = False, 'Chrome 配置已不可用：' + directory
            else:
                from .tab_reuse import route
                reused = route(self, entry)
                if isinstance(reused, tuple): return reused
                options = {} if browser_options_snapshot(self.snapshot.get('preferences',{}))['focus_before_action'] else {'activate':False}
                # A cold Chrome must receive its saved HTTPS project. An
                # extension settings entrance alone cannot confirm navigation.
                # Connected profiles still take the reuse route above.
                actual = reused if isinstance(reused, list) else targets
                ok, error = dialog.launch_profile(profile, urls=list(actual), new_window=False, **options)
        except Exception as failure: ok, error = False, str(failure)
        if ok:
            from .timing import value as timing_value
            preferences = self.snapshot.get('preferences', {})
            gap = random.uniform(timing_value(preferences, 'browser_launch_interval_seconds'),
                timing_value(preferences, 'browser_launch_interval_seconds') + timing_value(preferences, 'browser_launch_jitter_seconds'))
            self.launch_timer.start(max(1, round(gap * 1000)))
            self.browser_launches[directory, targets] = time.monotonic()
        self._launch_report(entry,ok,error)
        return ok, error

    def _next_browser_launch(self):
        if not self.pending_browser_launches or self.launch_ui.active: return
        key = next(iter(self.pending_browser_launches))
        entry = self.pending_browser_launches.pop(key)
        ok, _error = self._perform_browser_launch(entry)
        if not ok and self.pending_browser_launches: self.launch_timer.start(0)

    def _open_missing_project(self, command):
        from .project_startup import launch_allowed
        if self.stopping or not launch_allowed(self.snapshot, command):
            self.command.emit({**command, 'opened':False, 'cancelled':True})
            return
        def completed(ok, error):
            self.command.emit({**command, 'opened':ok, 'error':error, 'cancelled':error == CANCELLED_LAUNCH})
        self.launch_browser(command['profile'], [command['project']], completed=completed,
            guard=lambda: launch_allowed(self.snapshot, command))

    def _launch(self, directory):
        from .browser_manager import launcher_for
        launcher = launcher_for(self)
        dialog = getattr(launcher, 'dialog', None)
        if dialog is None:
            self.command.emit({'action':'browser_launch_result','profile':directory,'opened':False,'error':'Chrome 启动器尚未就绪'})
            self._failed('Chrome 启动器尚未就绪，保留 Flow 队列。')
            return
        profile = next((item for item in dialog.profiles if item['directory'] == directory), None)
        if profile is None:
            self.command.emit({'action':'browser_launch_result','profile':directory,'opened':False,'error':'Chrome 配置已不存在'})
            self._failed('配置组中的 Chrome 配置已不存在：' + directory)
            return
        from .setup_support import project_identity
        project = self.snapshot.get('preferences', {}).get('profile_projects', {}).get(directory)
        if not project_identity(project):
            panel = self.open_dialog()
            panel.open_setup(directory)
            panel.setup.prompt_project()
            self.context.log(f'{directory} 未保存 Flow 项目地址，任务保留排队，请在连接向导手动添加。')
            return
        if directory in self.launch_probes or self.stopping: return
        from .setup_ui import _Probe
        probe = _Probe(getattr(dialog, 'user_data_dir', ''), directory)
        self.launch_probes[directory] = probe
        probe.signals.done.connect(self._launch_checked)
        QtCore.QThreadPool.globalInstance().start(probe)

    @QtCore.pyqtSlot(object)
    def _launch_checked(self, result):
        directory = result['profile']; self.launch_probes.pop(directory, None)
        if self.stopping: return
        slot = self.snapshot.get('preferences', {}).get('browser_slots', {}).get(directory, {})
        if slot.get('state') == 'opening' and (self.snapshot.get('safety_paused') or
                self.snapshot.get('paused') and not self.snapshot.get('released')):
            self.command.emit({'action':'browser_launch_result','profile':directory,'opened':False,'cancelled':True})
            return
        from .setup_support import compatible_extension, project_identity, EXTENSION_VERSION
        live = next((c for c in self.snapshot.get('clients', []) if c.get('profile') == directory), None)
        if live:
            result = {**result, 'status': 'enabled', 'version': live.get('extension_version', '')}
        if (result['status'] != 'enabled' and not result.get('can_launch')) or not compatible_extension(result.get('version')):
            retryable = (result['status'] == 'unknown' and (not result.get('version') or compatible_extension(result['version']))
                and '矛盾' not in str(result.get('reason') or ''))
            error = ('扩展安装状态暂未确认，将按设置自动复查；尚未打开此配置：' if retryable else
                '扩展安装检查未通过，请在连接向导修正后恢复；尚未打开此配置：') + directory
            details = str(result.get('reason') or '')
            if result.get('version') and not compatible_extension(result['version']):
                details = f'检测到扩展 {result["version"]}，当前程序支持 1.1.8～{EXTENSION_VERSION}；请使程序与桥接扩展版本匹配'
            if details: error += '；' + details[:250]
            from .problem_details import manual_action
            if not retryable: error += '；需要手动处理：' + manual_action(details or '扩展安装检查失败')
            self.command.emit({'action': 'browser_launch_result', 'profile': directory, 'opened': False,
                               'error': error, 'kind': 'extension_probe_v3', 'retryable': retryable,
                               'probe': {key: result.get(key) for key in ('status', 'version', 'reason', 'can_launch')}})
            self._failed(error)
            return
        from .browser_manager import launcher_for
        launcher = launcher_for(self)
        dialog = getattr(launcher, 'dialog', None)
        profile = next((p for p in getattr(dialog, 'profiles', []) if p['directory'] == directory), None)
        if not profile:
            self.command.emit({'action':'browser_launch_result','profile':directory,'opened':False,'error':'Chrome 配置已不可用'})
            self._failed('Chrome 配置已不可用，保留 Flow 队列：' + directory); return
        # Do not inherit the launcher's unrelated preset websites (or an empty
        # new tab). Re-read the current per-profile project after the probe.
        project = self.snapshot.get('preferences', {}).get('profile_projects', {}).get(directory)
        if not project_identity(project):
            panel = self.open_dialog(); panel.open_setup(directory)
            panel.setup.prompt_project()
            return
        if live:
            # The worker rechecks the current profile and guards paid work.
            # Never turn an online page recovery into another Chrome launch.
            self.command.emit({'action': 'open_browser', 'profile': directory})
            return
        def still_needed():
            from .high_credit import allowed
            from .image_affinity import cached_profile
            from .policy import block_for
            from .credit_policy import balance_ready
            from .credit_cooldown import MIN_CREDITS
            state = self.snapshot
            preferences = state.get('preferences', {})
            cached = cached_profile(directory, preferences)
            if block_for(cached, preferences): return False
            if balance_ready(cached, preferences=preferences) and cached['credits'] < MIN_CREDITS: return False
            if 'profiles' in preferences and directory not in preferences['profiles']: return False
            if 'jobs' in state and not any(row['status']=='queued' for row in state['jobs']):return False
            if not allowed(cached, preferences):
                return False
            if (state.get('preferences', {}).get('profile_projects', {}).get(directory) != project or
                state.get('preferences', {}).get('browser_slots', {}).get(directory, {}).get('state') == 'opening'
                and (state.get('safety_paused') or state.get('paused') and not state.get('released'))):
                return False
            if any(c.get('profile') == directory for c in state.get('clients', [])):
                self.command.emit({'action':'open_browser','profile':directory})
                return False
            return True
        def completed(ok, error):
            from .launch_retries import ui_wait_error, KIND
            deferred=not ok and ui_wait_error(error)
            self.command.emit({'action':'browser_launch_result','profile':directory,'opened':ok,'error':error,
                              **({'cancelled':True} if error == CANCELLED_LAUNCH else {}),
                              **({'kind':KIND} if deferred else {})})
            if not ok and error != CANCELLED_LAUNCH and not deferred: self._failed('启动 Chrome 配置失败：' + str(error))
        self.launch_browser(directory, [project], completed=completed, guard=still_needed, bootstrap_existing=True)

    def open_dialog(self, *, refresh=True):
        created = self.dialog is None
        if self.dialog is None:
            from .ui import FlowDialog
            self.dialog = FlowDialog(self, self.context.parent_widget)
            if self.snapshot:
                self.dialog.update_state(self.snapshot)
        if refresh:
            self.refresh_tasks(activate=created)
        if self.mutex_snapshot:self.dialog.ui_lock_monitor.apply(self.mutex_snapshot)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    @staticmethod
    def _snapshots(targets):
        snapshots = []
        for target in targets:
            task = target['task']
            snapshots.append({'target_dir': target['target_dir'], 'label': target['label'],
                'task': {key: str(getattr(task, key, '') or '') for key in
                         ('task_id', 'task_name', 'task_date', 'task_type', 'task_video_prompt')}})
        return snapshots

    def refresh_tasks(self, *, activate=False):
        if self.dialog is None or self.context is None or self.stopping:
            return
        if self.dialog.manual.request_id:
            self.dialog.note.setText('当前提交正在保存，请等保存回执后再刷新任务；现有内容未改变。')
            return
        targets = self._snapshots(self.context.current_task_targets())
        self.dialog.set_targets(targets, activate=activate)

    def open_tasks(self, rows):
        targets = self.context.task_targets(rows)
        if not targets:
            return
        dialog = self.open_dialog(refresh=False)
        if dialog.manual.request_id:
            dialog.note.setText('当前提交正在保存，请等保存回执后再切换任务；现有内容未改变。')
            return
        # Right-click keeps the explicitly selected scope for bulk operations.
        # The main-menu entry / Refresh Tasks loads the whole current table.
        dialog.set_targets(self._snapshots(targets), preferred_directory=targets[0]['target_dir'])

    def apply_settings(self, config):
        from .browser_manager import skip_setting
        skip = skip_setting(config)
        changes = {}
        if CONFIG_KEY in config:
            changes['timing'] = validate({'timing': normalize(config[CONFIG_KEY])})
        if BROWSER_CONFIG_KEY in config:
            changes['browser_options'] = normalize_browser_options(config[BROWSER_CONFIG_KEY])
        if self.browser_launcher.context is not None and not self.browser_launcher.apply_settings(config): return False
        self.skip_exhausted_iterator = skip
        if not changes: return True
        if self.service is not None and self.thread is not None and self.thread.isRunning():
            if self.pending_timing == changes: return True
            preferences = self.snapshot.get('preferences')
            if self.pending_timing is None and isinstance(preferences, dict):
                current = {'timing': timing_snapshot(preferences), 'browser_options': browser_options_snapshot(preferences)}
                if all(current[key] == item for key, item in changes.items()): return True
            self.timing_request, self.pending_timing = uuid.uuid4().hex, changes
            self.command.emit({'action': 'preferences', 'request_id': self.timing_request, 'value': changes})
        return True

    def update_config(self, config):
        from .browser_manager import SKIP_CONFIG_KEY
        self.browser_launcher.update_config(config)
        config[SKIP_CONFIG_KEY] = self.skip_exhausted_iterator
        return config

    def can_close(self):
        return (False, 'Flow 视频正在归档／校验下载副本，请等文件操作完成后关闭。') if self.snapshot.get('saving') or any(
            row['status'] == 'archiving' for row in self.snapshot.get('jobs', [])) else (True, '')

    def stop(self):
        self.stopping = True
        if self.high_credit_box: self.high_credit_box.reject()
        self.launch_timer.stop()
        self.launch_ui.stop()
        self.browser_launcher.stop()
        pending, self.pending_browser_launches = self.pending_browser_launches, {}
        for entry in pending.values():
            self.launch_ui.untrack(entry)
            for callback in entry['callbacks']: callback(False, CANCELLED_LAUNCH)
        if self.dialog:
            self.dialog.setup.shutdown()
            self.dialog.manual.flush_draft()
        if self.thread is None or not self.thread.isRunning():
            return
        self.command.emit({'action': 'stop'})
        if not self.thread.wait(5000):
            return False
        self._running(False)
        return True
