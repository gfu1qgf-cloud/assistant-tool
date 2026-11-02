"""Bulk-first oral task queue: row selection, contextual actions, compact setup."""
from functools import wraps

from qt_compat import QtCore, QtGui, QtWidgets
from .tasks import spreadsheet_id, validate_payload

STATES = {'draft': '待提交', 'failed': '未提交', 'sending': '待核对', 'uncertain': '待核对', 'submitted': '已提交'}


def guarded(function):
    @wraps(function)
    def call(self, *_args, **kwargs):
        try: return function(self, **kwargs)
        except Exception as error: self.status.setText(str(error))
    return call


class AssistantDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin, self.jobs, self._jobs_by_id, self._ready = plugin, [], {}, set()
        self._loaded = self._active = False
        self.setWindowTitle('AI Agent 助手'); self.resize(1120, 730)
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget(self); layout.addWidget(tabs)
        oral = QtWidgets.QWidget(tabs); tabs.addTab(oral, '口播')
        body = QtWidgets.QVBoxLayout(oral)
        settings = plugin.store.settings()
        self.actions = {}
        for key, title, slot in (
            ('import_selected', '导入主界面所选任务', self.import_selected),
            ('import_all', '导入当前列表的全部口播任务', self.import_all),
            ('import_version', '作为新版本导入所选任务…', self.import_version),
            ('submit', '提交所选可提交任务', self.submit),
            ('copy', '复制所选任务表格', self.copy_rows),
            ('check', '核对所选提交结果', self.check_result),
            ('remove', '移除所选草稿…', self.remove_drafts),
            ('select_all', '全选任务\tCtrl+A', self.select_all),
            ('select_ready', '只选当前列表可提交任务', self.select_ready),
            ('select_uncertain', '只选待核对任务', self.select_uncertain),
            ('clear', '取消选择', self.clear_selection),
            ('save_target', '保存表格设置', self.save_target),
            ('open_sheet', '打开目标表格', self.open_sheet),
            ('account', 'Google 账号…', self.open_accounts),
        ):
            action = QtGui.QAction(title, self); action.setObjectName('agent_' + key)
            action.triggered.connect(slot); self.actions[key] = action
        toolbar = QtWidgets.QHBoxLayout()
        self.import_button = QtWidgets.QToolButton(oral)
        self.import_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.import_button.setDefaultAction(self.actions['import_selected']); self.import_button.setText('导入任务')
        imports = QtWidgets.QMenu(self.import_button)
        for key in ('import_selected', 'import_all', 'import_version'): imports.addAction(self.actions[key])
        self.import_button.setMenu(imports); toolbar.addWidget(self.import_button)
        self.send = QtWidgets.QPushButton('提交所选（0）', oral); self.send.clicked.connect(self.submit)
        toolbar.addWidget(self.send)
        toolbar.addStretch(1)
        self.target_summary = QtWidgets.QLabel(oral); toolbar.addWidget(self.target_summary)
        self.setup_button = QtWidgets.QToolButton(oral); self.setup_button.setText('表格与账号设置')
        self.setup_button.setCheckable(True); toolbar.addWidget(self.setup_button)
        body.addLayout(toolbar)
        self.setup_panel = QtWidgets.QWidget(oral)
        panel = QtWidgets.QFormLayout(self.setup_panel); panel.setContentsMargins(0, 0, 0, 0)
        self.url = QtWidgets.QLineEdit(settings.get('url', ''), self.setup_panel)
        self.url.setPlaceholderText('首次粘贴目标 Google 表格链接；失去焦点后保存')
        self.sheet = QtWidgets.QLineEdit(settings.get('sheet', '口播表格'), self.setup_panel)
        panel.addRow('目标 Google 表格', self.url); panel.addRow('口播分页名称', self.sheet)
        self.chinese_source = QtWidgets.QCheckBox('缺少中文列时，我确认“完整任务名称”就是完整中文台词', self.setup_panel)
        self.chinese_source.setToolTip('只影响之后导入的任务；没有确认的任务仍需校对，不会自动提交。')
        self.chinese_source.setChecked(settings.get('use_task_name_chinese', False)); panel.addRow(self.chinese_source)
        setup_actions = QtWidgets.QHBoxLayout()
        for key in ('open_sheet', 'account'):
            button = QtWidgets.QToolButton(self.setup_panel); button.setDefaultAction(self.actions[key]); setup_actions.addWidget(button)
        setup_actions.addStretch(1); panel.addRow(setup_actions)
        self.auth = QtWidgets.QLabel(self.setup_panel); self.auth.setWordWrap(True); panel.addRow(self.auth)
        body.addWidget(self.setup_panel)
        self.setup_button.toggled.connect(self.setup_panel.setVisible)
        self.setup_button.setChecked(not bool(settings.get('url')))
        self.setup_panel.setVisible(self.setup_button.isChecked())
        self.url.editingFinished.connect(self.save_target); self.sheet.editingFinished.connect(self.save_target)
        self.chinese_source.toggled.connect(self.save_target)
        self.hint = QtWidgets.QLabel('Ctrl+A 全选 · Shift 连选 · Ctrl 多选 · 右键复制、核对或移除草稿 · 双击校对正文。外文提交时自动约10秒分段。', oral)
        self.hint.setWordWrap(True); body.addWidget(self.hint)
        self.table = QtWidgets.QTableWidget(0, 8, oral)
        self.table.setHorizontalHeaderLabels(['任务编号', '任务名称', '语言', '口播原文', '中文台词', '素材链接', '状态', '说明'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        for col, width in enumerate((85, 150, 60, 200, 200, 110, 75, 160)): self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().setSectionResizeMode(7, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.cellDoubleClicked.connect(self.edit_cell)
        self.table.itemSelectionChanged.connect(self.update_selection)
        self.actions['copy'].setShortcut(QtGui.QKeySequence.StandardKey.Copy)
        self.actions['copy'].setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.table.addAction(self.actions['copy'])
        body.addWidget(self.table, 1)
        bottom = QtWidgets.QHBoxLayout()
        self.selection_summary = QtWidgets.QLabel(oral); bottom.addWidget(self.selection_summary, 1)
        self.cancel = QtWidgets.QPushButton('取消当前操作', oral); self.cancel.clicked.connect(self.cancel_job)
        bottom.addWidget(self.cancel); body.addLayout(bottom)
        self.status = QtWidgets.QLabel('先导入任务，再提交所选行；不需要逐条勾选。', oral)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText); self.status.setWordWrap(True); body.addWidget(self.status)
        self.update_target_summary(); self.reload(); self.refresh_auth()

    def target(self):
        identifier = spreadsheet_id(self.url.text())
        name = self.sheet.text().strip()
        if not name: raise ValueError('请填写口播分页名称。')
        return {'url': 'https://docs.google.com/spreadsheets/d/' + identifier + '/edit', 'sheet': name,
                'use_task_name_chinese': self.chinese_source.isChecked()}

    def update_target_summary(self):
        self.target_summary.setText('目标：' + (self.sheet.text().strip() if self.url.text().strip() else '尚未设置'))

    @guarded
    def save_target(self):
        self.plugin.store.save_settings(self.target()); self.update_target_summary()
        self.status.setText('表格设置已保存。选择任务后可直接提交。')

    @guarded
    def open_sheet(self):
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.target()['url'])): raise ValueError('无法打开默认浏览器。')

    @guarded
    def open_accounts(self): self.plugin.context.open_google_account_manager()

    def refresh_auth(self):
        try:
            from model.GoogleAccounts import GoogleAccountManager
            manager = GoogleAccountManager(); manager.integrate_legacy(); state = manager.snapshot()
            key = state['bindings'].get('ai_agent_assistant.sheets')
            item = next((item for item in state['accounts'] if item['id'] == key), None)
            self.auth.setText('本插件表格账号：' + (item.get('label') or item['email']) if item else '尚未分配 AI Agent 表格账号，请点“Google 账号”。也可先导入、校对和右键复制。')
        except Exception as error: self.auth.setText(str(error))

    def _selected_ids(self):
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows(0))
        return [self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) for row in rows if self.table.item(row, 0)]

    def selected_ids(self):
        ids = self._selected_ids()
        if not ids: raise ValueError('请选择任务行；Ctrl+A 可全选，Shift 可连续多选。')
        return ids

    def _select_ids(self, ids):
        ids = set(ids)
        selection = QtCore.QItemSelection()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) in ids:
                selection.select(self.table.model().index(row, 0), self.table.model().index(row, self.table.columnCount() - 1))
        flags = QtCore.QItemSelectionModel.SelectionFlag
        self.table.selectionModel().select(selection, flags.ClearAndSelect | flags.Rows)
        self.update_selection()

    @guarded
    def reload(self, select_ids=None):
        previous = set(self._selected_ids()) if self._loaded else None
        jobs = self.plugin.store.jobs()  # Read first: a lost volume must not clear the current view.
        self.jobs = jobs; self._jobs_by_id = {job['id']: job for job in jobs}; self._ready = set()
        blocker = QtCore.QSignalBlocker(self.table)
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            payload = job['payload']
            try: validate_payload(payload); problem = ''
            except ValueError as error: problem = str(error)
            if job['state'] in {'draft', 'failed'} and not problem: self._ready.add(job['id'])
            texts = [payload['task_id'], payload['title'], payload['language'], payload['spoken'], payload['chinese'],
                     payload['reference'], STATES[job['state']], problem or job['detail']]
            for col, text in enumerate(texts):
                item = QtWidgets.QTableWidgetItem(text); item.setToolTip(text)
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                if col == 0: item.setData(QtCore.Qt.ItemDataRole.UserRole, job['id'])
                if col == 4: item.setToolTip(payload.get('chinese_source', '') + '\n\n' + text)
                self.table.setItem(row, col, item)
        if select_ids is not None: chosen = set(select_ids) & self._ready
        elif previous is None: chosen = self._ready
        else: chosen = previous
        self._select_ids(chosen); self._loaded = True
        del blocker
        self.busy(self.plugin.handle is not None)

    def update_selection(self):
        ids = self._selected_ids(); ready = len(set(ids) & self._ready)
        self.selection_summary.setText(f'已选 {len(ids)} / 共 {len(self.jobs)} 条 · 可提交 {ready} 条')
        self.send.setText(f'提交所选（{ready}）')
        self.send.setToolTip('只提交所选行中资料完整的草稿；已提交、待核对和资料不全的行不重发。')
        self.send.setEnabled(not self._active and ready > 0)
        for key in ('submit', 'copy', 'check', 'remove', 'select_all', 'select_ready', 'select_uncertain', 'clear'):
            enabled = bool(ids) if key in {'copy', 'check', 'clear'} else bool(self.jobs)
            if key == 'submit': enabled = ready > 0
            if key == 'remove': enabled = any(self._jobs_by_id[key]['state'] in {'draft', 'failed'} for key in ids if key in self._jobs_by_id)
            self.actions[key].setEnabled(not self._active and enabled)

    def busy(self, active):
        self._active = active
        for widget in (self.import_button, self.table, self.setup_panel, self.setup_button): widget.setEnabled(not active)
        for key in ('import_selected', 'import_all', 'import_version', 'save_target', 'account'): self.actions[key].setEnabled(not active)
        self.cancel.setEnabled(active); self.cancel.setVisible(active); self.update_selection()

    def select_all(self): self.table.selectAll()
    def select_ready(self): self._select_ids(self._ready)
    def select_uncertain(self): self._select_ids(job['id'] for job in self.jobs if job['state'] in {'sending', 'uncertain'})
    def clear_selection(self): self.table.clearSelection()

    def create_context_menu(self):
        self.update_selection()
        menu = QtWidgets.QMenu(self)
        for key in ('submit', 'copy', 'check'): menu.addAction(self.actions[key])
        menu.addSeparator()
        for key in ('select_all', 'select_ready', 'select_uncertain', 'clear'): menu.addAction(self.actions[key])
        menu.addSeparator(); menu.addAction(self.actions['remove'])
        settings = menu.addMenu('表格与账号')
        for key in ('open_sheet', 'account', 'save_target'): settings.addAction(self.actions[key])
        return menu

    def show_context_menu(self, pos):
        index = self.table.indexAt(pos)
        if index.isValid() and not self.table.selectionModel().isRowSelected(index.row(), QtCore.QModelIndex()):
            self.table.selectRow(index.row())
        menu = self.create_context_menu()
        menu.exec(self.table.viewport().mapToGlobal(pos)); menu.deleteLater()

    @guarded
    def import_selected(self): self.plugin.import_rows(self.plugin.context.selected_task_rows())

    @guarded
    def import_all(self): self.plugin.import_targets(self.plugin.context.current_task_targets())

    @guarded
    def import_version(self):
        answer = QtWidgets.QMessageBox.question(self, '另建新版本', '将主界面所选任务另建一个版本？原记录保留；新版本提交后会作为新的 Agent 任务。')
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.plugin.import_rows(self.plugin.context.selected_task_rows(), new_version=True)

    @guarded
    def submit(self):
        ids = [key for key in self.selected_ids() if key in self._ready]
        if not ids: raise ValueError('所选行没有可提交任务。请先校对资料；已提交或待核对记录可右键核对，不会自动重发。')
        self.plugin.submit(self.target(), ids, 'submit')

    @guarded
    def check_result(self): self.plugin.submit(self.target(), self.selected_ids(), 'check')

    @guarded
    def copy_rows(self): self.plugin.copy_tasks(self.selected_ids())

    @guarded
    def remove_drafts(self):
        ids = self.selected_ids()
        removable = [key for key in ids if self._jobs_by_id[key]['state'] in {'draft', 'failed'}]
        if not removable: raise ValueError('所选行没有可移除的草稿。已提交和待核对记录保留。')
        answer = QtWidgets.QMessageBox.question(self, '移除本地草稿', f'移除所选的 {len(removable)} 条本地草稿？已提交和待核对记录保留，不删除云端数据。')
        if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.plugin.store.remove_drafts(removable); self.reload()
        self.status.setText(f'已移除 {len(removable)} 条本地草稿，云端数据未删除。')

    @guarded
    def cancel_job(self):
        if self.plugin.handle: self.plugin.handle.requestInterruption(); self.status.setText('已请求取消，等待当前有超时限制的网络步骤结束。')

    def edit_cell(self, row, col):
        try:
            key = {2: 'language', 3: 'spoken', 4: 'chinese', 5: 'reference'}.get(col)
            if not key: return
            identifier = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            job = self._jobs_by_id[identifier]
            if job['state'] not in {'draft', 'failed'}: raise ValueError('已提交或待核对任务不可改写；请明确导入新版本。')
            text, accepted = QtWidgets.QInputDialog.getMultiLineText(self, '校对待提交内容', self.table.horizontalHeaderItem(col).text(), job['payload'][key])
            if accepted: self.plugin.store.edit(job['id'], key, text); self.reload()
        except Exception as error: self.status.setText(str(error))
