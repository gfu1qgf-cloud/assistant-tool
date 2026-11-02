"""Core settings page; OAuth is a cancelable host background task, not a UI wait."""
import json
from functools import wraps

from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import data_file
from model.GoogleAccounts import GoogleAccountManager, DISABLED
from model.GoogleAccountAuth import authorize

LABELS = {'sheets': 'Google 表格', 'drive': 'Google 网盘', 'docs': 'Google 文档'}


def guarded(function):
    @wraps(function)
    def call(self, *_args):
        try: return function(self)
        except Exception as error:
            self.status.setText(str(error))
    return call


class GoogleAccountsPage(QtWidgets.QWidget):
    def __init__(self, parent=None, *, tasks=None, manager=None):
        super().__init__(parent)
        self.manager = manager or GoogleAccountManager()
        self.tasks, self.handle, self.loaded = tasks, None, False
        self.setObjectName('google_accounts_page')
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            '① 导入一次 Google 桌面应用客户端 → ② 登录，可添加多个账号 → ③ 分配各功能使用的账号。\n'
            '客户端和令牌均使用 Windows 当前用户加密，随主程序私有数据存储；启用密盘时也在密盘内。'
            '换电脑或 Windows 用户需要重新登录。不保存 Google 密码。', self)
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        self.clients = QtWidgets.QComboBox(self)
        self.import_button = QtWidgets.QPushButton('① 导入客户端…', self)
        row.addWidget(self.clients, 1); row.addWidget(self.import_button)
        layout.addLayout(row)
        scopes = QtWidgets.QHBoxLayout()
        self.permissions = {}
        for name, label in LABELS.items():
            box = QtWidgets.QCheckBox(label + ('（上传/下载/管理）' if name == 'drive' else '（编辑）'), self)
            box.setChecked(name in {'sheets', 'drive'})
            self.permissions[name] = box; scopes.addWidget(box)
        layout.addLayout(scopes)
        actions = QtWidgets.QHBoxLayout()
        self.login_button = QtWidgets.QPushButton('② 登录 / 补充授权…', self)
        self.cancel_button = QtWidgets.QPushButton('取消本次登录', self)
        self.remove_button = QtWidgets.QPushButton('移除选中账号', self)
        for button in (self.login_button, self.cancel_button, self.remove_button): actions.addWidget(button)
        layout.addLayout(actions)
        self.accounts = QtWidgets.QTableWidget(0, 2, self)
        self.accounts.setHorizontalHeaderLabels(['已验证的 Google 账号', '已授权功能'])
        self.accounts.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.accounts.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.accounts.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.accounts.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.accounts, 1)
        group = QtWidgets.QGroupBox('③ 功能使用哪个账号', self)
        form = QtWidgets.QFormLayout(group)
        self.bindings = {}
        for name, label in LABELS.items():
            combo = QtWidgets.QComboBox(group)
            self.bindings[name] = combo; form.addRow(label, combo)
        self.save_button = QtWidgets.QPushButton('保存功能分配', group)
        form.addRow(self.save_button)
        layout.addWidget(group)
        self.status = QtWidgets.QLabel('账号管理操作立即加密保存；关闭设置窗口不撤销已完成的登录和分配。', self)
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.import_button.clicked.connect(self.import_client)
        self.login_button.clicked.connect(self.login)
        self.cancel_button.clicked.connect(self.cancel_login)
        self.remove_button.clicked.connect(self.remove_account)
        self.save_button.clicked.connect(self.save_bindings)
        self.accounts.itemSelectionChanged.connect(self.update_buttons)
        self.clients.currentIndexChanged.connect(self.update_buttons)
        if tasks is not None and hasattr(tasks, 'changed'):
            tasks.changed.connect(self.host_changed)
        self.update_buttons()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.loaded:
            self.loaded = True
            self.reload()

    @guarded
    def reload(self):
        state = self.manager.snapshot()
        selected = self.clients.currentData()
        blocker = QtCore.QSignalBlocker(self.clients)
        self.clients.clear()
        for item in state['clients']: self.clients.addItem(item['name'], item['id'])
        index = self.clients.findData(selected)
        if index >= 0: self.clients.setCurrentIndex(index)
        del blocker
        self.accounts.setRowCount(len(state['accounts']))
        for row, item in enumerate(state['accounts']):
            cell = QtWidgets.QTableWidgetItem(item['email'])
            cell.setData(QtCore.Qt.ItemDataRole.UserRole, item['id'])
            self.accounts.setItem(row, 0, cell)
            self.accounts.setItem(row, 1, QtWidgets.QTableWidgetItem('、'.join(LABELS[key] for key in item['features'])))
        for feature, combo in self.bindings.items():
            combo.clear(); combo.addItem('暂不接管（保持原授权）', '')
            combo.addItem('停用（不使用旧授权）', DISABLED)
            for item in state['accounts']:
                if feature in item['features']: combo.addItem(item['email'], item['id'])
            key = state['bindings'].get(feature, '')
            index = combo.findData(key)
            if index < 0: raise ValueError('原功能绑定的账号或权限已失效，请补充授权后重新分配。')
            combo.setCurrentIndex(index)
        self.update_buttons()

    def update_buttons(self, *_args):
        busy = self.handle is not None  # Keep ownership until native task finish.
        self.import_button.setEnabled(not busy)
        self.clients.setEnabled(not busy)
        self.login_button.setEnabled(not busy and self.tasks is not None and bool(self.clients.currentData()))
        self.cancel_button.setEnabled(busy)
        self.remove_button.setEnabled(not busy and self.accounts.currentRow() >= 0)
        self.save_button.setEnabled(not busy)
        for box in self.permissions.values(): box.setEnabled(not busy)
        for combo in self.bindings.values(): combo.setEnabled(not busy)
        if self.tasks is None: self.login_button.setToolTip('请从主程序设置打开，登录交由主程序后台任务管理。')

    @guarded
    def import_client(self):
        source, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Google Cloud 桌面应用 OAuth 客户端', '', 'JSON (*.json)')
        if not source: return
        with data_file(source).open('r', encoding='utf-8-sig') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144: raise ValueError('客户端 JSON 过大。')
        try: value = json.loads(raw)
        except ValueError: raise ValueError('客户端文件不是有效的 JSON。') from None
        key = self.manager.add_client(value, '桌面客户端 ' + str(self.clients.count() + 1))
        self.reload(); self.clients.setCurrentIndex(self.clients.findData(key))
        self.status.setText('客户端已加密保存。下一步点击“登录 / 补充授权”。原文件未移动或删除。')

    @guarded
    def login(self):
        if self.handle is not None: raise ValueError('当前登录尚未结束，请先完成或取消。')
        features = [key for key, box in self.permissions.items() if box.isChecked()]
        if not features: raise ValueError('请至少勾选一项功能权限。')
        if self.tasks is None or not self.clients.currentData(): raise ValueError('请先导入桌面客户端，并从主程序设置启动登录。')
        if any(item['owner'] == 'google_accounts' and item['status'] in {'queued', 'running', 'canceling'}
               for item in self.tasks.records()):
            raise ValueError('已有 Google 登录在后台处理；请先完成，或在“后台任务”中取消，不重复打开授权。')
        self.handle = self.tasks.submit(owner='google_accounts', title='Google 账号登录', runner=authorize,
            payload={'client_id': self.clients.currentData(), 'features': features}, kind='network',
            execution='parallel', resources=['google-accounts:login'], retryable=False)
        self.handle.completed.connect(self.completed)
        self.handle.failed.connect(self.failed)
        self.handle.finished.connect(self.finished)
        self.status.setText('请在浏览器选择账号；登录在后台执行，可关闭设置窗口，不阻塞其他任务。')
        self.update_buttons()

    def completed(self, _result):
        self.reload()
        self.status.setText('Google 账号已验证并加密保存。请在下方选择功能账号，再保存分配。')

    def failed(self, message): self.status.setText(message)

    def finished(self):
        if self.sender() is not self.handle: return
        self.handle = None
        self.update_buttons()

    def host_changed(self, task_id):
        record = self.tasks.get(task_id)
        if record['owner'] == 'google_accounts' and record['status'] == 'succeeded':
            self.reload()

    @guarded
    def cancel_login(self):
        if self.handle:
            self.handle.requestInterruption()
            self.status.setText('已请求取消；等待当前网络步骤结束，不影响已有账号。')

    @guarded
    def save_bindings(self):
        self.manager.bind_many({key: combo.currentData() or '' for key, combo in self.bindings.items()})
        self.status.setText('功能分配已加密保存；后续新请求使用所选账号，已运行的请求不强制换号。')

    @guarded
    def remove_account(self):
        row = self.accounts.currentRow()
        if row < 0: return
        item = self.accounts.item(row, 0)
        if QtWidgets.QMessageBox.question(self, '移除本地授权',
                '移除该账号，并停用它绑定的功能？不会回退使用旧账号，也不会删除云端文件。\n'
                '不会撤销其他应用的 Google 授权。') != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.manager.remove(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self.reload()
        self.status.setText('本地账号已移除，其绑定功能已停用；旧版独立授权文件未修改，也不会自动回退使用。')
