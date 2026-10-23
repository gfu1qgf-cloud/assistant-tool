"""Task submission audit UI and its dedicated worker; no main-window dependency."""
import logging
import traceback
from qt_compat import QtWidgets, QtCore, QtGui, pyqtSignal, QMessageBox, QHeaderView
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
from model.TaskSubmissionAudit import repair_submission_items, scan_task_submission_sheet


class TaskSubmissionAuditThread(QtCore.QThread):
    log = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str, str)

    def __init__(self, config, operation='scan', items=None, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.operation = str(operation)
        self.items = list(items or [])

    def run(self):
        try:
            repair_result = None
            if self.operation == 'repair':
                repair_result = repair_submission_items(
                    self.config,
                    self.items,
                    progress_callback=self.log.emit,
                )
            result = scan_task_submission_sheet(
                self.config,
                progress_callback=self.log.emit,
            )
            if repair_result is not None:
                result['repair_result'] = repair_result
        except (Exception, SystemExit) as error:
            self.failed.emit(
                f'{type(error).__name__}: {error}',
                traceback.format_exc(),
            )
            return
        self.succeeded.emit(result)


class TaskSubmissionAuditDialog(QtWidgets.QDialog):
    item_role = QtCore.Qt.UserRole

    def __init__(self, result, config, parent=None):
        super().__init__(parent)
        self.result = dict(result)
        self.config = dict(config)
        self.worker = None
        self.setWindowTitle('任务提交表自查')
        self.resize(1120, 680)

        layout = QtWidgets.QVBoxLayout(self)
        self.summary_label = QtWidgets.QLabel(self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        filter_layout = QtWidgets.QHBoxLayout()
        filter_layout.addWidget(QtWidgets.QLabel('任务类型：', self))
        self.type_filter = QtWidgets.QComboBox(self)
        self.type_filter.setMinimumWidth(260)
        self.type_filter.currentTextChanged.connect(self.applyFilter)
        filter_layout.addWidget(self.type_filter)
        filter_layout.addStretch(1)
        self.progress_label = QtWidgets.QLabel('', self)
        filter_layout.addWidget(self.progress_label)
        layout.addLayout(filter_layout)

        self.tabs = QtWidgets.QTabWidget(self)
        self.missing_tree = self.createTree([
            '视频名称', '任务类型', '需求人', '任务日期', '任务编号', '网盘链接'
        ])
        self.blank_type_tree = self.createTree([
            '表格行号', '视频名称', '历史/建议类型', '需求人', '网盘链接'
        ])
        self.duplicate_tree = self.createTree([
            '表格行号', '视频名称', '任务类型', '需求人', '网盘链接'
        ])
        self.wrong_date_tree = self.createTree([
            '表格行号', '视频名称', '当前完成日期', '应为上传日期', '任务类型', '网盘链接'
        ])
        self.tabs.addTab(self.missing_tree, '未填写（0）')
        self.tabs.addTab(self.blank_type_tree, '视频类型为空（0）')
        self.tabs.addTab(self.duplicate_tree, '重复登记（0）')
        self.tabs.addTab(self.wrong_date_tree, '历史补填日期有误（0）')
        layout.addWidget(self.tabs, 1)

        hint = QtWidgets.QLabel(
            '“一键补填/修正”处理当前任务类型下的未填写视频，并修正旧版自查功能'
            '曾错误写成同一天的完成日期；视频类型为空和重复登记不会自动修改。'
            '双击条目可打开对应网盘文件。',
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        button_layout = QtWidgets.QHBoxLayout()
        self.open_sheet_button = QtWidgets.QPushButton('打开任务提交表格', self)
        self.open_sheet_button.clicked.connect(self.openSheet)
        button_layout.addWidget(self.open_sheet_button)
        self.refresh_button = QtWidgets.QPushButton('重新检查', self)
        self.refresh_button.clicked.connect(self.refresh)
        button_layout.addWidget(self.refresh_button)
        button_layout.addStretch(1)
        self.repair_button = QtWidgets.QPushButton('一键补填/修正当前筛选', self)
        self.repair_button.clicked.connect(self.repairCurrentFilter)
        button_layout.addWidget(self.repair_button)
        self.close_button = QtWidgets.QPushButton('关闭', self)
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.close_button)
        layout.addLayout(button_layout)

        self.populate()

    def createTree(self, headers):
        tree = QtWidgets.QTreeWidget(self)
        tree.setColumnCount(len(headers))
        tree.setHeaderLabels(headers)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        tree.itemDoubleClicked.connect(self.openItem)
        header = tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        return tree

    def populateTree(self, tree, items, row_builder):
        tree.clear()
        for data in items:
            item = QtWidgets.QTreeWidgetItem(row_builder(data))
            item.setData(0, self.item_role, dict(data))
            item.setToolTip(0, str(data.get('task_name') or ''))
            tree.addTopLevelItem(item)

    def populate(self):
        missing = list(self.result.get('missing', []))
        blank_type = list(self.result.get('blank_type', []))
        duplicates = list(self.result.get('duplicates', []))
        wrong_date = list(self.result.get('wrong_date', []))
        self.summary_label.setText(
            '上传历史中共找到 {history_count} 个当前视频；任务提交表已登记 '
            '{present_count} 个；未填写 {missing_count} 个；视频类型为空 '
            '{blank_count} 个；重复登记 {duplicate_count} 个；历史补填日期有误 '
            '{wrong_date_count} 个。'.format(
                history_count=self.result.get('history_count', 0),
                present_count=self.result.get('present_count', 0),
                missing_count=len(missing),
                blank_count=len(blank_type),
                duplicate_count=len(duplicates),
                wrong_date_count=len(wrong_date),
            )
        )
        previous_filter = self.type_filter.currentText()
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem('全部类型')
        self.type_filter.addItems(self.result.get('type_values', []))
        previous_index = self.type_filter.findText(previous_filter)
        self.type_filter.setCurrentIndex(max(0, previous_index))
        self.type_filter.blockSignals(False)

        self.populateTree(
            self.missing_tree,
            missing,
            lambda data: [
                str(data.get('file_name') or ''),
                str(data.get('expected_type') or '（未设置）'),
                str(data.get('admin') or ''),
                str(data.get('task_date') or ''),
                str(data.get('task_id') or ''),
                '双击打开' if data.get('drive_link') else '',
            ],
        )
        self.populateTree(
            self.blank_type_tree,
            blank_type,
            lambda data: [
                str(data.get('sheet_row') or ''),
                str(data.get('file_name') or ''),
                str(data.get('expected_type') or '（历史中也未设置）'),
                str(data.get('admin') or ''),
                '双击打开' if (data.get('sheet_link') or data.get('drive_link')) else '',
            ],
        )
        self.populateTree(
            self.duplicate_tree,
            duplicates,
            lambda data: [
                str(data.get('sheet_row') or ''),
                str(data.get('file_name') or ''),
                str(data.get('actual_type') or data.get('expected_type') or ''),
                str(data.get('admin') or ''),
                '双击打开' if (data.get('sheet_link') or data.get('drive_link')) else '',
            ],
        )
        self.populateTree(
            self.wrong_date_tree,
            wrong_date,
            lambda data: [
                str(data.get('sheet_row') or ''),
                str(data.get('file_name') or ''),
                str(data.get('actual_completed_at') or ''),
                str(data.get('expected_completed_at') or ''),
                str(data.get('actual_type') or data.get('expected_type') or '（未设置）'),
                '双击打开' if (data.get('sheet_link') or data.get('drive_link')) else '',
            ],
        )
        self.applyFilter()

    def filteredItems(self, tree):
        return [
            tree.topLevelItem(index).data(0, self.item_role)
            for index in range(tree.topLevelItemCount())
            if not tree.topLevelItem(index).isHidden()
        ]

    def applyFilter(self, *_args):
        selected_type = self.type_filter.currentText()
        visible_counts = []
        for tree in (
            self.missing_tree,
            self.blank_type_tree,
            self.duplicate_tree,
            self.wrong_date_tree,
        ):
            visible = 0
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                data = item.data(0, self.item_role) or {}
                hidden = (
                    selected_type != '全部类型'
                    and str(data.get('filter_type') or '') != selected_type
                )
                item.setHidden(hidden)
                if not hidden:
                    visible += 1
            visible_counts.append(visible)
        self.tabs.setTabText(0, f'未填写（{visible_counts[0]}）')
        self.tabs.setTabText(1, f'视频类型为空（{visible_counts[1]}）')
        self.tabs.setTabText(2, f'重复登记（{visible_counts[2]}）')
        self.tabs.setTabText(3, f'历史补填日期有误（{visible_counts[3]}）')
        self.repair_button.setEnabled(
            self.worker is None and (visible_counts[0] + visible_counts[3]) > 0
        )

    def openItem(self, item, _column=0):
        data = item.data(0, self.item_role) or {}
        link = str(data.get('sheet_link') or data.get('drive_link') or '').strip()
        if not link or not QtGui.QDesktopServices.openUrl(QtCore.QUrl(link)):
            QMessageBox.warning(self, '打开网盘文件', '这个条目没有可打开的网盘链接。')

    def openSheet(self):
        url = str(self.result.get('sheet_url') or '').strip()
        if not url or not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            QMessageBox.warning(self, '打开任务提交表格', '无法打开任务提交表格链接。')

    def setBusy(self, busy, text=''):
        self.progress_label.setText(text)
        self.type_filter.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.open_sheet_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if busy:
            self.repair_button.setEnabled(False)
        else:
            self.applyFilter()

    def startWorker(self, operation, items=None):
        if self.worker is not None:
            return
        self.setBusy(True, '正在补填并复查……' if operation == 'repair' else '正在重新检查……')
        thread = TaskSubmissionAuditThread(
            self.config,
            operation=operation,
            items=items,
            parent=self,
        )
        parent = self.parent()
        if hasattr(parent, 'appendLog'):
            thread.log.connect(
                lambda message: parent.appendLog(f'[任务表自查] {message}', end='')
            )
        thread.succeeded.connect(self.onWorkerSucceeded)
        thread.failed.connect(self.onWorkerFailed)
        connect_thread_finished(thread, self, 'worker', self.onWorkerFinished, pass_thread=True)
        self.worker = thread
        thread.start()

    def refresh(self):
        self.startWorker('scan')

    def repairCurrentFilter(self):
        items = self.filteredItems(self.missing_tree)
        items.extend(self.filteredItems(self.wrong_date_tree))
        if not items:
            QMessageBox.information(
                self,
                '一键补填/修正',
                '当前筛选下没有缺失视频或需要修正的历史补填日期。',
            )
            return
        selected_type = self.type_filter.currentText()
        answer = QMessageBox.question(
            self,
            '确认一键补填/修正',
            f'将处理当前筛选“{selected_type}”下的 {len(items)} 个条目：'
            '补填缺失视频，或把旧版自查写错的完成日期恢复为实际上传日期。'
            '\n\n视频类型为空和重复登记的条目不会自动修改。继续吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.startWorker('repair', items)

    def onWorkerSucceeded(self, result):
        self.result = dict(result)
        self.populate()
        repair_result = result.get('repair_result')
        if repair_result is not None:
            report = repair_result.get('report', {})
            failure_count = len(report.get('failed_files', []))
            date_failure_count = len(repair_result.get('date_failures', []))
            QMessageBox.information(
                self,
                '一键补填/修正完成',
                '本次补填 {} 个，修正历史日期 {} 个，失败 {} 个。'
                '列表已经重新检查。'.format(
                    repair_result.get('write_count', 0),
                    repair_result.get('date_updated_count', 0),
                    failure_count + date_failure_count,
                ),
            )

    def onWorkerFailed(self, message, traceback_text):
        parent = self.parent()
        if hasattr(parent, 'appendLog'):
            parent.appendLog(
                f'[任务表自查] 操作失败：{message}\n{traceback_text}',
                end='',
                level=logging.ERROR,
            )
        QMessageBox.critical(
            self,
            '任务提交表自查失败',
            f'{message}\n\n详细信息已写入程序日志。',
        )

    def onWorkerFinished(self, thread=None):
        thread = thread if thread is not None else self.sender()
        if self.worker is not thread:
            return
        if self.worker is thread:
            self.worker = None
        self.setBusy(False)
        thread.deleteLater()

    def reject(self):
        if self.worker is not None and thread_is_active(self.worker):
            QMessageBox.information(self, '任务提交表自查', '正在读写表格，请等待操作完成。')
            return
        super().reject()

    def accept(self):
        if self.worker is not None and thread_is_active(self.worker):
            QMessageBox.information(self, '任务提交表自查', '正在读写表格，请等待操作完成。')
            return
        super().accept()
