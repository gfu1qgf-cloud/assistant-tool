"""Bulk-first oral task queue: row selection, contextual actions, compact setup."""
from functools import wraps
import html

from qt_compat import QtCore, QtGui, QtWidgets
from .tasks import spreadsheet_id, validate_payload, fingerprint
from .preparation import prepare_payload
from .sheet_profiles import task_kind
from .review import review_state
from .receipt_monitor import poll_minutes

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
        self._confirm, self._blocked, self._reference_fixes = set(), {}, set()
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
            ('receive', '立即检查并收取成品', self.receive_results),
            ('receive_partial', '提前收取所选任务已有片段…', self.receive_partial_results),
            ('retry_download', '重试所选任务下载', self.retry_downloads),
            ('recollect_download', '重新收取所选成品…', self.recollect_downloads),
            ('open_result', '打开原任务成品目录', self.open_result),
            ('remove', '移除所选本地记录…', self.remove_drafts),
            ('clone', '复制所选任务为新草稿…', self.clone_drafts),
            ('select_all', '全选任务\tCtrl+A', self.select_all),
            ('select_ready', '只选当前列表可提交任务', self.select_ready),
            ('select_uncertain', '只选待核对任务', self.select_uncertain),
            ('clear', '取消选择', self.clear_selection),
            ('save_target', '保存表格设置', self.save_target),
            ('open_sheet', '打开目标表格', self.open_sheet),
            ('account', 'Google 账号…', self.open_accounts),
            ('assign', '批量设置执行 Agent…', self.assign_agent),
            ('user_requirements', '批量设置用户需求…', self.edit_requirements),
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
        self.default_agent = QtWidgets.QLineEdit(settings.get('default_agent', ''), self.setup_panel)
        self.default_agent.setPlaceholderText('与表格中 Agent 名称完全一致；只填充未指定的草稿')
        panel.addRow('默认执行 Agent', self.default_agent)
        self.default_agent.editingFinished.connect(self.save_target)
        self.chinese_source = QtWidgets.QCheckBox('缺少中文列时，我确认“完整任务名称”就是完整中文台词', self.setup_panel)
        self.chinese_source.setToolTip('此开关用于之后导入的任务；已有草稿可点提交一次批量确认，无需重新导入。不会自动确认。')
        self.chinese_source.setChecked(settings.get('use_task_name_chinese', False)); panel.addRow(self.chinese_source)
        polling = QtWidgets.QHBoxLayout()
        self.auto_download = QtWidgets.QCheckBox('自动巡查并收取（关闭助手仍继续）', self.setup_panel)
        self.auto_download.setChecked(settings.get('auto_download', True)); polling.addWidget(self.auto_download)
        polling.addWidget(QtWidgets.QLabel('间隔', self.setup_panel))
        self.poll_interval = QtWidgets.QSpinBox(self.setup_panel); self.poll_interval.setRange(1,1440)
        self.poll_interval.setKeyboardTracking(False)
        self.poll_interval.setSuffix(' 分钟'); self.poll_interval.setValue(poll_minutes(settings))
        self.poll_interval.setToolTip('默认30分钟。关闭自动巡查后仍可右键“检查并收取 Agent 成品”。已在收取的文件分批接续，不等待下次巡查。')
        polling.addWidget(self.poll_interval); polling.addStretch(1); panel.addRow(polling)
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
        self.auto_download.toggled.connect(self.save_auto_download)
        self.poll_interval.valueChanged.connect(self.save_poll_interval)
        self.hint = QtWidgets.QLabel('Ctrl+A 全选 · Shift 连选 · Ctrl 多选 · 右键复制、核对或管理记录 · 双击校对。外文约10秒分段，本地图片上传前须确认。', oral)
        self.hint.setWordWrap(True); body.addWidget(self.hint)
        self.table = QtWidgets.QTableWidget(0, 10, oral)
        self.table.setHorizontalHeaderLabels(['任务编号', '任务名称', '语言', '口播原文', '中文台词', '素材（链接／本地图片）', '状态', '说明', '执行 Agent', '用户需求'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        for col, width in enumerate((85, 150, 60, 200, 200, 110, 75, 160)): self.table.setColumnWidth(col, width)
        self.table.setColumnWidth(8, 110); self.table.setColumnWidth(9, 180)
        self.table.horizontalHeader().moveSection(8, 2)
        self.table.horizontalHeader().moveSection(9, 3)
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
        self.receipt_status = QtWidgets.QLabel(plugin.receipts.status, oral)
        self.receipt_status.setTextFormat(QtCore.Qt.TextFormat.PlainText); self.receipt_status.setWordWrap(True)
        body.addWidget(self.receipt_status)
        from .image_ui import ImageJobsPage
        self.image_page = ImageJobsPage(plugin, tabs); tabs.addTab(self.image_page, '图片转动画')
        self.update_target_summary(); self.reload(); self.refresh_auth()

    def target(self):
        identifier = spreadsheet_id(self.url.text())
        name = self.sheet.text().strip()
        if not name: raise ValueError('请填写口播分页名称。')
        return {'url': 'https://docs.google.com/spreadsheets/d/' + identifier + '/edit', 'sheet': name,
                'use_task_name_chinese': self.chinese_source.isChecked(), 'auto_download': self.auto_download.isChecked(),
                'default_agent': self.default_agent.text().strip(),
                'poll_minutes': self.poll_interval.value(),
                'upload_folder': self.plugin.store.settings().get('upload_folder', '')}

    def update_target_summary(self):
        self.target_summary.setText('目标：' + (self.sheet.text().strip() if self.url.text().strip() else '尚未设置'))

    @guarded
    def save_target(self):
        self.plugin.store.save_settings(self.target()); self.update_target_summary()
        self.plugin.receipts.configure()
        self.status.setText('表格设置已保存。选择任务后可直接提交。')
        if not self.auto_download.isChecked() and self.plugin.receipts.handle is not None and not self.plugin.receipts.manual_notice:
            self.plugin.receipts.handle.requestInterruption()

    @guarded
    def save_auto_download(self):
        settings = self.plugin.store.settings()
        settings['auto_download'] = self.auto_download.isChecked()
        self.plugin.store.save_settings(settings)
        self.plugin.receipts.configure()
        if not self.auto_download.isChecked():
            if self.plugin.receipts.handle is not None and not self.plugin.receipts.manual_notice: self.plugin.receipts.handle.requestInterruption()
            self.plugin.receipts.show_status('自动巡查已关闭；手动检查仍可用，手动收取不会因此取消。')
        else: self.plugin.receipts.show_status('自动收取已开启，下次检查自动处理；也可右键立即收取。')

    @guarded
    def save_poll_interval(self):
        settings=self.plugin.store.settings(); settings['poll_minutes']=self.poll_interval.value()
        self.plugin.store.save_settings(settings); self.plugin.receipts.configure()
        self.plugin.receipts.show_status(f'巡查间隔已设为{self.poll_interval.value()}分钟；需要时可右键立即检查。')

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
            download_key = state['bindings'].get('ai_agent_assistant.downloads')
            download = next((entry for entry in state['accounts'] if entry['id'] == download_key), None)
            label = (download.get('label') or download['email']) if download else '尚未单独分配；首次优先复用本插件已具有Drive权限的表格账号'
            self.auth.setText(self.auth.text() + '\n成品下载账号：' + label)
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
        jobs = [job for job in self.plugin.store.jobs(limit=100000) if task_kind(job['payload']) == 'oral']
        deliveries = self.plugin.store.delivery_statuses()
        reviews = self.plugin.store.ai_reviews()
        self.jobs = jobs; self._jobs_by_id = {job['id']: job for job in jobs}; self._ready = set()
        self._confirm, self._blocked, self._reference_fixes = set(), {}, set()
        blocker = QtCore.QSignalBlocker(self.table)
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            payload = job['payload']
            candidate = prepare_payload(payload)
            needs_confirm = not candidate.get('chinese_confirmed')
            try: validate_payload(dict(candidate, chinese_confirmed=True), allow_local_references=True); problem = ''
            except ValueError as error: problem = str(error)
            editable = job['state'] in {'draft', 'failed'}
            if editable:
                if problem: self._blocked[job['id']] = problem
                elif needs_confirm: self._confirm.add(job['id'])
                else: self._ready.add(job['id'])
                if candidate['reference'] != payload['reference']: self._reference_fixes.add(job['id'])
            notes = [problem or job['detail']]
            if editable and needs_confirm: notes.append('中文来源待确认；点提交可一次确认所选任务，无需重新导入。')
            if editable and not payload.get('agent'): notes.append('未分配 Agent：右键批量指定，或在设置中填写默认 Agent。')
            if editable and job['id'] in self._reference_fixes: notes.append('提交时提取 Drive 链接，原素材说明完整保留到制作要求。')
            state = '需处理' if editable and problem else '待确认中文' if editable and needs_confirm else STATES[job['state']]
            delivery = deliveries.get(job['id'])
            if delivery:
                state = delivery['status']; notes.append(delivery['detail'])
                state=self.plugin.notices.summaries.get(job['id'],{}).get('text',
                    {'已收取部分':'待补齐','已收取·任务未完成':'等待制作完成'}.get(state,state))
            review=review_state(reviews.get(job['id'],{}).get('text',''))
            if review['text']: notes.append('AI审核结果（只读）：\n'+review['text'])
            texts = [payload['task_id'], payload['title'], payload['language'], payload['spoken'], payload['chinese'],
                     payload['reference'], state, '\n'.join(note for note in notes if note), payload.get('agent', ''), payload.get('user_requirements', '')]
            for col, text in enumerate(texts):
                item = QtWidgets.QTableWidgetItem(text); item.setToolTip(text)
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                if col == 0: item.setData(QtCore.Qt.ItemDataRole.UserRole, job['id'])
                if col == 4: item.setToolTip(payload.get('chinese_source', '') + '\n\n' + text)
                if col == 6 and review['problem']:
                    item.setBackground(QtGui.QColor('#FCE2E2')); item.setForeground(QtGui.QColor('#A22020'))
                    item.setToolTip(text+'\nAI审核结果（只读）：\n'+review['text'])
                if col in {6,7} and review['text']:
                    item.setToolTip('<qt>'+html.escape(item.toolTip()).replace('\n','<br>')+'</qt>')
                self.table.setItem(row, col, item)
        if select_ids is not None: chosen = set(select_ids) & (self._ready | self._confirm)
        elif previous is None: chosen = self._ready | self._confirm
        else: chosen = previous
        self._select_ids(chosen); self._loaded = True
        del blocker
        self.busy(self.plugin.handle is not None)
        if hasattr(self, 'image_page'): self.image_page.reload()

    def apply_receipt_summaries(self,summaries):
        """Update status cells only; never reload selection or editable drafts."""
        for row in range(self.table.rowCount()):
            identity=self.table.item(row,0);item=self.table.item(row,6)
            summary=summaries.get(identity.data(QtCore.Qt.ItemDataRole.UserRole)) if identity is not None else None
            if summary and item is not None:
                item.setText(summary['text']);item.setToolTip(summary['tooltip'])
                item.setBackground(QtGui.QColor('#FCE2E2') if summary['tone']=='error' else QtGui.QBrush())
                item.setForeground(QtGui.QColor('#A22020') if summary['tone']=='error' else QtGui.QBrush())

    def update_selection(self):
        ids = self._selected_ids(); selected = set(ids); ready = len(selected & self._ready)
        pending = len(selected & self._confirm); blocked = len(selected & self._blocked.keys())
        self.selection_summary.setText(f'已选 {len(ids)} / 共 {len(self.jobs)} 条 · 可直接提交 {ready} · 待确认中文 {pending} · 需处理 {blocked}')
        self.send.setText(f'提交所选（{len(ids)}）')
        self.send.setToolTip('中文来源待确认的草稿可一次批量确认；资料不全会说明原因。已提交和待核对记录不会重发。')
        self.send.setEnabled(not self._active and bool(ids))
        for key in ('submit', 'copy', 'check', 'remove', 'clone', 'select_all', 'select_ready', 'select_uncertain', 'clear'):
            enabled = bool(ids) if key in {'copy', 'check', 'clear'} else bool(self.jobs)
            if key == 'submit': enabled = bool(ids)
            if key in {'remove', 'clone'}: enabled = bool(ids)
            self.actions[key].setEnabled(not self._active and enabled)
        self.actions['receive'].setEnabled(not self._active)
        self.actions['receive_partial'].setEnabled(not self._active and bool(ids))
        self.actions['retry_download'].setEnabled(not self._active and bool(ids))
        self.actions['recollect_download'].setEnabled(not self._active and bool(ids))
        self.actions['open_result'].setEnabled(len(ids) == 1)
        for key in ('assign', 'user_requirements'): self.actions[key].setEnabled(not self._active and bool(ids))

    def busy(self, active):
        self._active = active
        for widget in (self.import_button, self.table, self.setup_panel, self.setup_button): widget.setEnabled(not active)
        for key in ('import_selected', 'import_all', 'import_version', 'save_target', 'account'): self.actions[key].setEnabled(not active)
        self.cancel.setEnabled(active); self.cancel.setVisible(active); self.update_selection()
        for key in ('assign', 'user_requirements'): self.actions[key].setEnabled(not active and bool(self._selected_ids()))
        if hasattr(self, 'image_page'): self.image_page.busy(active)

    def select_all(self): self.table.selectAll()
    def select_ready(self): self._select_ids(self._ready | self._confirm)
    def select_uncertain(self): self._select_ids(job['id'] for job in self.jobs if job['state'] in {'sending', 'uncertain'})
    def clear_selection(self): self.table.clearSelection()

    def create_context_menu(self):
        self.update_selection()
        menu = QtWidgets.QMenu(self)
        for key in ('submit', 'copy', 'check'): menu.addAction(self.actions[key])
        for key in ('assign', 'user_requirements'): menu.addAction(self.actions[key])
        menu.addSeparator()
        for key in ('receive', 'receive_partial', 'retry_download', 'recollect_download', 'open_result'): menu.addAction(self.actions[key])
        menu.addSeparator()
        for key in ('select_all', 'select_ready', 'select_uncertain', 'clear'): menu.addAction(self.actions[key])
        menu.addSeparator(); menu.addAction(self.actions['clone']); menu.addAction(self.actions['remove'])
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
        target = self.target()
        ids = self._prepare_selection(submitting=True)
        if ids: self.plugin.submit(target, ids, 'submit')

    def edit_batch(self, field, label):
        ids = [key for key in self.selected_ids() if self._jobs_by_id[key]['state'] in {'draft', 'failed'}]
        if not ids: raise ValueError('仅可修改未提交草稿；已提交云端内容不会被覆盖。')
        text, accepted = QtWidgets.QInputDialog.getMultiLineText(self, label, label,
            self._jobs_by_id[ids[0]]['payload'].get(field, ''))
        if accepted:
            self.plugin.store.edit_batch(ids, field, text, expected={key: fingerprint(self._jobs_by_id[key]['payload']) for key in ids})
            self.reload(select_ids=ids)

    @guarded
    def assign_agent(self): self.edit_batch('agent', '执行 Agent（名称须与表格一致）')

    @guarded
    def edit_requirements(self): self.edit_batch('user_requirements', '用户需求（仅修改所选草稿）')

    def _prepare_selection(self, *, submitting=False):
        selected = self.selected_ids()
        ids = [key for key in selected if key in self._ready or key in self._confirm or
               (not submitting and self._jobs_by_id[key]['state'] not in {'draft', 'failed'})]
        if not ids:
            reasons = list(dict.fromkeys(self._blocked[key] for key in selected if key in self._blocked))
            message = '\n'.join(reasons) or '所选记录已提交或待核对，请用右键“核对所选提交结果”；不会重复发送。'
            self.status.setText(message)
            QtWidgets.QMessageBox.information(self, '需要处理的任务', message)
            return []
        confirm = set(ids) & self._confirm
        if confirm:
            text = f'所选任务中有 {len(confirm)} 条中文台词取自任务名称，尚未确认。\n\n确认这些现有中文内容就是完整中文台词，并继续本次操作？\n若不正确，请取消后双击中文台词修改。'
            if set(ids) & self._reference_fixes:
                text += '\n\n素材栏中的 Drive 链接会单独提取；原说明完整保留到制作要求，不删除原任务内容。'
            answer = QtWidgets.QMessageBox.question(self, '批量确认中文台词', text,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes: return []
        drafts = [key for key in ids if self._jobs_by_id[key]['state'] in {'draft', 'failed'}]
        if drafts:
            self.plugin.store.prepare_drafts(drafts, confirm_chinese=bool(confirm),
                expected={key: fingerprint(self._jobs_by_id[key]['payload']) for key in drafts})
            self.reload()
        skipped = len(selected) - len(ids)
        if skipped: self.status.setText(f'本次处理 {len(ids)} 条；略过 {skipped} 条资料不全、已提交或待核对记录，原因见说明列。')
        return ids

    @guarded
    def check_result(self): self.plugin.submit(self.target(), self.selected_ids(), 'check')

    @guarded
    def receive_results(self): self.plugin.receipts.check(manual=True)

    @guarded
    def receive_partial_results(self):self.plugin.request_partial_receipt(ids=self.selected_ids())

    @guarded
    def retry_downloads(self):
        ids = self.selected_ids()
        self.plugin.store.retry_deliveries(ids)
        self.plugin.receipts.check(manual=True, ids=ids)

    @guarded
    def recollect_downloads(self): self.plugin.request_recollect(ids=self.selected_ids())

    @guarded
    def open_result(self):
        from pathlib import Path
        ids = self.selected_ids()
        if len(ids) != 1: raise ValueError('请只选择一条任务以打开原任务目录。')
        directory = Path(self._jobs_by_id[ids[0]]['payload']['target_dir'])/'result'
        if not directory.is_dir(): raise ValueError('原任务的 result 目录尚未创建，或磁盘暂不可用。')
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))): raise ValueError('无法打开成品目录。')

    @guarded
    def copy_rows(self):
        ids = self._prepare_selection()
        if ids: self.plugin.copy_tasks(ids)

    @guarded
    def remove_drafts(self):
        ids = self.selected_ids()
        if self.plugin.handle is not None or self.plugin.receipts.handle is not None:
            raise ValueError('Agent操作仍在运行，请结束本轮后再移除；未删除记录。')
        expected = {key: (self._jobs_by_id[key]['state'], fingerprint(self._jobs_by_id[key]['payload'])) for key in ids}
        answer = QtWidgets.QMessageBox.question(self, '移除本地记录',
            f'移除所选 {len(ids)} 条本地记录并停止后续成品监视？\n包含已提交和待核对记录。\n不删除云端表格、网盘文件或本地成品，也不会取消外部Agent任务。\n重新导入后将使用新作业ID；请先核对旧云端任务，避免重复派单。',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.plugin.store.remove_records(expected); self.reload(); self.plugin.notices.refresh()
        self.status.setText(f'已移除 {len(ids)} 条本地记录；云端任务和所有文件保留。')

    @guarded
    def clone_drafts(self):
        ids = self.selected_ids()
        answer = QtWidgets.QMessageBox.question(self, '复制为新草稿',
            '复制所选任务为新草稿，以便修改素材链接或正文后再次提交？\n旧云端任务不会自动取消，请先确认旧任务不会重复执行。',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        result = self.plugin.store.stage([self._jobs_by_id[key]['payload'] for key in ids], new_version=True)
        self.reload(select_ids=result['imported']); self.status.setText('新草稿已创建，可双击修改素材链接，再提交。旧任务保留。')

    @guarded
    def cancel_job(self):
        if self.plugin.handle: self.plugin.handle.requestInterruption(); self.status.setText('已请求取消，等待当前有超时限制的网络步骤结束。')

    def edit_cell(self, row, col):
        try:
            key = {2: 'language', 3: 'spoken', 4: 'chinese', 5: 'reference', 8: 'agent', 9: 'user_requirements'}.get(col)
            if not key: return
            identifier = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            job = self._jobs_by_id[identifier]
            if job['state'] not in {'draft', 'failed'}: raise ValueError('已提交或待核对记录请右键“复制所选任务为新草稿”后修改；旧云端任务不会自动取消。')
            text, accepted = QtWidgets.QInputDialog.getMultiLineText(self, '校对待提交内容', self.table.horizontalHeaderItem(col).text(), job['payload'].get(key, ''))
            if accepted: self.plugin.store.edit(job['id'], key, text); self.reload()
        except Exception as error: self.status.setText(str(error))
