"""Explicit per-operation local image upload review; no network in the dialog."""
import base64
from qt_compat import QtCore, QtGui, QtWidgets
from model.GoogleAccounts import GoogleAccountManager
from .references import folder_id


class ReferenceUploadDialog(QtWidgets.QDialog):
    def __init__(self, plugin, plan):
        super().__init__(plugin.dialog or plugin.context.parent_widget)
        self.plugin = plugin; self.manager = GoogleAccountManager()
        self.setWindowTitle('确认上传本地图片'); self.resize(880, 560)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel('以下本地图片将上传到所选Google Drive目录，随后把链接填入任务表。\n已有Drive链接直接沿用；不会上传整个任务文件夹，也不自动修改共享权限。', self)
        note.setWordWrap(True); layout.addWidget(note)
        form = QtWidgets.QFormLayout(); self.accounts = QtWidgets.QComboBox(self)
        self.folder = QtWidgets.QLineEdit(plugin.store.settings().get('upload_folder', ''), self)
        self.folder.setPlaceholderText('粘贴已有Drive素材文件夹链接；新文件沿用目录的共享权限')
        form.addRow('上传账号', self.accounts); form.addRow('素材上传目录', self.folder); layout.addLayout(form)
        manage = QtWidgets.QPushButton('取消本次提交并管理Google账号…', self)
        manage.clicked.connect(self.manage_accounts); layout.addWidget(manage, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        count = sum(len(job['files']) for job in plan)
        table = QtWidgets.QTableWidget(count, 4, self)
        table.setHorizontalHeaderLabels(['预览', '所属任务', '本地图片（仅用于本机确认）', '大小'])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setIconSize(QtCore.QSize(150, 100)); table.setColumnWidth(0, 165)
        table.setColumnWidth(1, 90); table.setColumnWidth(3, 85)
        table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        row = 0
        for job in plan:
            for image in job['files']:
                preview = QtWidgets.QTableWidgetItem(); pixmap = QtGui.QPixmap()
                if pixmap.loadFromData(base64.b64decode(image['thumbnail'])): preview.setIcon(QtGui.QIcon(pixmap))
                table.setItem(row, 0, preview)
                for column, text in ((1, job['task_id']), (2, image['path']), (3, f'{image["size"] / 1024:.0f} KB')):
                    item = QtWidgets.QTableWidgetItem(text); item.setToolTip(text); table.setItem(row, column, item)
                table.setRowHeight(row, 110); row += 1
        layout.addWidget(table, 1); self.table = table
        warning = QtWidgets.QLabel('请确认图片正确，且制作Agent有权访问目标目录。取消不会上传，也不会提交这些任务。', self)
        warning.setWordWrap(True); layout.addWidget(warning)
        self.error = QtWidgets.QLabel(self); self.error.setWordWrap(True); layout.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('确认上传并继续')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(self.validate_and_accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.reload_accounts()

    def reload_accounts(self):
        selected = self.accounts.currentData(); self.accounts.clear()
        try:
            state = self.manager.snapshot()
            bound = state['bindings'].get('ai_agent_assistant.uploads') or state['bindings'].get('ai_agent_assistant.sheets')
            for account in state['accounts']:
                if 'https://www.googleapis.com/auth/drive' in account.get('scopes', []):
                    self.accounts.addItem(account.get('label') or account['email'], account['id'])
            index = self.accounts.findData(selected or bound)
            if index >= 0: self.accounts.setCurrentIndex(index)
            if self.accounts.count() == 0: self.error.setText('没有具Drive上传权限的账号，请先在账号管理器中添加或补全授权。')
        except Exception as error: self.error.setText(str(error))

    def manage_accounts(self):
        # The host's account window is modeless and a sibling of this modal.
        # End the review first so OAuth/account controls are not blocked by it.
        self.reject(); self.plugin.context.open_google_account_manager()

    def validate_and_accept(self):
        try:
            if not self.accounts.currentData(): raise ValueError('请选择具Drive上传权限的账号。')
            self.parent_id = folder_id(self.folder.text())
        except ValueError as error: self.error.setText(str(error)); return
        self.accept()
