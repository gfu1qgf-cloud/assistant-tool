"""One-profile connection guide. Chrome preferences are inspected, never edited."""
import json
import uuid

from qt_compat import QtCore, QtWidgets
from app_paths import APP_ROOT
from .setup_support import (inspect_extension, project_url, project_identity,
                            project_block, setup_project_entry, EXTENSION_VERSION, OPTIONS_URL, EXTENSIONS_URL, compatible_extension)
from .connection_status import browser_status
from .account_policy import account_hold


class _Signals(QtCore.QObject):
    done = QtCore.pyqtSignal(object)


class _Probe(QtCore.QRunnable):
    def __init__(self, root, profile):
        super().__init__(); self.root, self.profile = root, profile; self.signals = _Signals()

    def run(self):
        try: result = inspect_extension(self.root, self.profile)
        except (OSError, ValueError):
            result = {'profile': self.profile, 'status': 'unknown', 'version': '',
                      'reason': '本地安装记录无法读取，请在此配置的扩展页确认。'}
        self.signals.done.emit(result)


class ConnectionSetup(QtWidgets.QGroupBox):
    def __init__(self, dialog):
        super().__init__('连接向导 · 选择配置 → 自动打开项目并连接', dialog)
        self.dialog, self.drafts, self.active_profile = dialog, {}, ''
        self.dirty_profiles, self.stopping = set(), False
        self.request_id, self.probe, self.bundle = '', None, None
        self.phase, self.copied = '', ''
        self.auto_retries, self.retry_origin, self.auto_cancelled = 0, None, False
        self.claim_url, self.project_claim_url, self.waiting_profile = '', '', ''
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.profile = QtWidgets.QComboBox(self)
        self.project = QtWidgets.QLineEdit(self); self.project.setPlaceholderText('可以留空；浏览器打开后进入项目并复制地址，程序会自动识别')
        form.addRow('Chrome 配置', self.profile); form.addRow('本配置的 Flow 项目地址', self.project)
        layout.addLayout(form)
        self.connection_label = QtWidgets.QLabel(self)
        self.connection_label.setWordWrap(True)
        self.connection_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.connection_label)
        self.status = QtWidgets.QLabel('选择 Chrome 配置，点击“打开浏览器并连接”；已有地址会直接进入项目。', self)
        self.status.setWordWrap(True); self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        self.connect_button = QtWidgets.QPushButton('① 打开浏览器并连接', self)
        self.connect_button.clicked.connect(lambda: self.begin())
        self.connect_button.setAutoDefault(False); row.addWidget(self.connect_button)
        self.continue_button = QtWidgets.QPushButton('安装完成，继续配对', self)
        self.continue_button.setAutoDefault(False)
        self.continue_button.clicked.connect(self.continue_setup)
        self.continue_button.hide(); row.addWidget(self.continue_button)
        self.more_button = QtWidgets.QToolButton(self); self.more_button.setText('更多…')
        self.more_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.more_button)
        self.detect_button = menu.addAction('只检测安装状态', lambda: self.start_probe('check'))
        menu.addAction('检查／安装扩展…', lambda: self.start_probe('install'))
        menu.addAction('复制扩展管理地址', lambda: QtWidgets.QApplication.clipboard().setText(EXTENSIONS_URL))
        self.path_button = menu.addAction('复制扩展目录', self.copy_directory)
        menu.addAction('重新配对此配置…', lambda: self.begin('pair'))
        self.more_button.setMenu(menu); row.addWidget(self.more_button); row.addStretch()
        layout.addLayout(row)
        backup = QtWidgets.QHBoxLayout()
        backup.addWidget(QtWidgets.QLabel('故障备用（正常连接无需操作）：', self))
        self.copy_button = QtWidgets.QPushButton('复制配对信息', self)
        self.copy_button.setAutoDefault(False); self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_bundle); backup.addWidget(self.copy_button)
        self.options_button = QtWidgets.QPushButton('打开扩展设置', self)
        self.options_button.setAutoDefault(False); self.options_button.setEnabled(False)
        self.options_button.clicked.connect(self.open_options); backup.addWidget(self.options_button)
        backup.addStretch(); layout.addLayout(backup)
        self.hint = QtWidgets.QLabel('已有项目地址：点①，主程序打开项目，扩展后台自动领取并保存连接凭据，再通过 WebSocket 连接。'
            '无需复制 Token 或配对信息。首次未设置项目时，才需要在 Flow 中复制一次项目地址；下次直接使用已保存的地址。', self)
        self.hint.setWordWrap(True); layout.addWidget(self.hint)
        self.profile.currentIndexChanged.connect(self.changed_profile)
        self.project.textEdited.connect(lambda: self.dirty_profiles.add(self.active_profile))
        self.project.textChanged.connect(self.project_changed)
        self.clipboard_timer = QtCore.QTimer(self); self.clipboard_timer.setSingleShot(True)
        self.clipboard_timer.setInterval(120000); self.clipboard_timer.timeout.connect(self.clear_sensitive_clipboard)
        self.capture_timer = QtCore.QTimer(self); self.capture_timer.setSingleShot(True)
        self.capture_timer.setInterval(300000); self.capture_timer.timeout.connect(self.capture_expired)
        self.connection_timer = QtCore.QTimer(self); self.connection_timer.setSingleShot(True)
        self.connection_timer.setInterval(30000); self.connection_timer.timeout.connect(self.connection_expired)
        QtWidgets.QApplication.clipboard().dataChanged.connect(self.capture_project)
        self.apply_state({})

    def launcher(self):
        return getattr(getattr(self.dialog.plugin.context.parent_widget, 'chrome_plugin', None), 'dialog', None)

    def apply_state(self, state):
        launcher = self.launcher()
        profiles = list(getattr(launcher, 'profiles', []))
        stamp = tuple((p['directory'], p.get('name', '')) for p in profiles)
        if getattr(self, '_profiles', None) != stamp:
            self._profiles = stamp
            previous = self.profile.currentData()
            with QtCore.QSignalBlocker(self.profile):
                self.profile.clear()
                for item in profiles:
                    self.profile.addItem(f'{item.get("name") or item["directory"]} · {item["directory"]}', item['directory'])
                index = self.profile.findData(previous)
                if index >= 0: self.profile.setCurrentIndex(index)
            self.changed_profile()
        current = self.profile.currentData()
        if current and current not in self.drafts and current not in self.dirty_profiles:
            project = state.get('preferences', {}).get('profile_projects', {}).get(current)
            if not project:
                project = next((c.get('saved_project') for c in state.get('clients', []) if c.get('profile') == current), '')
            if project_identity(project): self.project.setText(project)
        self.render_connection(state)

    def render_connection(self, state):
        current = self.profile.currentData()
        client = next((c for c in state.get('clients', []) if c.get('profile') == current), None)
        status = browser_status(client or {'profile': current}, state.get('preferences', {}), state.get('jobs', []))
        self.connection_label.setText(f'扩展：{status["connection"]}  ·  项目：{status["project"]}  ·  {status["state"]}\n{status["reason"]}')
        self.connection_label.setStyleSheet(f'color: {status["color"]};')
        stamp = (current, client.get('id') if client else None, status['state'], status['reason'])
        if stamp != getattr(self, '_connection', None) and not self.request_id and not self.probe:
            self._connection = stamp
            self.connect_button.setText('① 打开浏览器并检查' if client and not client.get('setup_pending') else '① 打开浏览器并连接')
        if (not self.request_id and not self.probe and client
                and not project_block(client, state.get('preferences', {}))
                and client.get('page_ready') is True
                and compatible_extension(client.get('extension_version'))
                and self.phase == 'auto_pair'):
            self.connection_timer.stop()
            self.phase = ''
            self.auto_retries = 0; self.retry_origin = None
            self.status.setText('已自动连接成功；回到任务面板提交即可，之后自动调度。')

    def changed_profile(self, *_):
        self.connection_timer.stop()
        self.auto_retries = 0; self.retry_origin = None
        if self.waiting_profile:
            self.waiting_profile = ''; self.capture_timer.stop()
        self.bundle = None; self.claim_url = ''; self.project_claim_url = ''
        self.phase = ''
        if self.active_profile and (self.project.text().strip() or self.active_profile in self.dirty_profiles):
            self.drafts[self.active_profile] = self.project.text().strip()
        self.active_profile = self.profile.currentData() or ''
        stored = self.dialog.state.get('preferences', {}).get('profile_projects', {}).get(self.active_profile)
        saved = next((c.get('saved_project') for c in self.dialog.state.get('clients', [])
                      if c.get('profile') == self.active_profile), '')
        self.project.setText(self.drafts.get(self.active_profile, stored or saved or ''))
        self.sync_pair_buttons()
        self.status.setText('尚无项目地址，点击“打开浏览器并连接”，再到 Flow 打开项目并复制地址即可。' if not self.project.text().strip()
                            else '已有项目地址，点击“打开浏览器并连接”即可。')
        self.render_connection(self.dialog.state)

    def select_profile(self, profile):
        if self.request_id or self.probe:
            self.status.setText('连接向导正在处理当前配置，请完成后再切换。'); return
        self.apply_state(self.dialog.state)
        index = self.profile.findData(profile)
        if index >= 0: self.profile.setCurrentIndex(index)

    def prompt_project(self):
        self.status.setText('这个 Chrome 配置没有已确认的项目地址，请手动添加；任务仍保留排队，没有提交生成。')
        self.project.setFocus()

    def _busy(self, busy):
        for widget in (self.profile, self.project, self.connect_button, self.detect_button, self.continue_button):
            widget.setEnabled(not busy)
        self.sync_pair_buttons(busy=busy)

    def sync_pair_buttons(self, *_args, busy=False):
        if not hasattr(self, 'copy_button'): return
        try: project = project_url(self.project.text(), required=True)
        except ValueError: project = ''
        self.copy_button.setEnabled(bool(project and not busy)); self.options_button.setEnabled(bool(project and not busy))
        hint = ('点击后保存项目并生成／复制配对信息；再打开扩展设置粘贴保存。' if project
                else '先填写或复制这个浏览器的有效 Flow 项目地址，才能生成配对信息。')
        self.copy_button.setToolTip(hint)
        self.options_button.setToolTip('为此配置生成新的自动连接入口并打开扩展设置，无需粘贴。' if project else hint)

    def project_changed(self, *_):
        self.sync_pair_buttons()
        if self.phase == '' and not self.waiting_profile and not self.request_id and not self.probe:
            self.status.setText('已有项目地址，点击①自动完成连接；需要手动操作时，可用下方备用按钮。' if project_identity(self.project.text())
                                else '项目地址可留空，点击①打开浏览器，在 Flow 项目中复制地址即可。')

    def begin(self, phase='connect', *, automatic=False):
        if self.request_id or self.probe: return
        if not automatic: self.auto_cancelled = False
        self.connection_timer.stop()
        profile = self.profile.currentData()
        if not profile:
            self.status.setText('请先在 Chrome 启动器中加载浏览器配置。'); return
        self.waiting_profile = ''; self.capture_timer.stop()
        if not self.project.text().strip():
            self.bundle = None; self.claim_url = ''
            self.start_probe('discover'); return
        try: project = project_url(self.project.text(), required=True)
        except ValueError as error:
            self.status.setText(str(error)); self.project.setFocus(); return
        if not automatic:
            self.auto_retries = 0
            self.retry_origin = (profile, project_identity(project))
            self.auto_cancelled = False
        self.drafts[profile] = project; self.phase = phase; self.request_id = uuid.uuid4().hex
        self._busy(True); self.status.setText('正在保存此配置的项目地址；成功后准备连接。')
        self.dialog.send('setup_profile', request_id=self.request_id, profile=profile, project=project,
                         force_pair=phase in {'pair', 'options'}, automatic=automatic)

    def prepared(self, result):
        if self.stopping: return
        if result.get('request_id') != self.request_id: return
        self.request_id = ''
        if self.auto_cancelled:
            self._busy(False); return
        if not result.get('success'):
            self._busy(False); self.status.setText(result['error'])
            if result.get('deferred'):
                self.auto_retries = max(0, self.auto_retries - 1)
                self.phase = 'auto_pair'; self.connection_timer.start(30000)
            elif self.auto_retries and not result.get('account_blocked'):
                self.phase = 'auto_pair'; self.connection_timer.start(60000)
            return
        if result.get('connected'):
            if result.get('pairing'):
                self.bundle = dict(result['pairing'])
            self._busy(False)
            if self.phase == 'copy':
                self.copy_bundle(); return
            if result.get('repaired_pending'):
                self.phase = 'auto_pair'; self.connection_timer.start(30000)
                self.status.setText('正在检查并自动恢复原项目页的扩展消息通道；等待页面重新确认。')
                return
            if not self.launch_urls([result.get('project') or self.project.text().strip()]): return
            client = next((c for c in self.dialog.state.get('clients', []) if c.get('profile') == self.profile.currentData()), {})
            if self.auto_retries or not client.get('page_ready'):
                self.phase = 'auto_pair'
                self.connection_timer.start((30000, 60000, 120000, 120000)[self.auto_retries])
            if result.get('needs_update'):
                self.status.setText('扩展已连接且协议兼容，不用再复制配置，可以继续使用。有修复更新可用，空闲时再加载；无需重新配对。')
                return
            self.status.setText('扩展已经连接，已打开该配置的项目并刷新状态；没有重新配对，也没有暂停接单。')
            return
        self.bundle = dict(result['pairing'])
        self.claim_url = result.get('claim_url', '')
        self.project_claim_url = result.get('project_claim_url', '')
        self.sync_pair_buttons()
        if self.phase == 'copy':
            self._busy(False); self.copy_bundle(); return
        self.start_probe(self.phase)

    def start_probe(self, phase):
        if self.probe or self.stopping: return
        launcher = self.launcher(); profile = self.profile.currentData()
        if not launcher or not profile:
            self._busy(False); self.status.setText('Chrome 启动器尚未加载可用配置。'); return
        self.phase = phase; self._busy(True)
        client = next((c for c in self.dialog.state.get('clients', []) if c.get('profile') == profile), None)
        if client:
            self.probed({'profile': profile, 'status': 'connected', 'version': client.get('extension_version', ''),
                         'reason': '桥接扩展已在线，安装状态已确认。'})
            return
        root = getattr(launcher, 'user_data_dir', '')
        if not root:
            self.probed({'profile': profile, 'status': 'unknown', 'version': '', 'reason': '未确认 Chrome 用户数据目录。'})
            return
        self.status.setText('正在后台只读检测扩展；不改 Chrome 配置文件。')
        self.probe = _Probe(root, profile)
        self.probe.signals.done.connect(self.probed)
        QtCore.QThreadPool.globalInstance().start(self.probe)

    @QtCore.pyqtSlot(object)
    def probed(self, result):
        self.probe = None; self._busy(False)
        if self.stopping or self.auto_cancelled: return
        if result['profile'] != self.profile.currentData(): return
        if self.phase == 'check':
            self.status.setText(result['reason'] + (f' 记录版本：{result["version"]}。' if result.get('version') else ''))
            return
        if self.phase == 'install':
            self.show_install(result)
            return
        # Chrome may finish starting while its on-disk Preferences probe is
        # still running. A live bridge is authoritative over that stale hint.
        client = next((c for c in self.dialog.state.get('clients', [])
                       if c.get('profile') == result['profile']), None)
        if client:
            result = {**result, 'status': 'connected', 'version': client.get('extension_version', '')}
        if self.phase == 'discover':
            if (result['status'] == 'unknown' or result['status'] in {'connected', 'enabled'}
                    and compatible_extension(result.get('version'))):
                self.open_flow_for_project()
            else:
                self.show_install(result)
            return
        if not self.bundle or self.bundle.get('profile') != result['profile']:
            self.status.setText('连接信息已变化，请重新点击“连接此浏览器”。'); return
        update = result['status'] in {'connected', 'enabled'} and not compatible_extension(result.get('version'))
        # Unknown is not "missing". Open the one-time project entry first so
        # an already installed extension can wake and claim it automatically.
        ready = self.phase in {'pair', 'options'} or result['status'] in {'connected', 'enabled', 'unknown'}
        if not ready:
            self.show_install(result); return
        try:
            url = setup_project_entry(self.bundle, self.project_claim_url,
                action='options' if self.phase == 'options' or update else '')
        except ValueError as error:
            self.status.setText(str(error)); return
        launcher = self.launcher()
        profile = next((p for p in launcher.profiles if p['directory'] == result['profile']), None)
        if not profile:
            self.status.setText('这个 Chrome 配置已不存在，请刷新 Chrome 启动器。'); return
        ok, error = launcher.launch_profile(profile, urls=[url])
        if not ok:
            self.status.setText('启动所选 Chrome 配置失败：' + str(error)); return
        self.continue_button.hide()
        self.phase = 'auto_pair'
        self.connection_timer.start((30000, 60000, 120000, 120000)[self.auto_retries])
        self.status.setText((f'自动重试 {self.auto_retries}/3：' if self.auto_retries else '') +
            '主程序已将本配置的 Flow 项目网址交给 Chrome；扩展正在后台自动完成连接。')

    def launch_urls(self, urls):
        # Never pass chrome:// or chrome-extension:// as the sole startup
        # target. Chrome can filter internal URLs and open a New Tab instead.
        if not urls or any(not str(url).startswith('https://') for url in urls):
            self.status.setText('未提供有效网页地址，已阻止启动空白标签页。'); return False
        launcher = self.launcher()
        profile = next((p for p in getattr(launcher, 'profiles', []) if p['directory'] == self.profile.currentData()), None)
        if not profile:
            self.status.setText('未找到这个 Chrome 配置，请刷新 Chrome 启动器。'); return False
        ok, error = launcher.launch_profile(profile, urls=urls)
        if not ok: self.status.setText('打开浏览器失败：' + str(error))
        return ok

    def show_install(self, result):
        project = self.project.text().strip()
        url = project if project_identity(project) else 'https://flow.google.com/'
        if not self.launch_urls([url]): return
        from .ui import open_path
        open_path(APP_ROOT / 'browser_extensions/flow_bridge')
        QtWidgets.QApplication.clipboard().setText(str(APP_ROOT / 'browser_extensions/flow_bridge'))
        self.continue_button.setText('安装／更新完成，打开 Flow')
        self.continue_button.show(); self.phase = 'await_install'
        action = '已安装则启用／重新加载，不用重复安装' if result['status'] in {'connected', 'enabled', 'disabled', 'unknown'} else '点“加载已解压的扩展程序”，选择已打开的目录'
        self.status.setText('第 1 步：' + result['reason'] + '\n已打开此浏览器的 Flow 和扩展目录（目录已复制）。'
            '首次安装请在浏览器地址栏输入 chrome://extensions/；“更多”中也可复制管理地址。' + action +
            '。完成后点旁边“安装／更新完成，打开 Flow”。')

    def continue_setup(self):
        if self.phase == 'await_install' and not self.project.text().strip():
            self.open_flow_for_project()
        else:
            self.begin('pair')

    def open_flow_for_project(self):
        if not self.launch_urls(['https://flow.google.com/']): return
        self.waiting_profile = self.profile.currentData(); self.phase = 'await_project'
        self.continue_button.hide(); self.capture_timer.start()
        self.status.setText('第 2 步：已打开 Flow。请在这个浏览器进入要用的项目，复制地址栏网址（Ctrl+L、Ctrl+C）；程序会自动识别并完成后续配置，不用回软件粘贴。')

    def capture_project(self):
        if self.stopping or not self.waiting_profile or self.waiting_profile != self.profile.currentData(): return
        try: project = project_url(QtWidgets.QApplication.clipboard().text(), required=True)
        except ValueError: return
        self.waiting_profile = ''; self.capture_timer.stop()
        self.project.setText(project); self.dirty_profiles.add(self.active_profile)
        self.status.setText('已识别复制的 Flow 项目地址，正在自动保存并连接。')
        self.begin()

    def capture_expired(self):
        self.waiting_profile = ''
        self.status.setText('本次等待复制地址已结束。需要继续时点“打开浏览器并连接”，也可以直接粘贴项目地址。')

    def connection_expired(self):
        self.connection_timer.stop()
        if self.stopping or self.auto_cancelled or self.phase != 'auto_pair' or self.request_id or self.probe: return
        profile = self.profile.currentData()
        origin = (profile, project_identity(self.project.text()))
        if self.retry_origin and origin != self.retry_origin:
            self.phase = ''; return
        state = self.dialog.state; preferences = state.get('preferences', {})
        client = next((c for c in state.get('clients', []) if c.get('profile') == profile), None)
        identity = client or {'profile': profile}
        if account_hold(identity, preferences):
            self.phase = ''; self.status.setText('此账号已隔离，请处理登录／验证问题后手动重置账号状态。'); return
        account = str(identity.get('account') or '').strip().casefold()
        busy = any(row.get('status') in {'preparing', 'uploading', 'submitting', 'generating', 'downloading',
                'archiving', 'uncertain', 'download_failed'} and
            (row.get('remote', {}).get('profile') == profile or client and row.get('browser') == client.get('id') or
             account and str(row.get('remote', {}).get('account') or '').strip().casefold() == account)
            for row in state.get('jobs', []))
        if busy or client and client.get('batch_running'):
            self.status.setText('原账号仍在执行／核对任务，空闲后再自动重试连接。')
            self.connection_timer.start(30000); return
        if self.auto_retries >= 3:
            self.phase = ''
            self.status.setText('自动连接已重试3次仍未确认。请从“更多 → 检查／安装扩展”检查此配置，或处理浏览器中的登录／验证提示。')
            return
        self.auto_retries += 1
        self.begin(automatic=True)
        self.status.setText(f'自动重试 {self.auto_retries}/3：重新准备此配置的连接；等待确认后再调度。')

    def cancel_auto_retry(self):
        self.connection_timer.stop(); self.auto_cancelled = True; self.retry_origin = None

    def copy_bundle(self):
        if not self.copy_button.isEnabled(): return
        if (not self.bundle or self.bundle.get('profile') != self.profile.currentData()
                or self.bundle.get('project') != project_url(self.project.text(), required=True)):
            self.begin('copy'); return
        self.copied = json.dumps(self.bundle, ensure_ascii=False)
        QtWidgets.QApplication.clipboard().setText(self.copied)
        self.clipboard_timer.start()
        self.status.setText('配对信息已复制。备用手动连接：点“打开扩展设置”，粘贴后保存即可。')

    def copy_directory(self):
        self.clear_sensitive_clipboard()
        QtWidgets.QApplication.clipboard().setText(str(APP_ROOT / 'browser_extensions/flow_bridge'))
        self.status.setText('扩展目录已复制；安装后点击“安装完成，继续配对”，会重新准备连接信息。')

    def open_options(self):
        if project_identity(self.project.text()):
            self.begin('options')

    def clear_sensitive_clipboard(self):
        self.clipboard_timer.stop()
        clipboard = QtWidgets.QApplication.clipboard()
        if self.copied and clipboard.text() == self.copied: clipboard.clear()
        self.copied = ''

    def shutdown(self):
        self.cancel_auto_retry()
        self.stopping = True; self.request_id = ''; self.bundle = None
        self.waiting_profile = ''; self.capture_timer.stop()
        self.connection_timer.stop()
        self.clear_sensitive_clipboard()
