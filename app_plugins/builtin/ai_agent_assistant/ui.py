"""One oral-project tab, a persistent local preview and explicit submit/check."""
from functools import wraps

from qt_compat import QtCore, QtGui, QtWidgets
from .tasks import spreadsheet_id, validate_payload

STATES = {'draft': '待提交', 'failed': '未提交', 'sending': '待核对', 'uncertain': '待核对', 'submitted': '已提交'}


def guarded(function):
    @wraps(function)
    def call(self, *_args):
        try: return function(self)
        except Exception as error: self.status.setText(str(error))
    return call


class AssistantDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle('AI Agent 助手'); self.resize(1120, 730)
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget(self); layout.addWidget(tabs)
        oral = QtWidgets.QWidget(tabs); tabs.addTab(oral, '口播')
        body = QtWidgets.QVBoxLayout(oral)
        form = QtWidgets.QFormLayout()
        self.url = QtWidgets.QLineEdit(oral); self.url.setPlaceholderText('首次粘贴口播统一表格链接，保存后会记住')
        self.sheet = QtWidgets.QLineEdit('口播表格', oral)
        form.addRow('目标 Google 表格', self.url); form.addRow('口播分页名称', self.sheet)
        body.addLayout(form)
        buttons = QtWidgets.QHBoxLayout()
        self.save = QtWidgets.QPushButton('保存目标表格', oral)
        self.open = QtWidgets.QPushButton('打开表格', oral)
        self.account = QtWidgets.QPushButton('Google 账号…', oral)
        for button in (self.save, self.open, self.account): buttons.addWidget(button)
        body.addLayout(buttons)
        self.auth = QtWidgets.QLabel(oral); self.auth.setWordWrap(True); body.addWidget(self.auth)
        imports = QtWidgets.QHBoxLayout()
        self.selected = QtWidgets.QPushButton('导入主界面所选任务', oral)
        self.all = QtWidgets.QPushButton('导入当前列表有口播台词的任务', oral)
        self.version = QtWidgets.QCheckBox('作为新版本（允许再做一版）', oral)
        imports.addWidget(self.selected); imports.addWidget(self.all); imports.addWidget(self.version)
        body.addLayout(imports)
        self.chinese_source = QtWidgets.QCheckBox('无专门中文列时，我确认“完整任务名称”就是完整中文台词', oral)
        self.chinese_source.setToolTip('未勾选时，任务名称只作待核对草稿，不会当作完整中文正文直接提交。')
        body.addWidget(self.chinese_source)
        note = QtWidgets.QLabel('外文提交和复制时自动按约10秒分段，复用文案分段节点，不做精密对齐；此处保留原文供校对。双击语言、台词或素材可修改待提交内容。中文来源可悬停查看。', oral)
        note.setWordWrap(True); body.addWidget(note)
        self.table = QtWidgets.QTableWidget(0, 9, oral)
        self.table.setHorizontalHeaderLabels(['提交', '任务编号', '任务名称', '口播语言', '口播原文', '中文台词', '素材链接', '状态', '说明'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        for col, width in enumerate((48, 90, 180, 80, 210, 210, 150, 80, 180)): self.table.setColumnWidth(col, width)
        body.addWidget(self.table, 1)
        actions = QtWidgets.QHBoxLayout()
        self.send = QtWidgets.QPushButton('一键填入表格', oral)
        self.check = QtWidgets.QPushButton('核对提交结果', oral)
        self.copy = QtWidgets.QPushButton('复制勾选任务表格', oral)
        self.remove = QtWidgets.QPushButton('移除勾选草稿', oral)
        self.cancel = QtWidgets.QPushButton('取消当前操作', oral)
        for button in (self.send, self.check, self.copy, self.remove, self.cancel): actions.addWidget(button)
        body.addLayout(actions)
        self.status = QtWidgets.QLabel('本页先提供填表与提交核对，不会自动生成或下载视频。', oral)
        self.status.setWordWrap(True); body.addWidget(self.status)
        self.save.clicked.connect(self.save_target)
        self.open.clicked.connect(self.open_sheet)
        self.account.clicked.connect(self.open_accounts)
        self.selected.clicked.connect(self.import_selected)
        self.all.clicked.connect(self.import_all)
        self.send.clicked.connect(self.submit)
        self.check.clicked.connect(self.check_result)
        self.copy.clicked.connect(self.copy_rows)
        self.remove.clicked.connect(self.remove_drafts)
        self.cancel.clicked.connect(self.cancel_job)
        self.table.cellDoubleClicked.connect(self.edit_cell)
        settings = plugin.store.settings(); self.url.setText(settings['url']); self.sheet.setText(settings['sheet'])
        self.chinese_source.setChecked(settings.get('use_task_name_chinese', False))
        self.reload(); self.refresh_auth()

    def target(self):
        identifier = spreadsheet_id(self.url.text())
        name = self.sheet.text().strip()
        if not name: raise ValueError('请填写口播分页名称。')
        return {'url': 'https://docs.google.com/spreadsheets/d/' + identifier + '/edit', 'sheet': name,
                'use_task_name_chinese': self.chinese_source.isChecked()}

    @guarded
    def save_target(self):
        self.plugin.store.save_settings(self.target()); self.status.setText('目标表格已保存在主程序私有存储中。')

    @guarded
    def open_sheet(self):
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.target()['url'])): raise ValueError('无法打开默认浏览器。')

    @guarded
    def open_accounts(self): self.plugin.context.open_google_account_manager()

    def refresh_auth(self):
        try:
            from model.GoogleAccounts import GoogleAccountManager
            state = GoogleAccountManager().snapshot()
            key = state['bindings'].get('sheets')
            item = next((item for item in state['accounts'] if item['id'] == key), None)
            self.auth.setText('表格账号：' + item['email'] if item else '尚未分配程序表格账号。请点“Google 账号”；也可先导入、预览和复制表格。浏览器登录不会自动变为 API 授权。')
        except Exception as error: self.auth.setText(str(error))

    @guarded
    def reload(self):
        self.jobs = self.plugin.store.jobs()
        self.table.setRowCount(len(self.jobs))
        for row, job in enumerate(self.jobs):
            payload = job['payload']
            try: validate_payload(payload); problem = ''
            except ValueError as error: problem = str(error)
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(QtCore.Qt.CheckState.Checked if job['state'] in {'draft', 'failed'} and not problem else QtCore.Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            texts = [payload['task_id'], payload['title'], payload['language'], payload['spoken'], payload['chinese'],
                     payload['reference'], STATES[job['state']], problem or job['detail']]
            for col, text in enumerate(texts, 1):
                item = QtWidgets.QTableWidgetItem(text); item.setToolTip(text)
                if col == 5: item.setToolTip(payload.get('chinese_source', '') + '\n\n' + text)
                self.table.setItem(row, col, item)
        self.busy(self.plugin.handle is not None)

    def checked_ids(self):
        ids = [job['id'] for row, job in enumerate(self.jobs)
               if self.table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked]
        if not ids: raise ValueError('请先勾选任务。')
        return ids

    def busy(self, active):
        for widget in (self.url, self.sheet, self.save, self.selected, self.all, self.version, self.chinese_source, self.send, self.check, self.copy, self.remove, self.table): widget.setEnabled(not active)
        self.cancel.setEnabled(active)

    @guarded
    def import_selected(self): self.plugin.import_rows(self.plugin.context.selected_task_rows(), new_version=self.version.isChecked())

    @guarded
    def import_all(self): self.plugin.import_targets(self.plugin.context.current_task_targets(), new_version=self.version.isChecked())

    @guarded
    def submit(self): self.plugin.submit(self.target(), self.checked_ids(), 'submit')

    @guarded
    def check_result(self): self.plugin.submit(self.target(), self.checked_ids(), 'check')

    @guarded
    def copy_rows(self):
        self.plugin.copy_tasks(self.checked_ids())

    @guarded
    def remove_drafts(self):
        self.plugin.store.remove_drafts(self.checked_ids()); self.reload()
        self.status.setText('已移除勾选草稿。已提交和待核对记录保留，云端数据未删除。')

    @guarded
    def cancel_job(self):
        if self.plugin.handle: self.plugin.handle.requestInterruption(); self.status.setText('已请求取消，等待当前有超时限制的网络步骤结束。')

    def edit_cell(self, row, col):
        try:
            key = {3: 'language', 4: 'spoken', 5: 'chinese', 6: 'reference'}.get(col)
            if not key: return
            job = self.jobs[row]
            if job['state'] not in {'draft', 'failed'}: raise ValueError('已提交或待核对任务不可改写；请明确导入新版本。')
            text, accepted = QtWidgets.QInputDialog.getMultiLineText(self, '校对待提交内容', self.table.horizontalHeaderItem(col).text(), job['payload'][key])
            if accepted: self.plugin.store.edit(job['id'], key, text); self.reload()
        except Exception as error: self.status.setText(str(error))
