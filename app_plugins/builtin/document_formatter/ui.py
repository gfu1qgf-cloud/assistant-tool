"""One document panel with editable paragraph roles and host background jobs."""
import copy
from functools import wraps
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6 import sip
from model.FileLocations import open_file_location
from .rules import DEFAULT_RULES, ROLES, normalize_rules, paragraph_roles, summary
from .background import run_document_job
from .google_auth import configure_client
from .google_docs import document_target


def guarded(method):
    @wraps(method)
    def action(self, *args, **kwargs):
        if sip.isdeleted(self) or self.stopping: return
        try: return method(self, *args, **kwargs)
        except Exception as error:
            if not self.stopping:
                self.status.setText(str(error))
                QtWidgets.QMessageBox.warning(self, '文档自动格式化', str(error))
    return action


class DocumentFormatterDialog(QtWidgets.QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context, self.stopping, self.handle = context, False, None
        self.sources, self.snapshots, self.roles, self.outputs = [], {}, {}, []
        self.active_source, self.operation = '', ''
        self.setWindowTitle('文档自动格式化 · ODT / Google Docs'); self.resize(1150, 800)
        root = QtWidgets.QVBoxLayout(self)
        self.menu = QtWidgets.QMenuBar(self); self.menu.setNativeMenuBar(False); root.addWidget(self.menu)
        file_menu = self.menu.addMenu('文件')
        file_menu.addAction('添加ODT文档…', self.add_odt)
        file_menu.addAction('移出当前文档（不删除文件）', self.remove_source)
        file_menu.addAction('打开最后一个成品', self.open_output)
        file_menu.addAction('打开成品文件位置', self.open_output_location)
        file_menu.addSeparator(); file_menu.addAction('关闭', self.close)
        settings = self.menu.addMenu('格式参数')
        settings.addAction('恢复提供文档的规范', self.reset_rules)
        settings.addAction('保存当前格式参数', self.save_rules)
        google_menu = self.menu.addMenu('Google Docs')
        google_menu.addAction('选择OAuth客户端JSON…', self.select_client)
        google_menu.addAction('Google授权…', self.authorize)
        google_menu.addAction('恢复上次在线格式化', self.restore_google)
        help_menu = self.menu.addMenu('帮助'); help_menu.addAction('格式规范与使用说明', self.show_help)
        bar = QtWidgets.QHBoxLayout(); root.addLayout(bar)
        self.mode = QtWidgets.QComboBox(); self.mode.addItem('本地ODT文字文档', 'odt'); self.mode.addItem('Google Docs文字文档', 'google')
        self.mode.currentIndexChanged.connect(self.mode_changed); bar.addWidget(self.mode)
        self.add_button = QtWidgets.QPushButton('添加ODT…'); self.add_button.setAutoDefault(False); self.add_button.clicked.connect(self.add_odt); bar.addWidget(self.add_button)
        self.google_url = QtWidgets.QLineEdit(); self.google_url.setPlaceholderText('Google Docs链接（不是Google表格）'); bar.addWidget(self.google_url, 1)
        self.read_button = QtWidgets.QPushButton('读取 / 刷新文档'); self.read_button.setAutoDefault(False); self.read_button.clicked.connect(self.read_document); bar.addWidget(self.read_button)
        navigation = QtWidgets.QHBoxLayout(); root.addLayout(navigation)
        navigation.addWidget(QtWidgets.QLabel('当前文档'))
        self.source_combo = QtWidgets.QComboBox(); self.source_combo.currentIndexChanged.connect(self.source_changed); navigation.addWidget(self.source_combo, 1)
        self.tab_combo = QtWidgets.QComboBox(); self.tab_combo.setToolTip('只格式化所选Google文档分页，不修改其他分页。'); self.tab_combo.activated.connect(self.select_google_tab); navigation.addWidget(self.tab_combo)
        self.summary_label = QtWidgets.QLabel(); self.summary_label.setWordWrap(True); root.addWidget(self.summary_label)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); root.addWidget(splitter, 1)
        self.table = QtWidgets.QTableWidget(0, 3); self.table.setHorizontalHeaderLabels(['段落', '类型（可修改）', '原文'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 55); self.table.setColumnWidth(1, 110); splitter.addWidget(self.table)
        panel = QtWidgets.QWidget(); layout = QtWidgets.QVBoxLayout(panel); self.fields = {}
        form = QtWidgets.QFormLayout()
        labels = {'page_width_cm': '页面宽 / cm', 'page_height_cm': '页面高 / cm', 'margin_top_cm': '上边距 / cm',
                  'margin_bottom_cm': '下边距 / cm', 'margin_left_cm': '左边距 / cm', 'margin_right_cm': '右边距 / cm',
                  'indent_left_cm': '左缩进 / cm', 'indent_right_cm': '右缩进 / cm', 'first_line_cm': '首行缩进 / cm',
                  'title_pt': '标题字号 / 磅', 'body_pt': '正文字号 / 磅', 'line_spacing': '行距 / 倍'}
        for key, label in labels.items():
            control = QtWidgets.QDoubleSpinBox(); control.setDecimals(2); control.setRange(0, 60); control.setValue(DEFAULT_RULES[key])
            control.valueChanged.connect(self.rules_changed); form.addRow(label, control); self.fields[key] = control
        layout.addLayout(form)
        self.first_title = QtWidgets.QCheckBox('无标题样式时，首个非空段落视为标题'); self.first_title.setChecked(True); self.first_title.toggled.connect(self.reclassify)
        self.reset_spacing = QtWidgets.QCheckBox('段前 / 段后间距归零（默认保留原设置）'); self.reset_spacing.toggled.connect(self.rules_changed)
        layout.addWidget(self.first_title); layout.addWidget(self.reset_spacing)
        note = QtWidgets.QLabel('保留字体名称、对齐、原文字词、图片和链接。\n表格、列表、目录、批注默认“保留”，可逐段改成标题或正文。\n段落清单是修改预览，不模拟实际分页。')
        note.setWordWrap(True); layout.addWidget(note); layout.addStretch(1)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel); scroll.setMinimumWidth(320); splitter.addWidget(scroll); splitter.setSizes([750, 350])
        footer = QtWidgets.QHBoxLayout(); root.addLayout(footer)
        self.format_button = QtWidgets.QPushButton('按规范另存当前ODT'); self.format_button.clicked.connect(self.format_current); footer.addWidget(self.format_button)
        self.batch_button = QtWidgets.QPushButton('批量另存全部ODT'); self.batch_button.clicked.connect(self.format_all); footer.addWidget(self.batch_button)
        self.cancel_button = QtWidgets.QPushButton('取消后台操作'); self.cancel_button.clicked.connect(self.cancel_job); self.cancel_button.setEnabled(False); footer.addWidget(self.cancel_button)
        self.location_button = QtWidgets.QPushButton('打开成品文件位置'); self.location_button.clicked.connect(self.open_output_location); self.location_button.setEnabled(False); footer.addWidget(self.location_button)
        self.progress = QtWidgets.QProgressBar(); root.addWidget(self.progress)
        self.status = QtWidgets.QLabel('添加ODT或输入Google Docs链接后读取；原文件不覆盖。'); self.status.setWordWrap(True); root.addWidget(self.status)
        self.context.background_tasks.changed.connect(self.record_changed)
        self.mode_changed(); self.load_rules()

    def raw_rules(self): return {**{key: control.value() for key, control in self.fields.items()}, 'first_title': self.first_title.isChecked(), 'reset_spacing': self.reset_spacing.isChecked()}

    @guarded
    def rules_changed(self, *_): self.summary_label.setText(summary(self.raw_rules()))

    @guarded
    def load_rules(self):
        file = self.context.storage.file('preferences.json')
        if file.exists(): self.set_rules(normalize_rules(file.read_json()))
        else: self.rules_changed()

    def set_rules(self, values):
        for key, field in self.fields.items(): field.blockSignals(True); field.setValue(values[key]); field.blockSignals(False)
        self.first_title.blockSignals(True); self.first_title.setChecked(values['first_title']); self.first_title.blockSignals(False)
        self.reset_spacing.setChecked(values['reset_spacing']); self.rules_changed(); self.reclassify()

    @guarded
    def reset_rules(self, *_): self.set_rules(DEFAULT_RULES)

    @guarded
    def save_rules(self, *_):
        self.context.storage.file('preferences.json').write_json(normalize_rules(self.raw_rules())); self.status.setText('格式参数已保存到主程序管理的数据目录。')

    @guarded
    def mode_changed(self, *_):
        google = self.mode.currentData() == 'google'
        self.google_url.setVisible(google); self.tab_combo.setVisible(google); self.add_button.setVisible(not google)
        self.batch_button.setVisible(not google)
        self.format_button.setText('格式化所选Google文档分页…' if google else '按规范另存当前ODT')
        self.source_combo.clear()
        for source in self.sources if not google else []: self.source_combo.addItem(Path(source).name, source)
        self.source_changed()

    @guarded
    def add_odt(self, *_):
        if self.handle and self.handle.isRunning(): raise ValueError('请等当前文档操作完成或取消后再添加。')
        values, _filter = QtWidgets.QFileDialog.getOpenFileNames(self, '添加ODT文字文档', '', 'ODT文字文档 (*.odt)')
        for source in values:
            source = str(Path(source).resolve())
            if source not in self.sources: self.sources.append(source)
        self.mode.setCurrentIndex(0); self.mode_changed()
        if values: self.read_document()

    @guarded
    def remove_source(self, *_):
        if self.handle and self.handle.isRunning(): raise ValueError('请等当前文档操作完成或取消后再移出。')
        source = self.source_combo.currentData()
        if source in self.sources: self.sources.remove(source); self.snapshots.pop(source, None); self.roles.pop(source, None)
        self.mode_changed()

    @guarded
    def source_changed(self, *_):
        self.active_source = str(self.source_combo.currentData() or '')
        self.populate()

    def current_snapshot(self): return self.snapshots.get(self.active_source)

    def populate(self):
        snapshot = self.current_snapshot(); self.table.setRowCount(0)
        if not snapshot: return
        mapping = self.roles.setdefault(self.active_source, paragraph_roles(snapshot['paragraphs'], normalize_rules(self.raw_rules())))
        self.table.setRowCount(len(snapshot['paragraphs']))
        for row, paragraph in enumerate(snapshot['paragraphs']):
            for column, value in ((0, str(row+1)), (2, paragraph['text'].strip())):
                item = QtWidgets.QTableWidgetItem(value); item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(paragraph['text']); self.table.setItem(row, column, item)
            control = QtWidgets.QComboBox()
            for key, label in ROLES.items(): control.addItem(label, key)
            control.setCurrentIndex(control.findData(mapping[paragraph['id']]))
            control.currentIndexChanged.connect(lambda _index, p=paragraph['id'], c=control: self.set_role(p, c.currentData()))
            self.table.setCellWidget(row, 1, control)

    @guarded
    def set_role(self, paragraph_id, role): self.roles[self.active_source][paragraph_id] = role

    @guarded
    def reclassify(self, *_):
        self.rules_changed()
        if self.current_snapshot(): self.roles[self.active_source] = paragraph_roles(self.current_snapshot()['paragraphs'], normalize_rules(self.raw_rules())); self.populate()

    def busy(self, active):
        for control in (self.mode, self.read_button, self.add_button, self.source_combo, self.tab_combo, self.google_url, self.table, self.format_button, self.batch_button): control.setEnabled(not active)
        self.cancel_button.setEnabled(active)

    @guarded
    def submit(self, operation, **payload):
        if self.handle and self.handle.isRunning(): raise ValueError('当前文档操作还未完成，可先取消。')
        resources = ['document-formatter:oauth'] if 'google' in operation or operation == 'authorize' else [f'directory:{Path(plan["snapshot"]["source"]).parent}' for plan in payload.get('plans', [])]
        self.operation = operation
        self.handle = self.context.submit_background_task(title='文档自动格式化 · ' + operation,
            runner=run_document_job, payload={'operation': operation, 'private_root': str(self.context.storage.path), **payload},
            kind='network' if 'google' in operation or operation == 'authorize' else 'io', resources=resources, execution='parallel', retryable=False)
        self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed); self.handle.progressChanged.connect(self.report)
        self.busy(True); self.progress.setValue(0); self.status.setText('已提交主程序后台，可关闭此窗口继续执行。')

    @guarded
    def read_document(self, *_):
        if self.mode.currentData() == 'google':
            document_target(self.google_url.text()); self.submit('inspect_google', source=self.google_url.text())
        else:
            source = self.source_combo.currentData()
            if not source: raise ValueError('请先添加一份ODT文字文档。')
            self.submit('inspect_odt', source=source)

    @guarded
    def select_google_tab(self, *_):
        self.submit('inspect_google', source=self.google_url.text(), tab_id=self.tab_combo.currentData())

    def plan(self, snapshot):
        source = snapshot['source'] if snapshot['kind'] == 'odt' else self.active_source
        return {'snapshot': copy.deepcopy(snapshot), 'roles': copy.deepcopy(self.roles.get(source, {}))}

    @guarded
    def format_current(self, *_):
        snapshot = self.current_snapshot()
        if not snapshot: raise ValueError('请先读取当前文档，检查标题 / 正文识别。')
        rules = normalize_rules(self.raw_rules())
        if self.mode.currentData() == 'google':
            if snapshot['kind'] != 'google': raise ValueError('请先读取Google文档。')
            if document_target(self.google_url.text())[0] != snapshot['document_id']: raise ValueError('链接已改变，请先读取新的Google文档。')
            reply = QtWidgets.QMessageBox.question(self, '确认修改在线格式', f'将修改“{snapshot["title"]} / {snapshot["tab_title"]}”的格式，不改写文字。\n恢复记录保存在主程序的数据目录。\n\n是否执行？')
            if reply != QtWidgets.QMessageBox.StandardButton.Yes: return
            self.submit('format_google', snapshot=copy.deepcopy(snapshot), roles=copy.deepcopy(self.roles[self.active_source]), rules=rules)
        else: self.submit('format_odt', plans=[self.plan(snapshot)], rules=rules)

    @guarded
    def format_all(self, *_):
        missing = [source for source in self.sources if source not in self.snapshots]
        if missing:
            # Reading and formatting all documents is handled headlessly; no per-file popup.
            self.submit('batch_odt', sources=list(self.sources), rules=normalize_rules(self.raw_rules()),
                        known=[self.plan(self.snapshots[source]) for source in self.sources if source in self.snapshots])
        else: self.submit('format_odt', plans=[self.plan(self.snapshots[source]) for source in self.sources], rules=normalize_rules(self.raw_rules()))

    @guarded
    def select_client(self, *_):
        if self.handle and self.handle.isRunning(): raise ValueError('请先完成或取消当前操作，再更换Docs授权客户端。')
        source, _filter = QtWidgets.QFileDialog.getOpenFileName(self, 'Google桌面应用OAuth客户端JSON', '', 'JSON (*.json)')
        if source: configure_client(source, str(self.context.storage.path)); self.status.setText('Docs客户端已保存。其他Google授权和API密钥未改变；请点击Google授权。')

    @guarded
    def authorize(self, *_): self.submit('authorize')

    @guarded
    def restore_google(self, *_):
        latest = self.context.storage.file('google-last.json')
        if not latest.exists(): raise ValueError('还没有Google格式化恢复记录。')
        record = self.context.storage.file(latest.read_json()['backup']).read_json()
        if QtWidgets.QMessageBox.question(self, '恢复上次在线格式', f'恢复“{record["title"]}”上次格式化前的样式？\n若后来又被编辑，会拒绝自动恢复。') == QtWidgets.QMessageBox.StandardButton.Yes:
            self.submit('restore_google')

    @guarded
    def cancel_job(self, *_):
        if self.handle: self.handle.requestInterruption(); self.status.setText('已请求取消，等待当前安全步骤结束。')

    @guarded
    def report(self, message, percent):
        if self.stopping: return
        self.status.setText(str(message)); self.progress.setValue(int(percent or 0))

    @guarded
    def record_changed(self, task_id):
        if self.stopping or not self.handle or self.handle.task_id != task_id: return
        if self.context.background_tasks.get(task_id)['status'] == 'canceled':
            self.handle = None; self.busy(False); self.status.setText('操作已取消。已经完成的ODT成品保留，原文件未改。')

    @guarded
    def completed(self, value):
        if self.stopping: return
        self.handle = None; self.busy(False); self.progress.setValue(100)
        kind = value.get('kind')
        if 'paragraphs' in value:
            key = value['source'] if kind == 'odt' else value['document_id'] + ':' + value['tab_id']
            self.snapshots[key] = value; self.roles[key] = paragraph_roles(value['paragraphs'], normalize_rules(self.raw_rules()))
            if kind == 'google':
                self.source_combo.blockSignals(True); self.source_combo.clear(); self.source_combo.addItem(value['title'], key); self.source_combo.blockSignals(False)
                self.tab_combo.clear()
                for tab in value['tabs']: self.tab_combo.addItem(tab['title'], tab['id'])
                self.tab_combo.setCurrentIndex(self.tab_combo.findData(value['tab_id']))
            self.active_source = key; self.populate(); self.status.setText(f'已读取{len(value["paragraphs"])}段；修改段落类型后即可执行。')
        elif kind == 'auth': self.status.setText('Google授权成功。尚未读取或修改任何文档。')
        elif kind == 'batch':
            self.outputs = [path for result in value['results'] for path in result['paths']]
            failed = [result for result in value['results'] if result['status'] == 'failed']
            self.status.setText(f'成功{len(value["results"])-len(failed)}，失败{len(failed)}。原ODT均未覆盖。' + '\n' + '\n'.join(f'{Path(item["source"]).name}：{item["error"]}' for item in failed))
        else:
            self.outputs = [value['output']] if value.get('output') else []
            self.status.setText('Google格式化完成，可从Google Docs菜单恢复上次格式。' if kind == 'google' else 'Google原格式已恢复。')
        self.location_button.setEnabled(bool(self.outputs and not self.outputs[-1].startswith('https://')))
        # Every job retains a way to reopen this panel from its background card.
        sender = self.sender()
        if sender and hasattr(sender, 'set_result_handler'): sender.set_result_handler(self.show_result)

    @guarded
    def failed(self, message):
        if self.stopping: return
        self.handle = None; self.busy(False); self.status.setText(str(message))
        QtWidgets.QMessageBox.warning(self, '文档操作未完成', str(message))

    def show_result(self): self.show(); self.raise_(); self.activateWindow(); return True

    @guarded
    def open_output(self, *_):
        if not self.outputs: raise ValueError('还没有成品，请先执行格式化。')
        value = self.outputs[-1]
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(value) if value.startswith('https://') else QtCore.QUrl.fromLocalFile(value)):
            raise ValueError('系统未能打开成品，请检查默认文档阅读器。')

    @guarded
    def open_output_location(self, *_):
        if not self.outputs or self.outputs[-1].startswith('https://'): raise ValueError('只有本地ODT成品有文件位置。')
        open_file_location(self.outputs[-1], self)

    def show_help(self, *_):
        QtWidgets.QMessageBox.information(self, '格式规范与说明', summary(DEFAULT_RULES) +
            '\n\nODT另存到原文件旁，不覆盖原文件，也不会上传。Google Docs只处理所选分页，不改文字；需要你的账号有编辑权限。'
            '\n首次Google使用：在Google Cloud启用Google Docs API，下载“桌面应用”OAuth客户端JSON，选择该JSON后点击Google授权。'
            '\n可选择主程序已经使用的同一份OAuth客户端JSON，不用另建项目，但Docs编辑权限需要单独授权。Docs令牌独立保存，仅申请documents权限，不读取网盘文件列表、不复用或覆盖表格令牌。恢复记录和授权由主程序加密数据目录管理。'
            '\n表格/列表/目录默认保留；段前段后默认保留。图形预览不是精确分页，最终可打开成品检查。')

    def closeEvent(self, event):
        if self.stopping: event.accept()
        else: event.ignore(); self.hide()
