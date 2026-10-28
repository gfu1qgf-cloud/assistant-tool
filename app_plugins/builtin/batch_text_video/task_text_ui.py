"""Select and validate table copy before creating batch video jobs."""
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
import traceback
from pathlib import Path
from qt_compat import QtCore,QtGui,QtWidgets
from .component_ui import Combo
from .task_texts import read_sheet_records,entry_from_record,split_title_body


class SheetWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    def __init__(self,path,parent=None):
        super().__init__(parent)
        self.path = path
    def run(self):
        try:
            records,report = read_sheet_records(self.path)
            self.ready.emit({"records":records,"report":report})
        except Exception as error:
            self.ready.emit({"error":str(error),"traceback":traceback.format_exc()})


class TaskTextImportDialog(QtWidgets.QDialog):
    def __init__(self,path=None,records=None,parent=None,selection_keys=None):
        super().__init__(parent)
        self.setWindowTitle("导入表格文案")
        self.resize(870,620)
        self.records = list(records or [])
        self.selection_keys = set(selection_keys or [])
        self.entries = []
        self.worker = None
        layout = QtWidgets.QVBoxLayout(self)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(QtWidgets.QLabel("文案来源"))
        self.source = Combo()
        self.source.addItem("任务语音（原文）","task_audio_text")
        self.source.addItem("任务名称（中文）","task_name")
        self.source.currentIndexChanged.connect(self.rebuild)
        tools.addWidget(self.source)
        tools.addWidget(QtWidgets.QLabel("类型"))
        self.kind = Combo()
        self.kind.currentIndexChanged.connect(self.rebuild)
        tools.addWidget(self.kind)
        tools.addWidget(QtWidgets.QLabel("编号 ≥"))
        self.minimum = QtWidgets.QLineEdit()
        self.minimum.setMaximumWidth(70)
        self.minimum.setPlaceholderText("不限")
        self.minimum.setValidator(QtGui.QIntValidator(0,999999999,self))
        self.minimum.textChanged.connect(self.rebuild)
        tools.addWidget(self.minimum)
        layout.addLayout(tools)
        self.no_title = QtWidgets.QCheckBox('无标题（全部文字作为正文）')
        self.no_title.setAccessibleName('无标题，全部文字作为正文')
        self.no_title.setToolTip('包括第一行在内的全部文字进入正文，保留段落；单行文案也可导入。标题控件是否显示仍由你自行勾选。')
        self.no_title.toggled.connect(lambda *_: self.rebuild(preserve_checks=True))
        layout.addWidget(self.no_title)
        self.list = QtWidgets.QTableWidget(0,4)
        self.list.setHorizontalHeaderLabels(["任务","类型","第一行标题","检查结果"])
        self.list.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.selection_menu)
        self.list.setToolTip("Ctrl / Shift多选；Ctrl+A全选。右键可批量勾选，或点击“仅导入高亮行”。")
        self.list.horizontalHeader().setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.list.setColumnWidth(0,90)
        self.list.setColumnWidth(1,80)
        self.list.setColumnWidth(3,260)
        self.list.currentCellChanged.connect(self.show_copy)
        layout.addWidget(self.list,1)
        row = QtWidgets.QHBoxLayout()
        for label,checked in (("选择全部",True),("取消选择",False)):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda _checked=False,value=checked:self.check_all(value))
            row.addWidget(button)
        row.addStretch()
        import_highlighted = QtWidgets.QPushButton("仅导入高亮行")
        import_highlighted.clicked.connect(self.import_highlighted)
        row.addWidget(import_highlighted)
        layout.addLayout(row)
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(150)
        self.preview.setPlaceholderText("选中任务查看完整标题和正文")
        layout.addWidget(self.preview)
        self.status = QtWidgets.QLabel("读取中…" if path else "")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                                 QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("导入所选文案")
        self.buttons.accepted.connect(self.accept_selection)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if path:
            self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self.worker = SheetWorker(path,self)
            self.worker.ready.connect(self.loaded,QtCore.Qt.ConnectionType.QueuedConnection)
            connect_thread_finished(self.worker, self, 'worker', self.finished_loading)
            self.worker.start()
        else:
            self.populate_types()

    def loaded(self,result):
        if result.get("error"):
            self.status.setText("读取失败："+result["error"])
            self.setToolTip(result.get("traceback",""))
            return
        self.records = [record for record in result["records"] if not self.selection_keys
                        or (record["task_type"],record["task_id"]) in self.selection_keys]
        self.populate_types()

    def finished_loading(self):
        self.worker.deleteLater()
        self.worker = None
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.records))

    def populate_types(self):
        self.kind.blockSignals(True)
        self.kind.clear()
        self.kind.addItem("全部","")
        for kind in dict.fromkeys(record["task_type"] for record in self.records):
            self.kind.addItem(kind,kind)
        self.kind.blockSignals(False)
        self.rebuild()

    @staticmethod
    def record_key(record):
        return tuple(str(record.get(key, '')) for key in ('task_type', 'task_id', 'source_row', 'target_dir'))

    def rebuild(self,*_args,preserve_checks=False):
        previous, current = {}, None
        if preserve_checks:
            for row, record in enumerate(getattr(self, 'visible_records', [])):
                key = self.record_key(record)
                previous[key] = (self.list.item(row, 0).checkState(), self.list.item(row, 3).text() == '可导入')
                if row == self.list.currentRow(): current = key
        minimum = int(self.minimum.text() or 0)
        kind = self.kind.currentData()
        no_title = self.no_title.isChecked()
        self.note.setText(('全部文字（包括第一行）作为正文，标题留空；保留换行，单行文案也可导入。' if no_title else
            '第一行作为标题，其余内容作为正文；保留换行。') +
            '同时导入任务已有的人声和 SRT 字幕，没有则跳过。只读取，不修改表格或原文件。')
        self.list.setHorizontalHeaderLabels(['任务', '类型', '正文预览' if no_title else '第一行标题', '检查结果'])
        self.preview.setPlaceholderText('选中任务查看完整正文（不生成标题）' if no_title else '选中任务查看完整标题和正文')
        self.visible_records = [record for record in self.records if (not kind or record["task_type"] == kind)
            and (not minimum or record["task_id"].isdigit() and int(record["task_id"]) >= minimum)]
        self.list.setRowCount(len(self.visible_records))
        errors = 0
        for row,record in enumerate(self.visible_records):
            try:
                title,body = split_title_body(record.get(self.source.currentData(),""), no_title=no_title)
                if no_title: title = body.split('\n', 1)[0][:120]
                issue = "可导入"
            except ValueError as error:
                title,issue = "",str(error)
                errors += 1
            for column,value in enumerate((record["task_id"],record["task_type"],title,issue)):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    check = QtCore.Qt.CheckState.Unchecked if issue != '可导入' else QtCore.Qt.CheckState.Checked
                    old = previous.get(self.record_key(record))
                    if issue == '可导入' and old and old[1]: check = old[0]
                    item.setCheckState(check)
                if issue != "可导入":
                    item.setForeground(QtGui.QColor("#bd3030"))
                self.list.setItem(row,column,item)
        invalid = '正文为空' if no_title else '标题/正文无效'
        self.status.setText(f'显示{len(self.visible_records)}条，其中{errors}条{invalid}（未勾选）；可筛选reels和起始编号。')
        if self.visible_records:
            row = next((i for i, record in enumerate(self.visible_records) if self.record_key(record) == current), 0)
            self.list.setCurrentCell(row,0)
            self.show_copy(row)
        else:
            self.preview.clear()

    def show_copy(self,row,*_args):
        if 0 <= row < len(getattr(self,"visible_records",[])):
            text = self.visible_records[row].get(self.source.currentData(), '')
            if self.no_title.isChecked():
                try: _, text = split_title_body(text, no_title=True)
                except ValueError: text = ''
            self.preview.setPlainText(text)

    def check_all(self,checked):
        for row in range(self.list.rowCount()):
            self.list.item(row,0).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def set_selected_checks(self, checked, only=False):
        selected = {index.row() for index in self.list.selectedIndexes()}
        if only:
            self.check_all(False)
        for row in selected:
            if not checked or self.list.item(row,3).text() == "可导入":
                self.list.item(row,0).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def selection_menu(self, position):
        item = self.list.itemAt(position)
        if item and not item.isSelected():
            self.list.selectRow(item.row())
        menu = QtWidgets.QMenu(self.list)
        menu.addAction("勾选高亮行", lambda: self.set_selected_checks(True))
        menu.addAction("取消勾选高亮行", lambda: self.set_selected_checks(False))
        menu.addAction("仅勾选高亮行", lambda: self.set_selected_checks(True, only=True))
        menu.addAction("仅导入高亮行", self.import_highlighted)
        menu.addSeparator()
        menu.addAction("全选有效文案", self.check_valid)
        menu.addAction("取消全部勾选", lambda: self.check_all(False))
        menu.exec(self.list.viewport().mapToGlobal(position))

    def check_valid(self):
        for row in range(self.list.rowCount()):
            self.list.item(row,0).setCheckState(QtCore.Qt.CheckState.Checked if self.list.item(row,3).text() == "可导入"
                                               else QtCore.Qt.CheckState.Unchecked)

    def import_highlighted(self):
        if not self.list.selectedIndexes():
            self.status.setText("请先使用Ctrl / Shift选中要导入的行。")
            return
        self.set_selected_checks(True, only=True)
        self.accept_selection()

    def accept_selection(self):
        if self.worker:
            return
        entries,issues = [],[]
        for row,record in enumerate(self.visible_records):
            if self.list.item(row,0).checkState() != QtCore.Qt.CheckState.Checked:
                continue
            try:
                entries.append(entry_from_record(record,self.source.currentData(), no_title=self.no_title.isChecked()))
            except (ValueError,OSError) as error:
                issues.append(f"任务{record['task_id']}：{error}")
        if issues:
            QtWidgets.QMessageBox.warning(self,"文案无法导入","\n".join(issues[:20]))
            return
        if not entries:
            self.status.setText("请先选择可导入的任务。")
            return
        self.entries = entries
        super().accept()

    def reject(self):
        if self.worker:
            self.status.setText("正在读取表格，完成后即可关闭。")
            return
        super().reject()

    def closeEvent(self,event):
        if self.worker:
            event.ignore()
            return
        super().closeEvent(event)
