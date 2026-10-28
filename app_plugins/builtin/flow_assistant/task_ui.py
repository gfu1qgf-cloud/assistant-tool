"""Select registered task numbers, never infer a task from a typed path."""
import copy

from qt_compat import QtCore, QtWidgets


def task_caption(target):
    task = target.get('task', {})
    number = str(task.get('task_id') or '').strip() or '缺少任务编号'
    title = str(task.get('task_name') or '').strip()
    if len(title) > 42:
        title = title[:42] + '…'
    return ' · '.join(value for value in (number, str(task.get('task_type') or '').strip(), title) if value)


class TaskSelector(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()
    refresh_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.targets, self.remembered_directory = [], ''
        layout = QtWidgets.QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('任务编号', self))
        self.search = QtWidgets.QLineEdit(self)
        self.search.setPlaceholderText('搜索任务编号／名称')
        self.search.setMinimumWidth(150); self.search.setMaximumWidth(190); self.search.setClearButtonEnabled(True)
        row.addWidget(self.search)
        self.combo = QtWidgets.QComboBox(self)
        self.combo.setEditable(False)
        self.combo.setMinimumContentsLength(18)
        self.combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.combo, 1)
        self.refresh = QtWidgets.QPushButton('刷新任务', self); self.refresh.setAutoDefault(False)
        self.refresh.clicked.connect(lambda: self.refresh_requested.emit()); row.addWidget(self.refresh)
        layout.addLayout(row)
        self.details = QtWidgets.QLabel(self); self.details.setWordWrap(True)
        self.details.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.combo.currentIndexChanged.connect(self._selected)
        self.search.textChanged.connect(self._filter)
        self._details()

    def set_targets(self, targets, *, preferred_directory=None):
        before = self.combo.currentData()
        self.targets = copy.deepcopy(list(targets))
        if preferred_directory is not None:
            with QtCore.QSignalBlocker(self.search): self.search.clear()
        self._rebuild(preferred_directory or self.remembered_directory, before)

    def _filter(self, *_):
        self._rebuild(self.remembered_directory, self.combo.currentData())

    def _rebuild(self, preferred, before):
        query = self.search.text().strip().casefold()
        visible = [target for target in self.targets if not query or query in ' '.join(
            str(target.get('task', {}).get(key) or '') for key in ('task_id', 'task_name', 'task_type', 'task_date')).casefold()]
        with QtCore.QSignalBlocker(self.combo):
            self.combo.clear()
            self.combo.setPlaceholderText('没有匹配的任务编号' if query else '请先在主界面加载任务')
            for target in visible:
                self.combo.addItem(task_caption(target), target)
                self.combo.setItemData(self.combo.count() - 1,
                    str(target.get('task', {}).get('task_name') or '') + '\n' + target['target_dir'],
                    QtCore.Qt.ItemDataRole.ToolTipRole)
            index = next((i for i, target in enumerate(visible) if target['target_dir'] == preferred), None)
            if index is None:
                index = 0 if visible and (not query or len(visible) == 1) else -1
            self.combo.setCurrentIndex(index)
            if visible and index < 0: self.combo.setPlaceholderText('请选择匹配的任务编号')
        self._details()
        if self.combo.currentData() != before: self.changed.emit()

    def _selected(self, *_):
        self._details(); self.changed.emit()

    def _details(self):
        target = self.combo.currentData()
        if target:
            self.remembered_directory = target['target_dir']
            task = target.get('task', {})
            heading = ' · '.join(str(task.get(key) or '').strip() for key in ('task_date', 'task_id', 'task_name') if task.get(key))
            self.details.setText(heading)
            self.details.setToolTip(heading+'\n任务目录：'+target['target_dir'])
        elif not self.targets:
            self.details.setText('主界面尚未加载任务。请先加载任务表，再点“刷新任务”；不用手动填写目录。')
        elif self.combo.count():
            self.details.setText('有多个匹配任务，请在下拉框中选择任务编号。')
        else:
            self.details.setText('没有匹配的任务，请修改搜索内容。')
