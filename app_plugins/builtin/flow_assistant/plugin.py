from qt_compat import QtCore
from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand


class FlowAssistantPlugin(QtCore.QObject):
    command = QtCore.pyqtSignal(object)
    plugin_id = 'flow_assistant'
    display_name = 'Flow 视频助手'
    version = '0.4.15'
    upstream_version = '1.1.4'
    ui_api_version = 1
    required_api_version = 1

    def __init__(self):
        super().__init__()
        self.context = self.dialog = self.thread = self.service = None
        self.snapshot = {}
        self.busy = False
        self.launch_probes, self.stopping = {}, False

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand('open', 'Flow 视频助手…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=33, tooltip='连接 FlowCue 扩展、批量生成 Omni 视频、保存队列与任务归档'))
        context.register_command(PluginCommand('generate', '生成 Flow 视频…', self.open_tasks,
            frozenset({TASK_CONTEXT_MENU}), order=34, enabled=lambda rows: bool(rows)))

    def start(self):
        self.stopping = False
        from .service import FlowService
        self.thread = QtCore.QThread(self)
        downloads = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.DownloadLocation)
        self.service = FlowService(self.context.storage, downloads)
        self.service.moveToThread(self.thread)
        self.thread.started.connect(self.service.start)
        self.command.connect(self.service.execute)
        self.service.changed.connect(self._changed)
        self.service.failed.connect(self._failed)
        self.service.launch_requested.connect(self._launch)
        self.service.running_changed.connect(self._running)
        self.service.finished_job.connect(lambda path: self.context.log('Flow 视频已保存：' + path))
        self.service.submitted.connect(self._submitted)
        self.service.submission_failed.connect(self._submission_failed)
        self.service.setup_ready.connect(self._setup_ready)
        self.service.records_deleted.connect(self._records_deleted)
        self.service.stopped.connect(self.thread.quit, QtCore.Qt.ConnectionType.DirectConnection)
        self.thread.finished.connect(self.service.deleteLater)
        self.thread.start()

    def _changed(self, value):
        self.snapshot = value
        if self.dialog:
            self.dialog.update_state(value)

    def _running(self, value):
        self.busy = value
        guard = getattr(self.context.parent_widget, 'flow_guard_thread', None)
        if guard is not None and hasattr(guard, 'set_bridge_busy'):
            guard.set_bridge_busy(value)

    def _failed(self, text):
        self.context.log(text)
        if self.dialog:
            self.dialog.note.setText(text)

    def _submission_failed(self, result):
        if self.dialog:
            if result.get('source') == 'manual':
                self.dialog.manual.failed(result)
                return
            self.dialog.submit_button.setEnabled(True)
            self.dialog.submit_page.setEnabled(True)
            self.dialog.note.setText(result['error'])
            if result.get('duplicate_ids') and self.dialog.confirm_duplicate(result['duplicate_ids']):
                self.dialog.submit_button.setEnabled(False); self.dialog.submit_page.setEnabled(False)
                self.dialog.send('enqueue', **self.dialog.batch_request, allow_duplicates=True)

    def _submitted(self, result):
        if self.dialog:
            if result.get('source') == 'manual':
                self.dialog.manual.acknowledged(result)
                return
            count = result['count']
            self.dialog.compose.setRowCount(0)
            self.dialog.submit_button.setEnabled(True)
            self.dialog.submit_page.setEnabled(True)
            self.dialog.note.setText(f'已保存 {count} 条生成任务；' + ('暂未放行，请查看暂停／等待原因。' if result.get('held') else '已自动加入调度，生成和下载会在后台完成。'))
            self.dialog.tabs.setCurrentIndex(0)

    def _setup_ready(self, result):
        if self.dialog:
            self.dialog.setup.prepared(result)

    def _records_deleted(self, result):
        if self.dialog:
            self.dialog.records_deleted(result)

    def _launch(self, directory):
        launcher = getattr(self.context.parent_widget, 'chrome_plugin', None)
        dialog = getattr(launcher, 'dialog', None)
        if dialog is None:
            self._failed('Chrome 启动器尚未就绪，保留 Flow 队列。')
            return
        profile = next((item for item in dialog.profiles if item['directory'] == directory), None)
        if profile is None:
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
        from .setup_support import compatible_extension, project_identity
        live = next((c for c in self.snapshot.get('clients', []) if c.get('profile') == directory), None)
        if live:
            result = {**result, 'status': 'enabled', 'version': live.get('extension_version', '')}
        if result['status'] != 'enabled' or not compatible_extension(result.get('version')):
            panel = self.open_dialog(); panel.open_setup(directory)
            if panel.setup.profile.currentData() == directory and not panel.setup.request_id and not panel.setup.probe:
                panel.setup.begin()
            return
        launcher = getattr(self.context.parent_widget, 'chrome_plugin', None)
        dialog = getattr(launcher, 'dialog', None)
        profile = next((p for p in getattr(dialog, 'profiles', []) if p['directory'] == directory), None)
        if not profile:
            self._failed('Chrome 配置已不可用，保留 Flow 队列：' + directory); return
        # Do not inherit the launcher's unrelated preset websites (or an empty
        # new tab). Re-read the current per-profile project after the probe.
        project = self.snapshot.get('preferences', {}).get('profile_projects', {}).get(directory)
        if not project_identity(project):
            panel = self.open_dialog(); panel.open_setup(directory)
            panel.setup.prompt_project()
            return
        ok, error = dialog.launch_profile(profile, urls=[project])
        if not ok:
            self._failed('启动 Chrome 配置失败：' + str(error))

    def open_dialog(self, *, refresh=True):
        created = self.dialog is None
        if self.dialog is None:
            from .ui import FlowDialog
            self.dialog = FlowDialog(self, self.context.parent_widget)
            if self.snapshot:
                self.dialog.update_state(self.snapshot)
        if refresh:
            self.refresh_tasks(activate=created)
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

    def apply_settings(self, _config):
        return True

    def can_close(self):
        return (False, 'Flow 视频正在归档／校验下载副本，请等文件操作完成后关闭。') if self.snapshot.get('saving') or any(
            row['status'] == 'archiving' for row in self.snapshot.get('jobs', [])) else (True, '')

    def stop(self):
        self.stopping = True
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
