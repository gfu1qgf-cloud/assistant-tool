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
from .browser_table import Column as BC, HEADERS as BROWSER_HEADERS, BrowserItem, row_color
from .status_ui import status_area
from .suspicious_download import output_path

STATUS = {'collected': '待统一提交', 'queued': '等待执行', 'preparing': '首次核对／更新快照', 'uploading': '上传／挂载参考图',
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
        if (getattr(plugin, 'ui_api_version', 1) != 4
                or not all(hasattr(plugin, name) for name in ('context', 'command'))):
            raise RuntimeError('Flow 源码已更新，本次运行仍是旧服务。请在当前任务结束后正常重启辅助工具，再使用新面板；现有队列不会重新提交。')
        super().__init__(parent)
        self.plugin, self.targets, self.state = plugin, [], {}
        self.delete_request = ''
        self.reset_request = ''
        self.setWindowTitle('Chrome 与 Flow 助手 · 浏览器与视频生成')
        self.resize(1450, 900)
        shell = QtWidgets.QHBoxLayout(self)
        shell.setSpacing(12)
        self._sidebar(shell)
        self.sections = QtWidgets.QStackedWidget(self)
        shell.addWidget(self.sections, 1)
        self.flow_page = QtWidgets.QWidget(self.sections)
        layout = QtWidgets.QVBoxLayout(self.flow_page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.note = QtWidgets.QLabel('准备就绪')
        self.note.setToolTip('选择任务和图片，填写或识别提示词，加入清单后统一提交；生成结果自动归档到任务目录。')
        self.connection_summary = QtWidgets.QLabel('浏览器：尚未连接', self)
        self.tabs = QtWidgets.QTabWidget(self.flow_page)
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)
        self._queue_page()
        self.unified_submission = True
        self.manual = ManualSubmitPanel(self)
        self._submit_page()
        self._browser_page()
        self._settings_page()
        self._chrome_page()
        self.sections.addWidget(self.flow_page)
        self.sidebar.currentRowChanged.connect(self.sections.setCurrentIndex)
        self.sidebar.setCurrentRow(1)
        status_row = QtWidgets.QHBoxLayout()
        status_row.addWidget(status_area(self.connection_summary, 3), 1)
        self.connection_button = self.button('浏览器连接／时长设置', lambda: self.show_flow_page(self.browser_page), status_row)
        self.stall_details_button=self.button('查看阻塞原因…',self.show_stall_details,status_row)
        self.stall_details_button.hide()
        layout.addLayout(status_row)
        from .mutex_monitor_ui import MutexMonitor
        self.ui_lock_monitor=MutexMonitor(self.flow_page)
        layout.addWidget(self.ui_lock_monitor)
        layout.addWidget(status_area(self.note, 1))

    def _sidebar(self, layout):
        rail = QtWidgets.QFrame(self)
        rail.setObjectName('workspaceSidebar')
        rail.setFixedWidth(156)
        contents = QtWidgets.QVBoxLayout(rail)
        contents.setContentsMargins(8, 18, 8, 8)
        title = QtWidgets.QLabel('工作区', rail)
        title.setContentsMargins(12, 0, 0, 10)
        title.setStyleSheet('font-weight: 600; color: palette(text);')
        contents.addWidget(title)
        self.sidebar = QtWidgets.QListWidget(rail)
        self.sidebar.setObjectName('workspaceNavigation')
        self.sidebar.setAccessibleName('Chrome 与 FLOW 插件切换')
        self.sidebar.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for text in ('Chrome', 'FLOW 插件'):
            item = QtWidgets.QListWidgetItem(text)
            item.setSizeHint(QtCore.QSize(132, 46))
            self.sidebar.addItem(item)
        contents.addWidget(self.sidebar, 1)
        rail.setStyleSheet('''
            QFrame#workspaceSidebar {background: palette(alternate-base); border: none; border-radius: 10px;}
            QListWidget#workspaceNavigation {background: transparent; border: none; outline: none; font-size: 14px;}
            QListWidget#workspaceNavigation::item {padding-left: 12px; margin: 3px 0; border-radius: 6px;}
            QListWidget#workspaceNavigation::item:selected {background: palette(highlight); color: palette(highlighted-text);}
            QListWidget#workspaceNavigation::item:hover:!selected {background: palette(midlight);}
        ''')
        layout.addWidget(rail)

    def _chrome_page(self):
        launcher = getattr(self.plugin, 'browser_launcher', None)
        page = getattr(launcher, 'dialog', None)
        self.chrome_page = page if isinstance(page, QtWidgets.QWidget) else None
        self.chrome_section = QtWidgets.QWidget(self.sections)
        layout = QtWidgets.QVBoxLayout(self.chrome_section)
        layout.setContentsMargins(0, 0, 0, 0)
        if self.chrome_page is None:
            placeholder = QtWidgets.QLabel('Chrome 管理尚未初始化，请正常重启辅助工具后重试。', self.chrome_section)
            placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(placeholder)
        else:
            self.chrome_page.setParent(self.chrome_section)
            self.chrome_page.setWindowFlags(QtCore.Qt.WindowType.Widget)
            layout.addWidget(self.chrome_page)
        self.sections.addWidget(self.chrome_section)

    def show_chrome(self):
        self.sidebar.setCurrentRow(0)
        self.sections.setCurrentWidget(self.chrome_section)

    def show_flow_page(self, page=None):
        self.sidebar.setCurrentRow(1)
        self.sections.setCurrentWidget(self.flow_page)
        if isinstance(page, int): self.tabs.setCurrentIndex(page)
        elif page is not None: self.tabs.setCurrentWidget(self.batch if page is self.manual else page)

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
        self.start_button = self.button('开始／继续等待项', self.start_queue, controls)
        self.button('暂停后续任务', lambda: self.send('pause'), controls)
        self.retry_now_button = self.button('立即重试', self.retry_selected_now, controls)
        self.retry_now_button.setToolTip('跳过选中任务的自动重试倒计时；保留重试次数、账号限制和原视频绑定。普通失败项沿用手动重试。')
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
        menu.addAction('强制删除选中记录…', lambda: self.delete_records(force=True))
        menu.addAction('打开任务目录', self.open_directory)
        menu.addAction('播放成品', self.open_output)
        self.queue_menu_button.setMenu(menu); controls.addWidget(self.queue_menu_button)
        controls.addStretch()
        layout.addLayout(controls)
        from .queue_ui import QueueTools
        self.queue_tools = QueueTools(self, page, layout)
        self.progress = QtWidgets.QLabel('尚无任务')
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
        layout.addWidget(status_area(self.progress, 1))
        layout.addWidget(status_area(self.queue_tools.summary, 2))
        self.tabs.addTab(page, '全局生成队列')

    def _submit_page(self):
        from .batch_workspace import BatchWorkspace
        self.batch=BatchWorkspace(self);self.submit_page=self.batch
        self.compose=self.batch.compose;self.task_selector=self.manual.picker.task_selector
        self.target=self.manual.picker.target;self.images=self.manual.picker.images
        self.duration=self.manual.options.duration;self.aspect=self.manual.options.aspect
        self.resolution=self.manual.options.resolution;self.count=self.manual.options.count;self.budget=self.manual.options.budget
        self.batch_label=self.batch.batch_label;self.batch_limit=self.manual.batch_limit
        self.batch_estimate=self.batch.estimate;self.save_only=self.batch.save_only;self.submit_button=self.manual.collect_submit
        self.tabs.addTab(self.batch,'添加与统一提交')

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
        row = QtWidgets.QHBoxLayout()
        self.close_low_credit_button = self.button('关闭所有积分不足浏览器',
            lambda: self.send('close_low_credit_browsers'), row)
        self.close_low_credit_button.setEnabled(False)
        self.close_low_credit_button.setToolTip('手动批量关闭已连接且确认低于10分的配置；生成、下载、待核对或余额未知／过期的会跳过。需要扩展1.1.34。')
        self.reset_browser_errors_button = self.button('强制重置所有异常',lambda:self.send('reset_browser_errors'),row)
        supported=getattr(self.plugin,'force_browser_reset_api',0)>=1
        self.reset_browser_errors_button.setEnabled(supported)
        self.reset_browser_errors_button.setToolTip('清除浏览器异常与恢复次数并重新检测；保留用户标记和未到期的积分刷新限制。正常任务继续，忙碌账号在空闲后重新检测。' if supported else '正常重启主程序后可使用强制重置。')
        row.addStretch()
        layout.addLayout(row)
        hint = QtWidgets.QLabel('默认自动安排时长：按队首任务将空闲的自动配置切到所需时长；固定分档仅接相匹配的任务。\n'
            '严格按提交顺序派发；优先从已连接浏览器快照中按配置顺序取用。需要专用账号时，可手动选“固定 6／8／10 秒”。\n'
            '选中浏览器后右键“检测选中浏览器积分”，可直接检查账号和积分；积分栏显示检测时间或失败原因。\n'
            '点击表头排序，再次点击切换升序／降序；配置名称与 Chrome 启动器一致。')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.allow_general = QtWidgets.QCheckBox('允许通用档浏览器补位（不使用其他专用时长档）', page)
        self.allow_general.setChecked(True)
        self.allow_general.toggled.connect(self.general_changed)
        layout.addWidget(self.allow_general)
        self.auto_lane = QtWidgets.QCheckBox('自动分配时长，空闲配置可按排队任务切换（推荐）', page)
        self.auto_lane.setChecked(True)
        self.auto_lane.toggled.connect(lambda checked: self.send('preferences', value={'auto_lane': checked}))
        layout.addWidget(self.auto_lane)
        self.browsers = self.table(BROWSER_HEADERS, page)
        for column, width in ((BC.PROFILE, 115), (BC.NAME, 150), (BC.ACCOUNT, 200),
                              (BC.CREDITS, 165), (BC.LANE, 175), (BC.CONNECTION, 100),
                              (BC.PROJECT, 100), (BC.STATE, 100), (BC.REASON, 260)):
            self.browsers.setColumnWidth(column, width)
        self.browsers.setSortingEnabled(True)
        self.browsers.sortItems(BC.PROFILE, QtCore.Qt.SortOrder.AscendingOrder)
        self.browsers.horizontalHeader().sortIndicatorChanged.connect(lambda *_: self.browsers.resizeRowsToContents())
        self.browsers.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.browsers.cellDoubleClicked.connect(lambda row, _column: self.open_browser_row(row))
        self.browsers.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.browsers.customContextMenuRequested.connect(self.browser_menu)
        self.browsers.setToolTip('右键浏览器行可检测所选浏览器积分；无需提交生成任务。')
        self.lane_edits = {}
        self.pending_lanes = {}
        layout.addWidget(self.browsers)
        legend = QtWidgets.QLabel('整行颜色：黄色＝积分耗尽　红色＝异常／需处理　绿色＝正常可用／执行中　蓝色＝高积分账号。右键“运行时操作”可测试激活与 F5；未连接时禁用。',page)
        legend.setWordWrap(True); layout.addWidget(legend)
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
        from .browser_manager import launcher_for
        launcher = getattr(launcher_for(self.plugin), 'dialog', None)
        from .scheduling_groups import SchedulingGroups
        self.scheduling_groups = SchedulingGroups(self, launcher)
        self.group.currentIndexChanged.connect(lambda: self.update_browsers())
        self.concurrent = QtWidgets.QSpinBox(page); self.concurrent.setRange(1, 8); self.concurrent.setValue(2)
        self.window_limit = QtWidgets.QSpinBox(page); self.window_limit.setRange(2, 64); self.window_limit.setValue(4)
        self.window_limit.setToolTip('Flow 自动调度最多保留的浏览器配置数，包含正在启动、执行和等待的窗口。不能小于并行账号数；降低后只会安全回收空闲窗口。')
        self.concurrent.valueChanged.connect(self.window_limit.setMinimum)
        self.download_root = QtWidgets.QLineEdit(page)
        root_row = QtWidgets.QWidget(page); root_layout = QtWidgets.QHBoxLayout(root_row); root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.download_root, 1)
        self.button('选择…', self.pick_download_root, root_layout)
        form.addRow('允许自动启动的浏览器配置组', self.group)
        form.addRow('最多并行账号', self.concurrent)
        form.addRow('最多打开的 Flow 浏览器', self.window_limit)
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
        self.show_flow_page(self.settings_page)

    def show_stall_details(self):
        text=self.state.get('scheduler_recovery',{}).get('details')
        if not text:return
        box=QtWidgets.QMessageBox(self)
        box.setWindowTitle('Flow 调度阻塞原因')
        box.setText('自动恢复未能恢复派发。具体配置及处理办法：')
        box.setDetailedText(text)
        box.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        box.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()
        self._stall_details=box

    def refresh_task_catalog(self):
        self.plugin.refresh_tasks()

    def set_targets(self, targets, *, preferred_directory=None, activate=True):
        self.targets = list(targets)
        self.task_selector.set_targets(targets, preferred_directory=preferred_directory)
        self.manual.picker.set_targets(targets, preferred_directory=preferred_directory)
        if activate: self.show_flow_page(self.manual)

    def load_images(self):
        self.manual.picker.load_images()

    def add_image(self,target,path):
        if self.compose.rowCount()!=len(self.batch.rows):self.batch.rows=self.batch.rows[:self.compose.rowCount()]
        return self.batch.add_image(target,path)

    def add_selected_images(self):
        target=self.target.currentData()
        if target:
            for item in self.images.selectedItems():self.add_image(target,item.data(QtCore.Qt.ItemDataRole.UserRole))

    def add_all_images(self):self.batch.add_all_images()
    def remove_compose(self):self.batch.remove_selected()

    def apply_prompt(self):
        text,ok=QtWidgets.QInputDialog.getMultiLineText(self,'应用提示词','应用到选中行（未选则全部）：')
        if ok:
            for row in self.batch.selected_indices() or range(self.compose.rowCount()):self.compose.item(row,2).setText(text)

    def enqueue(self):self.batch.enqueue()
    def update_estimate(self,*_):self.batch.update_controls()

    def start_queue(self):
        self.save_preferences()
        self.send('start')

    def save_preferences(self):
        values = {'concurrency': self.concurrent.value(), 'browser_window_limit':self.window_limit.value(), **self.scheduling_groups.values(),
            'downloads_root': self.download_root.text().strip(), 'defaults': dict(self.batch.defaults),
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
        from .runtime_close import low_balance
        self.close_low_credit_button.setEnabled(any(low_balance(c, self.state.get('preferences', {}))
            for c in self.state.get('clients', [])))
        # QTableWidget otherwise moves a row as soon as its sorted cell changes,
        # causing the remaining cells to be written into another account's row.
        sorting = self.browsers.isSortingEnabled()
        self.browsers.setSortingEnabled(False)
        try:
            self.update_browser_rows()
        finally:
            self.browsers.setSortingEnabled(sorting)
        stamp = tuple(tuple(self.browsers.item(row, column).text()
                            for column in (BC.PROFILE, BC.NAME, BC.CREDITS, BC.REASON))
                      for row in range(self.browsers.rowCount()))
        if getattr(self, '_browser_reason_stamp', None) != stamp:
            self._browser_reason_stamp = stamp
            self.browsers.resizeRowsToContents()

    def update_browser_rows(self):
        clients = self.state.get('clients', [])
        preferences = self.state.get('preferences', {})
        from .browser_manager import launcher_for
        launcher = getattr(launcher_for(self.plugin), 'dialog', None)
        names = {profile['directory']: profile.get('name') or profile['directory']
                 for profile in getattr(launcher, 'profiles', [])}
        entries = {}
        for profile in self.group.currentData() or []:
            entries[('profile', profile)] = {'profile': profile}
        for profile in preferences.get('profile_lanes', {}):
            entries.setdefault(('profile', profile), {'profile': profile})
        for profile in preferences.get('profile_projects', {}):
            entries.setdefault(('profile', profile), {'profile': profile})
        for profile in set(preferences.get('browser_slots', {})) | set(preferences.get('browser_launch_failures', {})):
            entries.setdefault(('profile', profile), {'profile': profile})
        for client in clients:
            entries[lane_key(client)] = client
        # Reconcile by identity rather than row number. Sorting, a reconnect or
        # a new group member must keep selections and in-progress lane edits.
        wanted = {scope + ':' + identifier for scope, identifier in entries}
        for row in reversed(range(self.browsers.rowCount())):
            value = self.browsers.item(row, BC.PROFILE).data(QtCore.Qt.ItemDataRole.UserRole)
            if value['key'] not in wanted:
                self.browsers.removeRow(row)
                self.lane_edits.pop(tuple(value['key'].split(':', 1)), None)
        rows = {self.browsers.item(row, BC.PROFILE).data(QtCore.Qt.ItemDataRole.UserRole)['key']: row
                for row in range(self.browsers.rowCount())}
        for key, client in entries.items():
            scope, identifier = key
            identity = scope + ':' + identifier
            row = rows.get(identity)
            if row is None:
                row = self.browsers.rowCount()
                self.browsers.insertRow(row)
                for column in BC:
                    self.browsers.setItem(row, column, BrowserItem())
                combo = QtWidgets.QComboBox(self.browsers)
                combo.addItem('自动分配', 'automatic')
                combo.addItem('固定通用档', None)
                for seconds in DURATIONS: combo.addItem(f'固定 {seconds} 秒', seconds)
                combo.currentIndexChanged.connect(lambda _index, k=key, w=combo: self.lane_changed(k, w))
                self.browsers.setCellWidget(row, BC.LANE, combo)
                self.lane_edits[key] = combo
            if not client.get('id'):
                client = {**client, 'saved_project': preferences.get('profile_projects', {}).get(client.get('profile'))}
                restore_balance(client, preferences)
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
                           if client.get('credits') is not None else '未知（待检测；参考 50 分）')
            from .high_credit import is_high
            if is_high(client, preferences): credit_text += '\n千分账号（≥100 分）'
            from .high_credit import record as high_record
            permission = high_record(client,preferences).get('permission') if is_high(client,preferences) else None
            hold = credit_hold(client, preferences)
            if hold and (not online or client.get('credits') is None):
                credit_text = f'{hold["balance"]:g}（冷却记录）'
            if permission in {'allow','deny'}: credit_text += '\n'+('已允许备用调度' if permission=='allow' else '已禁止调度')
            credit_number = hold['balance'] if hold and (not online or client.get('credits') is None) else client.get('credits')
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
            from .credit_refresh import record as refresh_record, deadline as refresh_deadline
            daily = refresh_record(client,preferences)
            if daily:
                credit_text += '\n页面每天 '+daily['daily_time']+' 刷新'+('（'+daily['timezone']+'）' if daily.get('timezone') else '')
            if client.get('credits') is not None and client.get('credits_at'):
                lifetime = timing_value(preferences, 'credit_validity_seconds')
                limit = client['credits_at'] + lifetime if lifetime else 0
                refresh = refresh_deadline(client,preferences)
                if refresh and client['credits_at'] < refresh: limit = min(limit,refresh) if limit else refresh
                if limit:
                    deadline = QtCore.QDateTime.fromSecsSinceEpoch(int(limit)).toString('MM-dd HH:mm')
                    credit_text += '\n余额有效至 ' + deadline
                else: credit_text += '\n余额不过期'
            values = {BC.PROFILE: client.get('profile') or ('浏览器 ' + identifier[:8]),
                      BC.NAME: names.get(client.get('profile'), '未读取到名称'),
                      BC.ACCOUNT: (client.get('account') or '待确认') if online else (str(client['account']) + '（上次）' if client.get('account') else '未连接'),
                      BC.CREDITS: credit_text, BC.LANE: combo.currentText(),
                      BC.CONNECTION: status['connection'], BC.PROJECT: status['project'],
                      BC.STATE: status['state'], BC.REASON: status['reason']}
            if online:
                from .foreground import description
                values[BC.REASON] += '\n' + description(client, preferences)
                if client.get('runtime_note'): values[BC.REASON] += '\n'+client['runtime_note']
                if client.get('native_window'):
                    native = client['native_window']
                    values[BC.REASON] += f'\nChrome 窗口 {native.get("chrome_window_id")} · HWND {native.get("hwnd")} · PID {native.get("pid")}'
            tint = row_color(client,preferences,status,self.state.get('jobs',[]))
            combo.setStyleSheet('QComboBox { background-color: '+tint+'; color: #202020; }' if tint else '')
            for column, text in values.items():
                item = self.browsers.item(row, column)
                number = credit_number if column == BC.CREDITS else (current_lane if value == 'automatic' else value)
                item.update_value(text, identity, number=number, numeric=column in (BC.CREDITS, BC.LANE))
                item.setBackground(QtGui.QColor(tint) if tint else QtGui.QBrush())
                if column == BC.CREDITS and check.get('message'):
                    item.setToolTip(text + '\n' + str(check['message']))
                if column in (BC.CONNECTION, BC.PROJECT, BC.STATE): item.setForeground(QtGui.QColor(status['color']))
            self.browsers.item(row, BC.PROFILE).setData(QtCore.Qt.ItemDataRole.UserRole, {'key': identity,
                'browser': client.get('id'), 'profile': client.get('profile')})

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
        self.high_credit_permission_menu(menu)
        menu.addAction('标记选中账号不可用', lambda: self.account_state_action('mark_account_unavailable'))
        menu.addAction('重置选中账号状态并重新检测', lambda: self.account_state_action('reset_account_state'))
        menu.addAction('打开选中浏览器', self.open_selected_browser)
        menu.addAction('连接／安装扩展…', self.open_setup)
        self.runtime_menu(menu)
        menu.exec(self.browsers.viewport().mapToGlobal(position))

    def high_credit_permission_menu(self, menu):
        from .high_credit import is_high, record
        from .image_affinity import cached_profile
        selected = self.browser_selections(); preferences = self.state.get('preferences', {})
        online = {c.get('id'):c for c in self.state.get('clients', [])}
        clients = [online.get(item.get('browser')) or cached_profile(item.get('profile'),preferences) for item in selected]
        submenu = menu.addMenu('千分账号使用权限')
        supported = getattr(self.plugin,'high_credit_permission_api',0) == 1
        submenu.setEnabled(supported and bool(clients) and all(is_high(c,preferences) for c in clients))
        group = QtGui.QActionGroup(submenu); group.setExclusive(True)
        for text,permission in (('允许使用（普通账号优先）','allow'),('禁止使用','deny'),('恢复自动询问','auto')):
            action = submenu.addAction(text); action.setCheckable(True); group.addAction(action)
            action.setChecked(bool(clients) and all(record(c,preferences).get('permission','auto')==permission for c in clients))
            action.triggered.connect(lambda _checked=False,p=permission:self.send('set_high_credit_permission',
                permission=p,keys=[item['key'] for item in self.browser_selections()]))
        submenu.setToolTip('选择后立即保存，重启后仍有效；不弹倒计时确认框。允许后仍优先用普通账号，禁止不取消已提交任务。')
        return submenu

    def runtime_menu(self, menu):
        runtime = menu.addMenu('运行时操作')
        selected = self.browser_selections()
        online = {client.get('id'):client for client in self.state.get('clients',[]) if client.get('id')}
        runtime.setEnabled(bool(selected) and all(item.get('browser') in online for item in selected))
        for text, operation in (('激活窗口（切到前台）','focus'),('F5 刷新原项目页面','refresh'),
                                ('检测当前积分','credits'),('读取页面状态','snapshot'),('关闭此配置浏览器','close')):
            action = runtime.addAction(text,lambda _checked=False, op=operation:self.send('browser_runtime',
                operation=op,keys=[item['key'] for item in self.browser_selections()]))
            if operation in {'focus','refresh'}:
                supported = bool(selected) and all('runtime_controls_v1' in online.get(item.get('browser'),{}).get('capabilities',[]) for item in selected)
                action.setEnabled(supported)
                if not supported: action.setToolTip('当前扩展尚未加载此操作支持；相关浏览器正常重启加载 1.1.33 后可用。')
            if operation == 'close':
                from .runtime_close import CAPABILITY as CLOSE_CAPABILITY
                supported = bool(selected) and all(CLOSE_CAPABILITY in online.get(item.get('browser'),{}).get('capabilities',[]) for item in selected)
                action.setEnabled(supported)
                action.setToolTip('仅关闭所选 Chrome 配置的窗口；有非本项目标签页时保留整个浏览器。先核对无操作、生成、下载或待确认结果。关闭后暂停自动调度，可通过“修正后恢复”恢复。' if supported else
                    '保护其他标签页需要桥接扩展 1.1.39；当前运行的扩展尚未加载此功能。')
                if not supported: action.setText('关闭此配置浏览器（需扩展 1.1.39）')
        runtime.setToolTipsVisible(True)
        runtime.setToolTip('只对当前已连接的所选浏览器执行；未连接时禁用。F5 和关闭会再次核对无生成、下载或待确认结果。')
        return runtime

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
        if action == 'retry_high_credit' and getattr(self.plugin, 'high_credit_api', 0) != 1:
            self.note.setText('下次正常启动主程序后可使用千分账号确认；当前任务继续运行。')
            return
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
        self.show_flow_page(0)
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
        if job and Path(output_path(job)).is_file(): self.manual.recent.open_row(job)

    def retry_selected_downloads(self):
        ids = self.selected_ids()
        if not ids:
            self.note.setText('请先选中要恢复下载的任务，支持多选。'); return
        self.send('retry_download_selected', ids=ids)
        self.note.setText('已请求重试原视频下载；不会重新生成。正在下载的同一条不会重复启动。')

    def retry_selected_now(self):
        from .retry_now import retry_kind
        ids = set(self.selected_ids())
        rows = [row for row in self.state.get('jobs', []) if row['id'] in ids]
        if not rows:
            self.note.setText('请先选中要立即重试的任务，支持多选。'); return
        automatic = [row for row in rows if retry_kind(row)]
        if automatic:
            if not getattr(self.plugin, 'retry_now_api', 0):
                self.note.setText('正在运行的 Flow 服务尚未加载立即重试功能；正常重启辅助工具后生效。'); return
            self.send('retry_now', ids=[row['id'] for row in automatic],
                versions={row['id']: {key: row[key] for key in ('attempt', 'browser', 'status')} for row in automatic})
        manual = [row['id'] for row in rows if row not in automatic]
        if manual: self.send('retry', ids=manual)
        self.note.setText('已请求立即重试选中项；正在执行的操作不会重复启动，账号限制和安全暂停仍有效。')

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

    def delete_records(self, ids=None, *, force=False):
        if force and getattr(self.plugin,'force_delete_api',0)!=1:
            self.note.setText('强制删除需要正常重启主程序后启用；本次运行仍是旧服务，记录保持不变。');return
        if self.delete_request: return
        ids = self.selected_ids() if ids is None else ids
        rows = [row for row in self.state.get('jobs', []) if row['id'] in set(ids)]
        if not rows:
            self.note.setText('请先选择要删除的任务记录；支持多选。'); return
        box = QtWidgets.QMessageBox(self); box.setWindowTitle('强制删除 Flow 任务记录' if force else '删除 Flow 任务记录')
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setText((f'强制删除选中的 {len(rows)} 条任务记录？\n无论错误、等待或执行状态，均从本地队列移除，停止软件对这些记录的后续调度和自动重试。\n'
            '已提交的网页生成、浏览器下载或正在保存的文件仍可能继续；记录不会恢复。图片、成品和下载文件保留，已用积分不退回。此操作不可撤销。') if force else
            f'删除选中的 {len(rows)} 条任务记录？\n只删除队列和最近提交中的记录，不删除任务图片、成品或浏览器下载文件，也不会退回已用积分。\n正在生成、下载或保存的任务会保留，不会打断。此操作不可撤销。')
        unresolved = sum(row['status'] in RECORD_UNRESOLVED for row in rows)
        confirm = None
        if unresolved and not force:
            confirm = QtWidgets.QCheckBox(f'已在原网页确认 {unresolved} 条待核对／下载任务的云端生成已结束，并放弃其恢复记录', box)
            box.setCheckBox(confirm)
            box.setInformativeText('不勾选则保留这些待核对记录；删除后不能再从软件重试其下载。')
        remove = box.addButton('强制删除记录' if force else '删除记录', QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton('取消', QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel); box.setEscapeButton(cancel); box.exec()
        if box.clickedButton() is not remove: return
        self.delete_request = uuid.uuid4().hex
        self.delete_button.setEnabled(False)
        self.note.setText('正在删除记录；图片、成品和下载文件均保留。')
        self.send('delete_records', request_id=self.delete_request, ids=[row['id'] for row in rows],
                  confirmed_finished=bool(confirm and confirm.isChecked()), **({'force':True} if force else {}))

    def records_deleted(self, result):
        if result.get('request_id') != self.delete_request: return
        self.delete_request = ''; self.delete_button.setEnabled(True)
        if not result.get('success'):
            self.note.setText('删除记录失败：' + result['error']); return
        self.note.setText(f'已删除 {len(result["deleted"])} 条记录；图片、成品和下载文件未删除。' +
            (f' 另有 {len(result["skipped"])} 条保留：'+'；'.join(sorted({r['reason'] for r in result['skipped']}))+
                '。需要移除本地记录时，可使用“强制删除选中记录”。' if result['skipped'] else ''))

    def queue_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction('立即重试（跳过自动等待）', self.retry_selected_now)
        menu.addAction('只提交选中的等待项', lambda: self.send('submit_selected', ids=self.selected_ids()))
        menu.addAction('重试失败／仅重试已有结果下载', lambda: self.send('retry', ids=self.selected_ids()))
        menu.addAction('重试下载（不重新生成）', self.retry_selected_downloads)
        menu.addAction('完全重置并重新调度…', self.reset_selected_tasks)
        menu.addAction('打开任务目录', self.open_directory)
        menu.addAction('播放成品', self.open_output)
        menu.addAction('复制图片固定编号（SHA-256）', self.queue_tools.copy_image_id)
        menu.addAction('删除选中记录…', lambda: self.delete_records())
        menu.addAction('强制删除选中记录…', lambda: self.delete_records(force=True))
        menu.addAction('暂存下载并释放账号…', self.queue_tools.release)
        choose_video = menu.addAction('选择 Flow 视频恢复下载…', self.queue_tools.choose_download_video)
        choose_video.setEnabled(bool(self.selected_job()))
        menu.addAction('粘贴视频页面链接，恢复原视频下载…', self.queue_tools.repair_download_url)
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
        self.ui_lock_monitor.apply(state.get('ui_lock_monitor',{}))
        collected = sum(r['status']=='collected' for r in state.get('jobs', []))
        self.start_button.setText('统一提交／继续等待项' if collected else '开始／继续等待项')
        collecting = state.get('preferences', {}).get('collect_before_submit', True)
        self.batch.update_controls()
        report = state.get('runtime_close_batch', {})
        stamp = (report.get('id'), report.get('message'), report.get('detail'))
        if report.get('id') and stamp != getattr(self, '_runtime_close_report', None):
            self._runtime_close_report = stamp
            self.note.setText(report['message'])
            self.note.setToolTip(report.get('detail') or report['message'])
        reset_report=state.get('browser_error_reset',{})
        if reset_report.get('id') and reset_report['id']!=getattr(self,'_browser_error_reset_id',None):
            self._browser_error_reset_id=reset_report['id']
            self.note.setText(reset_report['message'])
            self.note.setToolTip('正在进行的上传、生成、下载和待核对结果保留；忙碌浏览器在空闲后重新检测。')
        if not self.preferences_loaded:
            preferences = state.get('preferences', {})
            self.concurrent.setValue(int(preferences.get('concurrency', 2)))
            self.window_limit.setValue(preferences.get('browser_window_limit',min(8,2*self.concurrent.value())))
            self.download_root.setText(state.get('downloads_root', ''))
            defaults = preferences.get('defaults', {})
            self.batch.defaults.update({key:defaults[key] for key in ('aspect','resolution','count','budget') if key in defaults})
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
            self.preferences_loaded = True
            self.scheduling_groups.load(preferences)
        jobs = state['jobs']
        scroll = self.queue.verticalScrollBar().value()
        self.queue_tools.render(jobs, STATUS)
        self.queue.verticalScrollBar().setValue(scroll)
        done = sum(row['status'] == 'completed' for row in jobs)
        activity = '安全暂停，需确认继续' if state.get('safety_paused') else ('仅执行已提交项；其余等待开始' if state['paused'] else '全局调度中')
        statuses = [browser_status(client, state.get('preferences', {}), jobs) for client in state['clients']]
        ready = sum(item['state'] == '可接单' for item in statuses)
        busy = sum(item['state'] == '执行中' for item in statuses)
        queued = [row for row in jobs if row['status'] == 'queued']
        reason = (queued[0].get('wait_reason') or queued[0].get('error') or '等待调度') if queued else ''
        active = sum(row['status'] in {'preparing', 'uploading', 'submitting', 'generating', 'downloading'} for row in jobs)
        self.progress.setText(f'{activity} · 待统一提交 {collected} · 排队 {len(queued)} · 执行 {active} · 完成 {done}/{len(jobs)} · 已连接 {len(state["clients"])} 个浏览器')
        limit = int(state.get('preferences', {}).get('concurrency', 2))
        attention = sum(item['state'] in {'需处理', '未就绪', '账号已隔离', '自动等待中', '自动恢复中', '恢复失败／已跳过', '积分不足当前任务', '原结果待核对', '原结果待下载'} for item in statuses)
        from .credit_packs import active_count
        summary = (f'账号并行 {active_count(jobs)}/{limit} · 排队 {len(queued)} · 浏览器：已连接 {len(statuses)} · 可接单 {ready} · 执行中 {busy} · 等待／需处理 {attention}' +
            (f'\n排队原因：{reason}' if reason else f'\n已收集 {collected} 条记录，点击“统一提交”后按同图积分分包执行。' if collected else '\n提交并保存成功后自动调度；不必再点开始。'))
        capacity = state.get('browser_capacity', {})
        if capacity.get('message') and queued and not state.get('scheduler_recovery',{}).get('message'):
            summary += '\n' + capacity['message']
        recovery=state.get('scheduler_recovery',{})
        if recovery.get('message'):summary+='\n'+recovery['message']
        self.stall_details_button.setVisible(bool(recovery.get('attention')))
        self.connection_summary.setText(summary)
        self.update_browsers()
        self.setup.apply_state(state)
        self.manual.apply_state(state)
        self.batch.apply_state(state)
        self.update_estimate()

    def closeEvent(self, event):
        if self.ui_lock_monitor.details:self.ui_lock_monitor.details.close()
        self.setup.cancel_auto_retry()
        self.setup.waiting_profile = ''; self.setup.capture_timer.stop()
        if hasattr(self.manual.recent, 'preview_dialog'): self.manual.recent.preview_dialog.close()
        self.batch.flush()
        self.manual.flush_draft()
        super().closeEvent(event)
