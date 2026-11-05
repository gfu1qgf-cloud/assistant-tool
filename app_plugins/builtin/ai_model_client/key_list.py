"""Multi-key settings editor. Only encrypted credentials enter its row model."""
import copy
import hashlib
import os

from qt_compat import QtCore, QtWidgets
from model.AIProviderKeys import (ENV_KEYS, KEYS_CONFIG_KEY, credential_entry, key_pool,
                                  read_provider_key)
from model.SensitiveData import redact_sensitive_text


class ProviderKeysEditor(QtWidgets.QWidget):
    changed=QtCore.pyqtSignal()
    def __init__(self, provider, parent=None, *, account_provider=lambda:''):
        super().__init__(parent)
        self.provider=provider; self.account_provider=account_provider
        self.original=None; self.pool=key_pool(None); self.dirty=False
        layout=QtWidgets.QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.count=QtWidgets.QLabel(self); layout.addWidget(self.count)
        self.table=QtWidgets.QTableWidget(0,5 if provider=='cloudflare' else 4,self)
        self.table.setHorizontalHeaderLabels(['当前','备注','密钥标识','来源']+(['Account ID'] if provider=='cloudflare' else []))
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3,QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(125); self.table.setMaximumHeight(230)
        layout.addWidget(self.table)
        bar=QtWidgets.QHBoxLayout()
        self.add_button=QtWidgets.QPushButton('添加 Key…',self)
        self.edit_button=QtWidgets.QPushButton('编辑…',self)
        self.use_button=QtWidgets.QPushButton('设为当前',self)
        self.delete_button=QtWidgets.QPushButton('删除',self)
        for button in (self.add_button,self.edit_button,self.use_button,self.delete_button):
            button.setAutoDefault(False); bar.addWidget(button)
        bar.addStretch(1); layout.addLayout(bar)
        self.note=QtWidgets.QLabel('双击可编辑，右键也可操作。修改在程序设置保存后生效；只使用当前 Key，不自动轮换或重发。',self)
        self.note.setWordWrap(True); layout.addWidget(self.note)
        self.add_button.clicked.connect(lambda:self.edit_dialog())
        self.edit_button.clicked.connect(lambda:self.edit_dialog(self.current_id()))
        self.use_button.clicked.connect(self.use_current)
        self.delete_button.clicked.connect(self.confirm_remove)
        self.table.cellDoubleClicked.connect(lambda *_:self.edit_dialog(self.current_id()))
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)

    def load(self, value):
        self.original=copy.deepcopy(value); self.pool=key_pool(value); self.dirty=False
        self.exported_value=copy.deepcopy(value)
        self.reload()

    def selected_ids(self):
        return [self.table.item(index.row(),0).data(QtCore.Qt.ItemDataRole.UserRole)
                for index in self.table.selectionModel().selectedRows() if self.table.item(index.row(),0) is not None]

    def current_id(self):
        selected=self.selected_ids(); return selected[0] if len(selected)==1 else None

    def reload(self, selected=None):
        env=bool(os.environ.get(ENV_KEYS[self.provider],'').strip())
        rows=[*self.pool['items']]
        if env: rows.append({'id':'@environment','name':ENV_KEYS[self.provider],'hint':'••••','environment':True})
        active=self.pool.get('active') or ('@environment' if not self.pool['items'] and env else None)
        self.table.blockSignals(True); self.table.clearSelection(); self.table.setRowCount(len(rows))
        for index,row in enumerate(rows):
            values=['使用中' if row['id']==active else '',row['name'],row.get('hint','••••'),
                    '环境变量 · 只读' if row.get('environment') else 'Windows加密']
            if self.provider=='cloudflare': values.append(row.get('account_id') or '使用兼容配置')
            for column,value in enumerate(values):
                item=QtWidgets.QTableWidgetItem(value); item.setData(QtCore.Qt.ItemDataRole.UserRole,row['id'])
                item.setToolTip(value)
                self.table.setItem(index,column,item)
            if row['id']==selected: self.table.selectRow(index)
        self.table.blockSignals(False)
        current=next((row['name'] for row in rows if row['id']==active),'未选择')
        self.count.setText(f'已保存 {len(self.pool["items"])} 个 Key'+('，另有 1 个环境变量 Key' if env else '')+'；当前使用：'+current)
        self.update_actions()

    def update_actions(self):
        selected=self.selected_ids(); editable=bool(selected) and '@environment' not in selected
        self.edit_button.setEnabled(editable and len(selected)==1)
        self.delete_button.setEnabled(editable)
        self.use_button.setEnabled(len(selected)==1)

    def add_key(self, key, name='', *, account_id=''):
        return self.save_entry(None,key,name,account_id=account_id)

    def save_entry(self, identifier, key, name, *, account_id=''):
        previous=next((row for row in self.pool['items'] if row['id']==identifier),None)
        if identifier and previous is None: raise ValueError('请选择一条可编辑的密钥。')
        row=credential_entry(key,name,account_id=account_id) if str(key).strip() else copy.deepcopy(previous)
        if row is None: raise ValueError('请填写要添加的 API Key。')
        row['name']=str(name).strip()[:120] or 'Key '+str(len(self.pool['items'])+(0 if previous else 1))
        if self.provider=='cloudflare':
            import re
            if account_id and not re.fullmatch(r'[0-9a-fA-F]{32}',account_id.strip()):
                raise ValueError('Account ID 必须是32位十六进制。')
            row['account_id']=account_id.strip().lower()
        if str(key).strip():
            for other in self.pool['items']:
                if other['id']==identifier: continue
                fingerprint=other.get('fingerprint')
                if not fingerprint:
                    try:
                        existing=read_provider_key({KEYS_CONFIG_KEY:{self.provider:other['encrypted']}},self.provider)
                        fingerprint=hashlib.sha256(existing.encode()).hexdigest()
                    except (ValueError,RuntimeError): pass
                if fingerprint and fingerprint==row.get('fingerprint'):
                    raise ValueError('这个 Key 已经在列表中，未重复添加。')
        if previous:
            row['id']=previous['id']
            self.pool['items'][self.pool['items'].index(previous)]=row
        else:
            if len(self.pool['items'])>=1000: raise ValueError('每个服务商最多1000条密钥。')
            self.pool['items'].append(row)
            if len(self.pool['items'])==1 and not self.pool.get('active'): self.pool['active']=row['id']
        self.dirty=True; self.reload(row['id']); self.changed.emit(); return row['id']

    def set_active(self, identifier):
        if identifier!='@environment' and identifier not in {row['id'] for row in self.pool['items']}:
            raise ValueError('密钥不存在。')
        if identifier=='@environment' and not os.environ.get(ENV_KEYS[self.provider],'').strip():
            raise ValueError('环境变量密钥当前为空。')
        self.pool['active']=identifier; self.dirty=True; self.reload(identifier); self.changed.emit()

    def remove_keys(self, identifiers):
        identifiers=set(identifiers)
        if '@environment' in identifiers: raise ValueError('环境变量只读，不能在这里删除。')
        self.pool['items']=[row for row in self.pool['items'] if row['id'] not in identifiers]
        if self.pool.get('active') in identifiers: self.pool['active']=None
        self.dirty=True; self.reload(); self.changed.emit()

    def value(self):
        if not self.dirty: return copy.deepcopy(self.original)
        if not self.pool['items'] and self.pool.get('active')!='@environment': return None
        return copy.deepcopy(self.pool)

    def use_current(self):
        selected=self.current_id()
        if selected: self.set_active(selected)

    def confirm_remove(self):
        selected=self.selected_ids()
        if not selected: return
        message=f'删除选中的 {len(selected)} 条 Key？修改需保存后生效。'
        if self.pool.get('active') in selected and len(self.pool['items'])>len(selected):
            message+='\n当前 Key 被删除后，请明确选择另一条；不会自动切到其他账户。'
        if QtWidgets.QMessageBox.question(self,'删除密钥',message)==QtWidgets.QMessageBox.StandardButton.Yes:
            self.remove_keys(selected)

    def context_menu(self,position):
        item=self.table.itemAt(position)
        if not item: return
        if not item.isSelected(): self.table.selectRow(item.row())
        menu=QtWidgets.QMenu(self)
        use=menu.addAction('设为当前使用'); use.setEnabled(self.use_button.isEnabled())
        edit=menu.addAction('编辑备注／更换 Key…'); edit.setEnabled(self.edit_button.isEnabled())
        remove=menu.addAction('删除所选'); remove.setEnabled(self.delete_button.isEnabled())
        chosen=menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is use: self.use_current()
        elif chosen is edit: self.edit_dialog(self.current_id())
        elif chosen is remove: self.confirm_remove()

    def edit_dialog(self, identifier=None):
        row=next((row for row in self.pool['items'] if row['id']==identifier),None)
        if identifier and row is None: return
        dialog=QtWidgets.QDialog(self); dialog.setWindowTitle('编辑密钥' if row else '添加密钥')
        dialog.resize(520,240)
        layout=QtWidgets.QVBoxLayout(dialog); form=QtWidgets.QFormLayout()
        name=QtWidgets.QLineEdit(row['name'] if row else '',dialog)
        key=QtWidgets.QLineEdit(dialog); key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        key.setPlaceholderText('留空保留原 Key；输入新值则更换' if row else '输入一个 API Key')
        form.addRow('备注／账号用途',name); form.addRow('API Key',key)
        account=None
        if self.provider=='cloudflare':
            account=QtWidgets.QLineEdit((row or {}).get('account_id') or self.account_provider(),dialog)
            form.addRow('Account ID',account)
        layout.addLayout(form)
        error=QtWidgets.QLabel(dialog); error.setWordWrap(True); error.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(error)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel,dialog)
        def accept():
            try:
                self.save_entry(identifier,key.text(),name.text(),account_id=account.text() if account else '')
                dialog.accept()
            except Exception as failure: error.setText(redact_sensitive_text(str(failure)))
        buttons.accepted.connect(accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        dialog.exec()
