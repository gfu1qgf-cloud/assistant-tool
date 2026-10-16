"""Main-thread editor; no execution here and no dialogs from worker callbacks."""
import copy
import json
from uuid import uuid4
from qt_compat import QtCore, QtWidgets
from model.node_workflow.code_node import TYPES, ports, signature, validate_script


class CodeNodeEditor(QtWidgets.QWidget):
    def __init__(self, params, changed, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        self.params, self.changed = copy.deepcopy(params), changed
        self.busy = True
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        note = QtWidgets.QLabel('本地可信代码，不是沙箱。inputs／outputs 的键是“代码名”；改代码后需重新授权。\n'
            '可直接使用 ColorValue、ResourceRef 等公共类型。不要处理Qt对象或保存主程序密钥。')
        note.setWordWrap(True); root.addWidget(note)
        self.tables = {}
        tabs = QtWidgets.QTabWidget(); root.addWidget(tabs)
        for direction, title in (('inputs', '输入端'), ('outputs', '输出端')):
            page = QtWidgets.QWidget(); layout = QtWidgets.QVBoxLayout(page)
            layout.setContentsMargins(4, 6, 4, 6)
            table = QtWidgets.QTableWidget(0, 5 if direction == 'inputs' else 4)
            table.setHorizontalHeaderLabels(['代码名', '显示标题', '类型', '必填'] + (['默认值(JSON)'] if direction == 'inputs' else []))
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            # Port columns scroll inside the table, not the entire narrow inspector.
            table.setMinimumWidth(0)
            table.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
            layout.addWidget(table); self.tables[direction] = table
            row = QtWidgets.QHBoxLayout(); layout.addLayout(row)
            add = QtWidgets.QPushButton('＋端口'); remove = QtWidgets.QPushButton('删除端口')
            remove.setToolTip('删除当前表格中选中的端口；仍有连接时不会直接删除。')
            row.addWidget(add); row.addWidget(remove)
            add.clicked.connect(lambda _=False, d=direction: self.add_port(d))
            remove.clicked.connect(lambda _=False, d=direction: self.remove_port(d))
            table.itemChanged.connect(self.apply)
            for spec in self.params[direction]:
                self.append(direction, spec)
            tabs.addTab(page, title)
        self.code = QtWidgets.QPlainTextEdit(self.params['code'])
        self.code.setPlaceholderText('outputs["value"] = inputs["value"]')
        self.code.setMinimumHeight(200); root.addWidget(self.code)
        form = QtWidgets.QFormLayout(); root.addLayout(form)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.timeout = QtWidgets.QSpinBox(); self.timeout.setRange(1, 600); self.timeout.setValue(params.get('timeout', 30))
        form.addRow('超时(秒)', self.timeout)
        self.memory = QtWidgets.QSpinBox(); self.memory.setRange(64, 32768); self.memory.setValue(params.get('memory', 512))
        self.memory.setToolTip('Windows 子进程组的总内存上限；超出上限将终止本次执行。')
        form.addRow('内存上限(MB)', self.memory)
        self.approve = QtWidgets.QCheckBox('我已阅读，允许运行本地代码')
        self.approve.setToolTip('这份代码拥有本机权限；不是安全沙箱。代码或端口修改后必须重新授权。')
        self.approve.setChecked(params.get('approved') == signature(params)); root.addWidget(self.approve)
        self.error = QtWidgets.QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet('color:#c33442'); root.addWidget(self.error)
        self.busy = False
        self.code.textChanged.connect(self.apply); self.timeout.valueChanged.connect(self.apply)
        self.memory.valueChanged.connect(self.apply)
        self.approve.toggled.connect(lambda _: self.apply(trust=True))

    def append(self, direction, spec):
        table = self.tables[direction]; row = table.rowCount(); table.insertRow(row)
        name = QtWidgets.QTableWidgetItem(spec['name']); name.setData(QtCore.Qt.ItemDataRole.UserRole, spec['id'])
        table.setItem(row, 0, name); table.setItem(row, 1, QtWidgets.QTableWidgetItem(spec['title']))
        dtype = QtWidgets.QComboBox(); dtype.addItems(TYPES); dtype.setCurrentText(spec['type']); table.setCellWidget(row, 2, dtype)
        required = QtWidgets.QCheckBox(); required.setChecked(spec['required']); table.setCellWidget(row, 3, required)
        if direction == 'inputs':
            table.setItem(row, 4, QtWidgets.QTableWidgetItem(json.dumps(spec.get('default'), ensure_ascii=False)))
        dtype.currentTextChanged.connect(self.apply); required.toggled.connect(self.apply)

    def add_port(self, direction):
        if self.tables[direction].rowCount() >= 32:
            self.error.setText('每侧最多32个端口。'); return
        self.busy = True
        suffix = uuid4().hex[:8]
        self.append(direction, {'id': 'p_' + suffix, 'name': 'value_' + suffix, 'title': '新端口', 'type': 'text', 'required': True, 'default': None})
        self.busy = False; self.apply()

    def remove_port(self, direction):
        rows = {index.row() for index in self.tables[direction].selectedIndexes()}
        self.busy = True
        for row in sorted(rows, reverse=True):
            self.tables[direction].removeRow(row)
        self.busy = False; self.apply()

    def values(self):
        values = dict(self.params, code=self.code.toPlainText(), timeout=self.timeout.value(), memory=self.memory.value())
        for direction, table in self.tables.items():
            specs = []
            for row in range(table.rowCount()):
                spec = {'id': table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole), 'name': table.item(row, 0).text(),
                    'title': table.item(row, 1).text(), 'type': table.cellWidget(row, 2).currentText(), 'required': table.cellWidget(row, 3).isChecked()}
                if direction == 'inputs':
                    spec['default'] = json.loads(table.item(row, 4).text() or 'null')
                specs.append(spec)
            values[direction] = specs
            ports(values, direction)
        return values

    def apply(self, *_, trust=False):
        if self.busy:
            return
        try:
            values = self.values()
            changed_code = signature(values) != signature(self.params)
            if changed_code:
                with QtCore.QSignalBlocker(self.approve): self.approve.setChecked(False)
            values['approved'] = signature(values) if trust and self.approve.isChecked() else '' if changed_code or not self.approve.isChecked() else self.params.get('approved', '')
            accepted = self.changed(values)
            if accepted is False:
                self.busy = True
                try:
                    for direction, table in self.tables.items():
                        table.setRowCount(0)
                        for spec in self.params[direction]: self.append(direction, spec)
                finally:
                    self.busy = False
                self.error.setText('端口仍有连接或类型冲突，本次修改未应用。请先断开受影响连线。'); return
            self.params = copy.deepcopy(values)
            try:
                validate_script(values); self.error.clear()
            except ValueError as error:
                self.error.setText(str(error))
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
