"""Optional visual editor for the existing task schema; no workflow file."""
from pathlib import Path
from qt_compat import QtCore, QtWidgets
from model.TaskTableSchema import normalize_task_table_schema, FIELD_ORDER
from model.TaskSchemaMapping import SchemaMapping, read_columns, saved_display_names
from PYUI.task_schema_canvas import SchemaMappingPanel


class TaskTableSchemaNodeDialog(QtWidgets.QDialog):
    def __init__(self, schema, ods_path=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('节点式编辑任务表格结构')
        self.resize(1260, 850)
        self.setMinimumSize(900, 620)
        self.original = normalize_task_table_schema(schema)
        self.ods_path = Path(ods_path) if ods_path else None
        self.handle = None
        self._manager = None
        self._owns_manager = False
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel('左侧每个筛选器可放多个候选列名，连到右侧最终数据结构的一个字段。每个字段只接一个筛选器；这里只编辑映射，不运行工作流。', self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QtWidgets.QHBoxLayout()
        self.path_label = QtWidgets.QLabel(self.ods_path.name if self.ods_path else '未选择本地任务表', self)
        self.path_label.setToolTip(str(self.ods_path or ''))
        self.choose = QtWidgets.QPushButton('选择 ODS 表格', self)
        self.read = QtWidgets.QPushButton('读取列名', self)
        self.cancel_read = QtWidgets.QPushButton('取消读取', self)
        self.from_headers = QtWidgets.QPushButton('从表头添加筛选器', self)
        self.available_headers = []
        self.from_headers.setEnabled(False)
        self.cancel_read.setEnabled(False)
        row.addWidget(self.path_label, 1)
        for button in (self.choose, self.read, self.from_headers, self.cancel_read):
            row.addWidget(button)
        layout.addLayout(row)
        self.advanced = QtWidgets.QToolButton(self)
        self.advanced.setText('工作表与表头设置（展开）')
        self.advanced.setCheckable(True)
        layout.addWidget(self.advanced)
        advanced = QtWidgets.QWidget(self)
        form = QtWidgets.QFormLayout(advanced)
        self.sheet_name = QtWidgets.QLineEdit(self.original['sheet_name'], advanced)
        self.sheet_name.setPlaceholderText('留空按工作表序号')
        self.sheet_index = QtWidgets.QSpinBox(advanced)
        self.sheet_index.setRange(1, 100)
        self.sheet_index.setValue(self.original['sheet_index'] + 1)
        self.header_row = QtWidgets.QSpinBox(advanced)
        self.header_row.setRange(0, 10000)
        self.header_row.setSpecialValueText('自动识别（需已有至少两个匹配字段）')
        self.header_row.setValue(self.original['header_row'])
        self.submission_header_row = QtWidgets.QSpinBox(advanced)
        self.submission_header_row.setRange(0, 10000)
        self.submission_header_row.setSpecialValueText('自动识别')
        self.submission_header_row.setValue(self.original['submission_sheet']['header_row'])
        form.addRow('任务工作表名称', self.sheet_name)
        form.addRow('任务工作表序号', self.sheet_index)
        form.addRow('任务表头行号', self.header_row)
        form.addRow('Google 回写表头行号', self.submission_header_row)
        advanced.hide()
        self.advanced.toggled.connect(advanced.setVisible)
        layout.addWidget(advanced)
        self.tabs = QtWidgets.QTabWidget(self)
        self.task_panel = SchemaMappingPanel(SchemaMapping(self.original), self.tabs)
        self.submission_panel = SchemaMappingPanel(SchemaMapping(self.original, submission=True), self.tabs)
        self.tabs.addTab(self.task_panel, '本地任务读取')
        self.tabs.addTab(self.submission_panel, '结果回写字段')
        self.tabs.currentChanged.connect(self.update_read_state)
        self.sheet_name.textEdited.connect(self.invalidate_headers)
        self.sheet_index.valueChanged.connect(self.invalidate_headers)
        self.header_row.valueChanged.connect(self.invalidate_headers)
        layout.addWidget(self.tabs, 1)
        note = QtWidgets.QLabel('双击筛选器编辑多个列名及其先后顺序；默认值支持 {row}、{data_row}、{task_id} 等模板。'
                               '未连接的筛选器只是当前草稿，不单独保存；实际表格的自定义列仍由原读取器保留。', self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QtWidgets.QLabel('修改只在本窗口草稿中生效，点击保存结构才交给原设置保存。', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel, self)
        self.buttons.button(QtWidgets.QDialogButtonBox.Save).setText('保存结构')
        self.buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.choose.clicked.connect(self.choose_table)
        self.read.clicked.connect(self.read_headers)
        self.cancel_read.clicked.connect(self.cancel_reading)
        self.from_headers.clicked.connect(self.filter_from_headers)
        self.update_read_state()
        QtCore.QTimer.singleShot(0, self.initial_view)

    def initial_view(self):
        # Keep labels readable; long schemas scroll rather than shrinking to dots.
        for panel in (self.task_panel, self.submission_panel):
            bounds = panel.scene.itemsBoundingRect()
            scale = min(.9, max(.72, (panel.view.viewport().width() - 70) / max(1, bounds.width())))
            panel.view.scale(scale, scale)
            panel.view.centerOn(bounds.center().x(), panel.view.viewport().height() / scale / 2 - 20)

    def schema(self):
        result = self.task_panel.document.compile()
        result['submission_sheet'] = self.submission_panel.document.compile()['submission_sheet']
        result.update(sheet_name=self.sheet_name.text(), sheet_index=self.sheet_index.value() - 1,
                      header_row=self.header_row.value())
        result['submission_sheet']['header_row'] = self.submission_header_row.value()
        return normalize_task_table_schema(result)

    def choose_table(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择本地任务表', str(self.ods_path or ''), 'ODS 表格 (*.ods)')
        if path:
            self.ods_path = Path(path)
            self.path_label.setText(self.ods_path.name)
            self.path_label.setToolTip(str(self.ods_path))
            self.invalidate_headers()

    def invalidate_headers(self, *_args):
        self.available_headers = []
        self.update_read_state()

    def manager(self):
        if self._manager is None:
            host = getattr(self.parent(), 'plugin_host', None)
            if host is not None:
                self._manager = host.background_tasks
            else:
                from model.BackgroundTasks import BackgroundTaskManager
                self._manager = BackgroundTaskManager(self)
                self._owns_manager = True
        return self._manager

    def update_read_state(self, *_args):
        busy = self.handle is not None
        self.choose.setEnabled(not busy)
        self.read.setEnabled(not busy and self.tabs.currentIndex() == 0)
        self.read.setToolTip('从本地 ODS 读取表头；Google 回写列名请手动添加，不会访问线上表格。')
        self.cancel_read.setEnabled(busy)
        self.from_headers.setEnabled(not busy and bool(self.available_headers) and self.tabs.currentIndex() == 0)
        self.buttons.button(QtWidgets.QDialogButtonBox.Save).setEnabled(not busy)

    def read_headers(self):
        if self.handle is not None:
            return
        if self.ods_path is None or not self.ods_path.is_file():
            self.status.setText('请先选择存在的本地 ODS 表格。')
            return
        self._read_snapshot = self.schema()
        self.available_headers = []
        self._read_canceled = False
        try:
            handle = self.manager().submit(owner='task_schema_editor', title='读取任务表列名', runner=read_columns,
                payload={'path': str(self.ods_path), 'schema': self._read_snapshot}, kind='cpu')
        except (RuntimeError, ValueError, OSError) as error:
            self.status.setText('未开始读取：' + str(error))
            return
        self.handle = handle
        handle.completed.connect(self.headers_ready)
        handle.failed.connect(lambda message: self.status.setText('读取失败：' + message))
        handle.finished.connect(self.read_finished)
        self.status.setText('正在后台读取表头，不会读取或修改在线表格。')
        self.update_read_state()

    @QtCore.pyqtSlot(object)
    def headers_ready(self, result):
        if self.sender() is not self.handle:
            return
        if self._read_canceled:
            return
        if self.schema() != self._read_snapshot:
            self.status.setText('读取期间配置已更改，未应用旧结果，请重新读取。')
            return
        self.available_headers = list(result['headers'])
        self.status.setText(f'已读取“{result["sheet_name"]}”第{result["header_row"]}行：{len(result["headers"])}个列名；点击“从表头添加筛选器”选择候选列，已有连线不变。')

    def filter_from_headers(self):
        aliases = self.task_panel.aliases_dialog(self.available_headers)
        if aliases:
            self.task_panel.add_filter_record(aliases)

    @QtCore.pyqtSlot()
    def read_finished(self):
        if self.sender() is self.handle:
            self.handle = None
            self.update_read_state()

    def cancel_reading(self):
        if self.handle is not None:
            self._read_canceled = True
            self.handle.requestInterruption()
            self.status.setText('已请求取消，等待表格解析安全结束；不会应用取消后的结果。')

    def can_finish(self):
        if self.handle is not None:
            self.status.setText('请等待读取完成，或点击“取消读取”并等待安全结束。')
            return False
        return True

    def accept(self):
        if not self.can_finish():
            return
        if any(not panel.label_edit.text().strip() for panel in (self.task_panel, self.submission_panel)):
            self.status.setText('字段显示名称不能为空，请填写后保存。')
            return
        if not any(self.schema()['fields'][key]['aliases'] for key in FIELD_ORDER):
            self.status.setText('至少连接一个本地任务字段；任务编号和类型仍受原读取器校验。')
            return
        if self._owns_manager and not self._manager.shutdown():
            return
        super().accept()

    def reject(self):
        if self.can_finish():
            if self._owns_manager and not self._manager.shutdown():
                return
            super().reject()

    def closeEvent(self, event):
        if self.can_finish():
            if self._owns_manager and not self._manager.shutdown():
                event.ignore()
                return
            super().closeEvent(event)
        else:
            event.ignore()

    @classmethod
    def get_schema(cls, schema, ods_path=None, parent=None):
        dialog = cls(schema, ods_path, parent)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return None
        return saved_display_names(dialog.schema())
