import uuid
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from .store import IMAGE_SUFFIXES, RECORD_UNRESOLVED, submission_risk
from .submission_ui import ManualSubmitPanel
from .routing import DURATIONS, lane_key, automatic_lane
from .manual_panel import estimate_text
from .task_ui import TaskSelector
from .connection_status import browser_status
from .credit_cooldown import credit_hold
from .credit_cache import restore_balance
from .timing import value as timing_value
from .pricing import RULE_NOTE

STATUS = {'queued': '等待执行', 'preparing': '核对模型／费用', 'uploading': '上传参考图',
          'submitting': '提交生成', 'generating': '生成中', 'downloading': '下载中',
          'archiving': '保存到任务目录', 'completed': '已完成', 'failed': '失败',
          'download_failed': '下载自动恢复／需处理', 'archive_failed': '归档待重试',
          'uncertain': '等待原浏览器核对', 'cancelled': '已取消',
          'download_parked': '下载已暂存／账号已释放', 'needs_review': '已保存 · 待复核'}


def open_path(path):
    if not path:
        return
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))


class FlowDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        if (getattr(plugin, 'ui_api_version', 1) != 2
                or not all(hasattr(plugin, name) for name in ('context', 'command'))):
            raise RuntimeError('Flow 源码已更新，本次运行仍是旧服务。请在当前任务结束后正常重启辅助工具，再使用新面板；现有队列不会重新提交。')
        super().__init__(parent)
        self.plugin, self.targets, self.state = plugin, [], {}
        self.delete_request = ''
        self.reset_request = ''
        self.setWindowTitle('Flow 视频助手 · Omni 生成队列')
        self.resize(1160, 760)
        layout = QtWidgets.QVBoxLayout(self)
        self.note = QtWidgets.QLabel('选择任务编号和图片 → 写提示词 → 提交并生成。浏览器按需启动，成品自动下载到任务目录；连接和等待原因见下方。')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        status_row = QtWidgets.QHBoxLayout()
        self.connection_summary = QtWidgets.QLabel('浏览器：尚未连接', self)
        self.connection_summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.connection_summary.setWordWrap(True); status_row.addWidget(self.connection_summary, 1)
        self.connection_button = self.button('浏览器连接／时长设置', lambda: self.tabs.setCurrentWidget(self.browser_page), status_row)
        layout.addLayout(status_row)
        self.tabs = QtWidgets.QTabWidget(self)
        layout.addWidget(self.tabs, 1)
        self._queue_page()
        self.manual = ManualSubmitPanel(self)
        self.tabs.addTab(self.manual, '手动逐条提交')
        self._submit_page()
        self._browser_page()
        self._settings_page()

    def button(self, text, callback, layout):
        button = QtWidgets.QPushButton(text, self)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    @staticmethod
    def table(headers, parent):
        widget = QtWidgets.QTableWidget(0, len(headers), parent)
        widget.setHorizontalHeaderLabels(headers)
        widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        widget.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        widget.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        widget.horizontalHeader().setStretchLastSection(True)
        widget.verticalHeader().hide()
        return widget

    def _queue_page(self):
        page = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(page)
        controls = QtWidgets.QHBoxLayout()
        self.button('开始／继续等待项', self.start_queue, controls)
        self.button('暂停后续任务', lambda: self.send('pause'), controls)
        self.button('重试选中', lambda: self.send('retry', ids=self.selected_ids()), controls)
        self.retry_download_button = self.button('重试下载', self.retry_selected_downloads, controls)
        self.reset_button = self.button('完全重置并重新调度', self.reset_selected_tasks, controls)
        self.reset_button.setToolTip('清除选中任务的原浏览器绑定及重试状态，重新选择可用浏览器；可能已提交的任务需先确认。')
        self.delete_button = self.button('删除记录…', lambda: self.delete_records(), controls)
        self.queue_menu_button = QtWidgets.QToolButton(self); self.queue_menu_button.setText('选中项操作…')
        self.queue_menu_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.queue_menu_button)
        menu.addAction('只提交选中项', lambda: self.send('submit_selected', ids=self.selected_ids()))
        menu.addAction('取消等待项', lambda: self.send('cancel', ids=self.selected_ids()))
        menu.addAction('完全重置并重新调度…', self.reset_selected_tasks)
        menu.addAction('打开任务目录', self.open_directory)
        menu.addAction('播放成品', self.open_output)
        self.queue_menu_button.setMenu(menu); controls.addWidget(self.queue_menu_button)
        controls.addStretch()
        layout.addLayout(controls)
        from .queue_ui import QueueTools
        self.queue_tools = QueueTools(self, page, layout)
        self.progress = QtWidgets.QLabel('尚无任务')
        layout.addWidget(self.progress)
        self.queue = self.table(['任务', '参考图／时长', '状态', '执行账号', '保存文件', '详情'], page)
        self.queue.setColumnWidth(0, 150)
        self.queue.setColumnWidth(1, 170)
        self.queue.setColumnWidth(2, 135)
        self.queue.setColumnWidth(3, 180)
        self.queue.setColumnWidth(4, 200)
        self.queue.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue.customContextMenuRequested.connect(self.queue_menu)
        self.delete_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Delete), self.queue)
        self.delete_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_shortcut.activated.connect(lambda: self.delete_records())
        layout.addWidget(self.queue, 1)
        self.tabs.addTab(page, '全局生成队列')

    def _submit_page(self):
        page = QtWidgets.QWidget(self)
        self.submit_page = page
        layout = QtWidgets.QVBoxLayout(page)
        self.task_selector = TaskSelector(page)
        self.target = self.task_selector.combo
        self.task_selector.changed.connect(self.load_images)
        self.task_selector.refresh_requested.connect(self.refresh_task_catalog)
        layout.addWidget(self.task_selector)
        controls = QtWidgets.QHBoxLayout()
        self.button('选中图片加入清单', self.add_selected_images, controls)
        self.button('全部任务图片加入清单', self.add_all_images, controls)
        layout.addLayout(controls)
        self.images = QtWidgets.QListWidget(page)
        self.images.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.images.setIconSize(QtCore.QSize(100, 85))
        self.images.setMaximumHeight(150)
        self.images.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.images)
        self.compose = self.table(['任务', '参考图片', '提示词（双击可修改）'], page)
        self.compose.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked)
        self.compose.setColumnWidth(0, 160)
        self.compose.setColumnWidth(1, 200)
        layout.addWidget(self.compose, 1)
        options = QtWidgets.QHBoxLayout()
        self.duration = QtWidgets.QComboBox(page)
        self.duration.addItem('模型默认', None)
        for seconds in (4, 6, 8, 10): self.duration.addItem(f'{seconds} 秒', seconds)
        self.aspect = QtWidgets.QComboBox(page); self.aspect.addItems(['9:16', '16:9'])
        self.resolution = QtWidgets.QComboBox(page); self.resolution.addItems(['720p', '360p'])
        self.count = QtWidgets.QSpinBox(page); self.count.setRange(1, 20)
        self.budget = QtWidgets.QSpinBox(page); self.budget.setRange(0, 10000); self.budget.setSpecialValueText('自动计算积分')
        self.budget.setToolTip(RULE_NOTE + '。非0为单条预算；低于固定费用时停止提交。其他时长保留页面读取或手动预算。')
        for title, widget in [('模型：Omni 1.1 Flash', None), ('时长', self.duration), ('比例', self.aspect),
                              ('分辨率', self.resolution), ('每图数量', self.count), ('单条积分预算', self.budget)]:
            options.addWidget(QtWidgets.QLabel(title))
            if widget: options.addWidget(widget)
        layout.addLayout(options)
        limits = QtWidgets.QHBoxLayout()
        self.batch_label = QtWidgets.QLineEdit(page); self.batch_label.setPlaceholderText('提交批次名称（可选）')
        self.batch_limit = QtWidgets.QSpinBox(page); self.batch_limit.setRange(0, 10**8); self.batch_limit.setSpecialValueText('不限整批')
        limits.addWidget(self.batch_label, 1); limits.addWidget(QtWidgets.QLabel('整批积分上限')); limits.addWidget(self.batch_limit)
        layout.addLayout(limits)
        self.batch_estimate = QtWidgets.QLabel(page); self.batch_estimate.setWordWrap(True); layout.addWidget(self.batch_estimate)
        self.compose.itemChanged.connect(self.update_estimate)
        for widget in (self.duration, self.aspect, self.resolution): widget.currentIndexChanged.connect(self.update_estimate)
        for widget in (self.count, self.budget): widget.valueChanged.connect(self.update_estimate)
        actions = QtWidgets.QHBoxLayout()
        self.button('删除清单选中项', self.remove_compose, actions)
        self.button('批量应用提示词…', self.apply_prompt, actions)
        actions.addStretch()
        self.save_only = QtWidgets.QCheckBox('仅保存，暂不生成', page); actions.addWidget(self.save_only)
        self.submit_button = self.button('提交并生成', self.enqueue, actions)
        self.save_only.toggled.connect(lambda checked: self.submit_button.setText('仅保存到队列' if checked else '提交并生成'))
        layout.addLayout(actions)
        self.tabs.addTab(page, '批量添加任务')

    def _browser_page(self):
        page = QtWidgets.QWidget(self)
        self.browser_page = page
        layout = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        self.button('刷新账号积分／核对中断任务', lambda: self.send('credits', manual=True), row)
        self.button('打开选中浏览器', self.open_selected_browser, row)
        self.button('修正后恢复选中浏览器', self.resume_selected_browsers, row)
        self.button('连接／安装扩展…', self.open_setup, row)
        row.addStretch()
        layout.addLayout(row)
        hint = QtWidgets.QLabel('默认自动安排时长：优先同档和通用浏览器，没有匹配时会把空闲的自动配置改为所需时长并接单，无需手动分档。\n'
            '正在生成／下载的浏览器不改；需要专用账号时，可手动选“固定 6／8／10 秒”。积分连接时读取一次，之后本地扣算。\n'
            '选中浏览器后右键“检测选中浏览器积分”，可直接检查账号和积分；积分栏显示检测时间或失败原因。')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.allow_general = QtWidgets.QCheckBox('允许通用档浏览器补位（不使用其他专用时长档）', page)
        self.allow_general.setChecked(True)
        self.allow_general.toggled.connect(self.general_changed)
        layout.addWidget(self.allow_general)
        self.auto_lane = QtWidgets.QCheckBox('自动分配时长，空闲配置可按排队任务切换（推荐）', page)
        self.auto_lane.setChecked(True)
        self.auto_lane.toggled.connect(lambda checked: self.send('preferences', value={'auto_lane': checked}))
        layout.addWidget(self.auto_lane)
        self.browsers = self.table(['Chrome 配置', 'Flow 登录账号', '可用积分／本地估算', '时长分档', '扩展连接', '项目页面', '接单状态', '等待原因／下一步'], page)
        self.browsers.setColumnWidth(1, 200)
        self.browsers.setColumnWidth(2, 145)
        self.browsers.setColumnWidth(3, 175)
        for column in (4, 5, 6): self.browsers.setColumnWidth(column, 100)
        self.browsers.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.browsers.cellDoubleClicked.connect(lambda row, _column: self.open_browser_row(row))
        self.browsers.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.browsers.customContextMenuRequested.connect(self.browser_menu)
        self.browsers.setToolTip('右键浏览器行可检测所选浏览器积分；无需提交生成任务。')
        self.lane_edits = {}
        self.pending_lanes = {}
        layout.addWidget(self.browsers)
        self.tabs.addTab(page, '浏览器与时长分档')

    def _settings_page(self):
        page = QtWidgets.QWidget(self)
        self.settings_page = page
        layout = QtWidgets.QVBoxLayout(page)
        hint = QtWidgets.QLabel('先用下方连接向导设置各个 Chrome 配置，再选择允许自动启动的配置组。日常只需选任务编号、选图、写提示词并提交。\n'
            '每个配置的项目地址分别保存；没有地址会提示手动添加，不会从其他网页猜测。首次加载扩展仍由 Chrome 要求手动确认。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        from .setup_ui import ConnectionSetup
        self.setup = ConnectionSetup(self); layout.addWidget(self.setup)
        form = QtWidgets.QFormLayout()
        self.group = QtWidgets.QComboBox(page)
        self.group.addItem('只使用已连接浏览器', [])
        launcher = getattr(getattr(self.plugin.context.parent_widget, 'chrome_plugin', None), 'dialog', None)
        if launcher:
            for group in launcher.profile_groups:
                self.group.addItem(group['name'], launcher.get_group_profile_directories(group['id']))
        self.group.currentIndexChanged.connect(lambda: self.update_browsers())
        self.concurrent = QtWidgets.QSpinBox(page); self.concurrent.setRange(1, 8); self.concurrent.setValue(2)
        self.download_root = QtWidgets.QLineEdit(page)
        root_row = QtWidgets.QWidget(page); root_layout = QtWidgets.QHBoxLayout(root_row); root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.download_root, 1)
        self.button('选择…', self.pick_download_root, root_layout)
        form.addRow('允许自动启动的浏览器配置组', self.group)
        form.addRow('最多并行账号', self.concurrent)
        form.addRow('浏览器默认下载目录', root_row)
        self.upload_retries = QtWidgets.QSpinBox(page); self.upload_retries.setRange(0, 10); self.upload_retries.setValue(5)
        self.download_retries = QtWidgets.QSpinBox(page); self.download_retries.setRange(0, 10); self.download_retries.setValue(5)
        for widget in (self.upload_retries, self.download_retries):
            widget.setSuffix(' 次'); widget.setSpecialValueText('关闭自动重试')
            widget.setToolTip('首次失败后最多自动重试的次数；等待间隔在“程序设置 → 插件设置 → Flow 视频助手 · 时间与重试”中设置。只重试传输，不重新生成。')
        form.addRow('图片上传自动重试', self.upload_retries)
        form.addRow('视频下载自动重试', self.download_retries)
        timing_hint = QtWidgets.QLabel('积分有效期、冷却、超时和重试间隔：在主界面的“程序设置 → 插件设置 → Flow 视频助手 · 时间与重试”中统一修改。', page)
        timing_hint.setWordWrap(True); layout.addWidget(timing_hint)
        self.cleanup_downloads = QtWidgets.QCheckBox('归档校验成功且无质量异常后，自动清理浏览器下载副本（默认关闭）', page)
        form.addRow(self.cleanup_downloads)
        layout.addLayout(form)
        self.button('保存调度与下载设置', self.save_preferences, layout)
        layout.addWidget(QtWidgets.QLabel('成品保存在提交时的任务目录，浏览器下载原件暂时保留。队列与配对信息使用主程序私有存储。'))
        layout.addStretch()
        self.tabs.addTab(page, '连接与设置')
        self.preferences_loaded = False

    def send(self, action, **values):
        self.plugin.command.emit({'action': action, **values})

    def open_setup(self, profile=None):
        if not isinstance(profile, str):
            selected = self.browser_selections()
            profile = selected[0].get('profile') if selected else None
        if profile: self.setup.select_profile(profile)
        self.tabs.setCurrentWidget(self.settings_page)

    def refresh_task_catalog(self):
        self.plugin.refresh_tasks()

    def set_targets(self, targets, *, preferred_directory=None, activate=True):
        self.targets = list(targets)
        self.task_selector.set_targets(targets, preferred_directory=preferred_directory)
        self.manual.picker.set_targets(targets, preferred_directory=preferred_directory)
        if activate: self.tabs.setCurrentWidget(self.manual)

    def load_images(self):
        self.images.clear()
        target = self.target.currentData()
        if not target: return
        try:
            for path in sorted(Path(target['target_dir']).iterdir()):
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    reader = QtGui.QImageReader(str(path))
                    size = reader.size()
                    if size.isValid(): reader.setScaledSize(size.scaled(100, 85, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
                    image = reader.read()
                    item = QtWidgets.QListWidgetItem(QtGui.QIcon(QtGui.QPixmap.fromImage(image)), path.name)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path)); item.setToolTip(str(path))
                    self.images.addItem(item)
        except OSError as error: self.note.setText(str(error))

    def add_image(self, target, path):
        row = self.compose.rowCount(); self.compose.insertRow(row)
        for column, text in enumerate((target['label'], Path(path).name, target['task'].get('task_video_prompt', ''))):
            item = QtWidgets.QTableWidgetItem(text)
            if column < 2: item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.compose.setItem(row, column, item)
        self.compose.item(row, 0).setData(QtCore.Qt.ItemDataRole.UserRole, {'target': target, 'image': str(path)})

    def add_selected_images(self):
        target = self.target.currentData()
        if target:
            for item in self.images.selectedItems(): self.add_image(target, item.data(QtCore.Qt.ItemDataRole.UserRole))

    def add_all_images(self):
        for target in self.targets:
            for path in sorted(Path(target['target_dir']).iterdir()):
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES: self.add_image(target, path)

    def remove_compose(self):
        for row in sorted({item.row() for item in self.compose.selectedItems()}, reverse=True): self.compose.removeRow(row)

    def apply_prompt(self):
        text, ok = QtWidgets.QInputDialog.getMultiLineText(self, '应用提示词', '应用到选中行（未选则全部）：')
        if ok:
            rows = {item.row() for item in self.compose.selectedItems()} or range(self.compose.rowCount())
            for row in rows: self.compose.item(row, 2).setText(text)

    def enqueue(self):
        items = []
        for row in range(self.compose.rowCount()):
            value = self.compose.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            prompt = self.compose.item(row, 2).text()
            if not prompt.strip():
                self.note.setText(f'第 {row + 1} 项缺少提示词，请先填写。'); return
            items.append({'target': value['target'], 'images': [value['image']], 'prompt': prompt,
                'duration': self.duration.currentData(), 'aspect': self.aspect.currentText(),
                'resolution': self.resolution.currentText(), 'count': self.count.value(), 'budget': self.budget.value()})
        if items:
            self.submit_button.setEnabled(False)
            self.submit_page.setEnabled(False)
            self.save_preferences()
            self.batch_request = {'items': items, 'request_id': uuid.uuid4().hex,
                'start_after_save': not self.save_only.isChecked(),
                'batch_limit': self.batch_limit.value(), 'batch_label': self.batch_label.text().strip()}
            self.note.setText(f'正在固定图片与参数并保存 {len(items) * self.count.value()} 条任务，请等待保存回执。')
            self.send('enqueue', **self.batch_request)

    def update_estimate(self, *_):
        items = []
        for row in range(self.compose.rowCount()):
            if self.compose.item(row, 0) is not None:
                items.append({'images': ['image'], 'duration': self.duration.currentData(),
                    'aspect': self.aspect.currentText(), 'resolution': self.resolution.currentText(), 'count': self.count.value()})
        self.batch_estimate.setText(estimate_text(items, self.state))

    def start_queue(self):
        self.save_preferences()
        self.send('start')

    def save_preferences(self):
        values = {'concurrency': self.concurrent.value(), 'profiles': self.group.currentData() or [],
            'downloads_root': self.download_root.text().strip(), 'defaults': {
                'duration': self.duration.currentData(), 'aspect': self.aspect.currentText(),
                'resolution': self.resolution.currentText(), 'count': self.count.value(), 'budget': self.budget.value()},
            'manual_defaults': self.manual.options.values(), 'allow_general': self.allow_general.isChecked(),
            'auto_lane': self.auto_lane.isChecked(),
            'upload_retry_limit': self.upload_retries.value(), 'download_retry_limit': self.download_retries.value(),
            'cleanup_downloads': self.cleanup_downloads.isChecked()}
        self.send('preferences', value=values)

    def general_changed(self, checked):
        self.send('preferences', value={'allow_general': checked})
        self.manual.update_route()

    def lane_changed(self, key, widget):
        value = widget.currentData()
        self.pending_lanes[key] = value
        scope, identifier = key
        if value == 'automatic':
            self.send('lane', scope=scope, key=identifier, mode='automatic')
        else:
            self.send('lane', scope=scope, key=identifier, duration=value)

    def update_browsers(self):
        clients = self.state.get('clients', [])
        preferences = self.state.get('preferences', {})
        entries = {}
        for profile in self.group.currentData() or []:
            entries[('profile', profile)] = {'profile': profile}
        for profile in preferences.get('profile_lanes', {}):
            entries.setdefault(('profile', profile), {'profile': profile})
        for profile in preferences.get('profile_projects', {}):
            entries.setdefault(('profile', profile), {'profile': profile})
        for client in clients:
            entries[lane_key(client)] = client
        keys = list(entries)
        # Reuse editors while state updates; do not steal focus or reset edits.
        if getattr(self, '_browser_keys', None) != keys:
            self.browsers.setRowCount(0)
            self.lane_edits = {}
            self.browsers.setRowCount(len(keys))
            self._browser_keys = keys
            for row, key in enumerate(keys):
                combo = QtWidgets.QComboBox(self.browsers)
                combo.addItem('自动分配', 'automatic')
                combo.addItem('固定通用档', None)
                for seconds in DURATIONS: combo.addItem(f'固定 {seconds} 秒', seconds)
                combo.currentIndexChanged.connect(lambda _index, k=key, w=combo: self.lane_changed(k, w))
                self.browsers.setCellWidget(row, 3, combo)
                self.lane_edits[key] = combo
        for row, key in enumerate(keys):
            client = entries[key]
            if not client.get('id'):
                client = {**client, 'saved_project': preferences.get('profile_projects', {}).get(client.get('profile'))}
                restore_balance(client, preferences)
            scope, identifier = key
            stored = preferences.get(scope + '_lanes', {}).get(identifier)
            current_lane = stored
            if automatic_lane(client, preferences): stored = 'automatic'
            if key in self.pending_lanes and self.pending_lanes[key] == stored:
                self.pending_lanes.pop(key)
            value = self.pending_lanes.get(key, stored)
            combo = self.lane_edits[key]
            combo.setItemText(0, f'自动分配 · 当前 {current_lane} 秒' if current_lane else '自动分配')
            with QtCore.QSignalBlocker(combo): combo.setCurrentIndex(combo.findData(value))
            online = bool(client.get('id'))
            status = browser_status(client, preferences, self.state.get('jobs', []))
            credit_text = ((f'{client["credits"]:g}' + ('（估算）' if client.get('credits_estimated') else ''))
                           if client.get('credits') is not None else '未知（调度按 50 分排序）')
            hold = credit_hold(client, preferences)
            if hold and (not online or client.get('credits') is None):
                credit_text = f'{hold["balance"]:g}（冷却记录）'
            check = client.get('credit_check') or {}
            if check.get('state') == 'checking':
                credit_text += '\n检测中…'
            elif check.get('state') == 'success':
                checked = QtCore.QDateTime.fromSecsSinceEpoch(int(check['at'])).toString('HH:mm:ss')
                credit_text += '\n检测成功 ' + checked
            elif check.get('state') == 'failed':
                if client.get('credits') is not None: credit_text += '（上次）'
                credit_text += '\n检测失败：' + str(check.get('message') or '读取未完成')[:80]
            elif check.get('state') == 'deferred':
                credit_text += '\n暂未检测：执行中，完成后再检测'
            elif check.get('state') == 'cooldown':
                credit_text += '\n冷却中，未自动检测'
            elif check.get('state') == 'cached':
                credit_text += '\n上次检测缓存'
            if client.get('credits') is not None and client.get('credits_at'):
                lifetime = timing_value(preferences, 'credit_validity_seconds')
                if lifetime:
                    deadline = QtCore.QDateTime.fromSecsSinceEpoch(int(client['credits_at'] + lifetime)).toString('MM-dd HH:mm')
                    credit_text += '\n余额有效至 ' + deadline
                else: credit_text += '\n余额不过期'
            values = {0: client.get('profile') or ('浏览器 ' + identifier[:8]),
                      1: (client.get('account') or '待确认') if online else (str(client['account']) + '（上次）' if client.get('account') else '未连接'),
                      2: credit_text,
                      4: status['connection'], 5: status['project'], 6: status['state'], 7: status['reason']}
            for column, text in values.items():
                item = self.browsers.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem(); self.browsers.setItem(row, column, item)
                if item.text() != text: item.setText(text)
                item.setToolTip(text)
                if column == 2 and check.get('message'):
                    item.setToolTip(text + '\n' + str(check['message']))
                if column in (4, 5, 6): item.setForeground(QtGui.QColor(status['color']))
            self.browsers.item(row, 0).setData(QtCore.Qt.ItemDataRole.UserRole, {'key': scope + ':' + identifier,
                'browser': client.get('id'), 'profile': client.get('profile')})
        stamp = tuple((self.browsers.item(row, 7).text(), self.browsers.item(row, 2).text())
                      for row in range(self.browsers.rowCount()))
        if getattr(self, '_browser_reason_stamp', None) != stamp:
            self._browser_reason_stamp = stamp
            self.browsers.resizeRowsToContents()

    def browser_selections(self):
        return [self.browsers.item(index, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                for index in sorted({item.row() for item in self.browsers.selectedIndexes()}) if self.browsers.item(index, 0)]

    def browser_menu(self, position):
        index = self.browsers.indexAt(position)
        if not index.isValid(): return
        if index.row() not in {item.row() for item in self.browsers.selectedIndexes()}:
            self.browsers.clearSelection()
            self.browsers.selectRow(index.row())
        menu = QtWidgets.QMenu(self.browsers)
        menu.addAction('检测选中浏览器积分', self.detect_selected_credits)
        menu.addAction('标记选中账号不可用', lambda: self.account_state_action('mark_account_unavailable'))
        menu.addAction('重置选中账号状态并重新检测', lambda: self.account_state_action('reset_account_state'))
        menu.addAction('打开选中浏览器', self.open_selected_browser)
        menu.addAction('连接／安装扩展…', self.open_setup)
        menu.exec(self.browsers.viewport().mapToGlobal(position))

    def detect_selected_credits(self):
        if getattr(self.plugin, 'manual_credit_check_api', 0) != 1:
            self.note.setText('本次运行仍是旧服务；下次正常重启后可使用所选浏览器积分检测，现有任务保持不动。')
            return
        selected = self.browser_selections()
        if not selected:
            self.note.setText('请先选择要检测积分的浏览器。')
            return
        self.note.setText('正在检测所选浏览器账号和积分，结果显示在积分栏；未放行生成任务。')
        self.send('credits', keys=[value['key'] for value in selected], manual=True)

    def account_state_action(self, action):
        if getattr(self.plugin, 'account_state_api', 0) != 1:
            self.note.setText('下次正常重启主程序后可使用账号隔离和手动重置；当前任务保持不动。')
            return
        selected = self.browser_selections()
        if not selected:
            self.note.setText('请先选择需要处理的账号。')
            return
        self.send(action, keys=[value['key'] for value in selected])

    def open_selected_browser(self):
        rows = sorted({item.row() for item in self.browsers.selectedIndexes()})
        if rows: self.open_browser_row(rows[0])
        else:
            self.open_setup(); self.setup.begin()

    def open_browser_row(self, row):
        item = self.browsers.item(row, 0)
        if not item: return
        value = item.data(QtCore.Qt.ItemDataRole.UserRole) or {}
        if value.get('profile'):
            self.open_setup(value['profile']); self.setup.begin()
        elif value.get('browser'):
            self.send('open_browser', browser=value['browser'])

    def resume_selected_browsers(self):
        self.send('resume_browser', keys=[value['key'] for value in self.browser_selections()])

    def pick_download_root(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, '浏览器默认下载目录', self.download_root.text())
        if path: self.download_root.setText(path)

    def selected_ids(self):
        return [self.queue.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                for row in sorted({index.row() for index in self.queue.selectedIndexes()})
                if self.queue.item(row, 0) and self.queue.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)]

    def select_jobs(self, ids):
        self.queue_tools.search.clear(); self.queue_tools.status.setCurrentIndex(0)
        self.tabs.setCurrentIndex(0)
        for row in range(self.queue.rowCount()):
            item = self.queue.item(row, 0)
            if item and item.data(QtCore.Qt.ItemDataRole.UserRole) in ids:
                self.queue.selectRow(row); self.queue.scrollToItem(item); break

    def confirm_duplicate(self, ids):
        box = QtWidgets.QMessageBox(self); box.setWindowTitle('已有相同任务正在处理')
        box.setText('同一图片、提示词和参数已有任务等待或生成中。再次提交会新增生成次数并可能消耗积分。')
        view = box.addButton('查看原任务', QtWidgets.QMessageBox.ButtonRole.ActionRole)
        variant = box.addButton('仍然再生成一版', QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton('取消', QtWidgets.QMessageBox.ButtonRole.RejectRole); box.exec()
        if box.clickedButton() is view: self.select_jobs(ids)
        return box.clickedButton() is variant

    def selected_job(self):
        ids = self.selected_ids()
        return next((row for row in self.state.get('jobs', []) if row['id'] in ids), None)

    def open_directory(self):
        job = self.selected_job()
        if job: open_path(job['payload']['directory'])

    def open_output(self):
        job = self.selected_job()
        if job and Path(job['payload']['output']).is_file(): self.manual.recent.open_row(job)

    def retry_selected_downloads(self):
        ids = self.selected_ids()
        if not ids:
            self.note.setText('请先选中要恢复下载的任务，支持多选。'); return
        self.send('retry_download_selected', ids=ids)
        self.note.setText('已请求重试原视频下载；不会重新生成。正在下载的同一条不会重复启动。')

    def reset_selected_tasks(self):
        if self.reset_request: return
        ids = set(self.selected_ids())
        rows = [row for row in self.state.get('jobs', []) if row['id'] in ids]
        if not rows:
            self.note.setText('请先选中要完全重置的任务，支持多选。'); return
        risky = any(row.get('reset_risk') or submission_risk(row) for row in rows)
        if risky:
            answer = QtWidgets.QMessageBox.warning(self, '完全重置并重新生成',
                '选中项中有任务可能已经提交或扣费。完全重置会放弃当前结果的自动核对并重新生成，可能再次消耗积分。'
                '\n原网页任务可能继续运行；旧记录、已用预算和现有文件保留，新结果使用新的文件名。\n仍然完全重置并重新调度？',
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.reset_request = uuid.uuid4().hex
        self.reset_button.setEnabled(False)
        self.note.setText('正在解除原浏览器绑定并重置选中任务…')
        self.send('reset_tasks', request_id=self.reset_request, ids=[row['id'] for row in rows],
            versions={row['id']: {key: row[key] for key in ('attempt', 'browser', 'status')} for row in rows},
            confirmed_submitted=risky)

    def tasks_reset(self, result):
        if result.get('request_id') != self.reset_request: return
        self.reset_request = ''; self.reset_button.setEnabled(True)
        if not result.get('success'):
            self.note.setText('完全重置失败：' + result['error']); return
        message = f'已完全重置 {len(result["reset"])} 条，已解除原浏览器绑定，可由其他可用浏览器接单。'
        if result.get('held'): message += ' 安全暂停仍有效，请处理原因后继续队列。'
        if result['skipped']: message += ' 未重置：' + '；'.join(sorted({row['reason'] for row in result['skipped']}))
        self.note.setText(message)

    def delete_records(self, ids=None):
        if self.delete_request: return
        ids = self.selected_ids() if ids is None else ids
        rows = [row for row in self.state.get('jobs', []) if row['id'] in set(ids)]
        if not rows:
            self.note.setText('请先选择要删除的任务记录；支持多选。'); return
        box = QtWidgets.QMessageBox(self); box.setWindowTitle('删除 Flow 任务记录')
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setText(f'删除选中的 {len(rows)} 条任务记录？\n只删除队列和最近提交中的记录，不删除任务图片、成品或浏览器下载文件，也不会退回已用积分。\n正在生成、下载或保存的任务会保留，不会打断。此操作不可撤销。')
        unresolved = sum(row['status'] in RECORD_UNRESOLVED for row in rows)
        confirm = None
        if unresolved:
            confirm = QtWidgets.QCheckBox(f'已在原网页确认 {unresolved} 条待核对／下载任务的云端生成已结束，并放弃其恢复记录', box)
            box.setCheckBox(confirm)
            box.setInformativeText('不勾选则保留这些待核对记录；删除后不能再从软件重试其下载。')
        remove = box.addButton('删除记录', QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton('取消', QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel); box.setEscapeButton(cancel); box.exec()
        if box.clickedButton() is not remove: return
        self.delete_request = uuid.uuid4().hex
        self.delete_button.setEnabled(False)
        self.note.setText('正在删除记录；图片、成品和下载文件均保留。')
        self.send('delete_records', request_id=self.delete_request, ids=[row['id'] for row in rows],
                  confirmed_finished=bool(confirm and confirm.isChecked()))

    def records_deleted(self, result):
        if result.get('request_id') != self.delete_request: return
        self.delete_request = ''; self.delete_button.setEnabled(True)
        if not result.get('success'):
            self.note.setText('删除记录失败：' + result['error']); return
        self.note.setText(f'已删除 {len(result["deleted"])} 条记录；图片、成品和下载文件未删除。' +
            (f' 另有 {len(result["skipped"])} 条正在执行或尚未确认云端结束，已保留。' if result['skipped'] else ''))

    def queue_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction('只提交选中的等待项', lambda: self.send('submit_selected', ids=self.selected_ids()))
        menu.addAction('重试失败／仅重试已有结果下载', lambda: self.send('retry', ids=self.selected_ids()))
        menu.addAction('重试下载（不重新生成）', self.retry_selected_downloads)
        menu.addAction('完全重置并重新调度…', self.reset_selected_tasks)
        menu.addAction('打开任务目录', self.open_directory)
        menu.addAction('播放成品', self.open_output)
        menu.addAction('删除选中记录…', lambda: self.delete_records())
        menu.addAction('暂存下载并释放账号…', self.queue_tools.release)
        menu.addAction('原链接过期：更新原媒体地址…', self.queue_tools.repair_download_url)
        menu.addAction('打开对应浏览器', self.queue_tools.open_browser)
        menu.addAction('修改未提交项…', self.queue_tools.edit)
        menu.addAction('再做一版（恢复图片和参数）', lambda: self.manual.restore_job(self.selected_job()) if self.selected_job() else None)
        menu.addAction('采用成品', lambda: self.send('review', ids=self.selected_ids(), accepted=True))
        menu.addAction('标记待调整', lambda: self.send('review', ids=self.selected_ids(), accepted=False))
        menu.addAction('校验后清理下载副本', lambda: self.send('cleanup', ids=self.selected_ids()))
        job = self.selected_job()
        if job and job['status'] == 'uncertain':
            def regenerate():
                answer = QtWidgets.QMessageBox.question(self, '确认再次生成', '原提交结果尚未确认。再次生成可能重复消耗积分，仍然执行？')
                if answer == QtWidgets.QMessageBox.StandardButton.Yes: self.send('regenerate', ids=self.selected_ids())
            menu.addAction('确认放弃核对并重新生成…', regenerate)
        menu.exec(self.queue.viewport().mapToGlobal(point))

    def update_state(self, state):
        self.state = state
        if not self.preferences_loaded:
            preferences = state.get('preferences', {})
            self.concurrent.setValue(int(preferences.get('concurrency', 2)))
            self.download_root.setText(state.get('downloads_root', ''))
            defaults = preferences.get('defaults', {})
            duration_index = self.duration.findData(defaults.get('duration'))
            if duration_index >= 0: self.duration.setCurrentIndex(duration_index)
            self.aspect.setCurrentText(defaults.get('aspect', '9:16'))
            self.resolution.setCurrentText(defaults.get('resolution', '720p'))
            self.count.setValue(int(defaults.get('count', 1)))
            self.budget.setValue(int(defaults.get('budget', 0)))
            if not self.manual.dirty_keys:
                self.manual.loading = True
                self.manual.options.set_values(preferences.get('manual_defaults', {}))
                self.manual.loading = False
            from .transfer_policy import retry_limit
            self.upload_retries.setValue(retry_limit(preferences, 'upload'))
            self.download_retries.setValue(retry_limit(preferences, 'download'))
            self.cleanup_downloads.setChecked(preferences.get('cleanup_downloads', False))
            with QtCore.QSignalBlocker(self.allow_general):
                self.allow_general.setChecked(preferences.get('allow_general', True))
            with QtCore.QSignalBlocker(self.auto_lane):
                self.auto_lane.setChecked(preferences.get('auto_lane', True))
            for index in range(self.group.count()):
                if self.group.itemData(index) == preferences.get('profiles', []): self.group.setCurrentIndex(index)
            self.preferences_loaded = True
        jobs = state['jobs']
        scroll = self.queue.verticalScrollBar().value()
        self.queue_tools.render(jobs, STATUS)
        self.queue.verticalScrollBar().setValue(scroll)
        done = sum(row['status'] == 'completed' for row in jobs)
        activity = '安全暂停，需确认继续' if state.get('safety_paused') else ('仅执行已提交项；其余等待开始' if state['paused'] else '全局调度中')
        self.progress.setText(f'{activity} · 完成 {done}/{len(jobs)} · 已连接 {len(state["clients"])} 个浏览器')
        statuses = [browser_status(client, state.get('preferences', {}), jobs) for client in state['clients']]
        ready = sum(item['state'] == '可接单' for item in statuses)
        busy = sum(item['state'] == '执行中' for item in statuses)
        queued = [row for row in jobs if row['status'] == 'queued']
        reason = (queued[0].get('wait_reason') or queued[0].get('error') or '等待调度') if queued else ''
        active = sum(row['status'] in {'preparing', 'uploading', 'submitting', 'generating', 'downloading'} for row in jobs)
        limit = int(state.get('preferences', {}).get('concurrency', 2))
        attention = sum(item['state'] in {'需处理', '未就绪', '账号已隔离', '自动等待中'} for item in statuses)
        self.connection_summary.setText(f'账号并行 {active}/{limit} · 浏览器：已连接 {len(statuses)} · 可接单 {ready} · 执行中 {busy} · 等待／需处理 {attention}' +
            (f'\n排队原因：{reason}' if reason else '\n提交并保存成功后自动调度；不必再点开始。'))
        self.update_browsers()
        self.setup.apply_state(state)
        self.manual.apply_state(state)
        self.update_estimate()

    def closeEvent(self, event):
        self.setup.cancel_auto_retry()
        self.setup.waiting_profile = ''; self.setup.capture_timer.stop()
        if hasattr(self.manual.recent, 'preview_dialog'): self.manual.recent.preview_dialog.close()
        self.manual.flush_draft()
        super().closeEvent(event)
