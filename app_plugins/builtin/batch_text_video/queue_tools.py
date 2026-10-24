"""Find and select jobs without editing the project or reading media files."""
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo

TERMINAL = {'已完成', '需处理', '已取消', '已结束'}
WAITING = {'待生成', '排队中', '待匹配', '匹配失败', ''}


def is_active(status):
    return status not in TERMINAL | WAITING and not status.startswith('需')


class QueueTools(QtWidgets.QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.visible_rows = set()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.filter = Combo()
        self.filter.setAccessibleName('筛选任务状态')
        for label, key in (('全部任务', 'all'), ('未完成', 'unfinished'), ('处理中', 'active'),
                           ('需处理', 'failed'), ('已完成', 'completed')):
            self.filter.addItem(label, key)
        if any(job['status'] != '已完成' for job in owner.state['jobs']):
            self.filter.setCurrentIndex(self.filter.findData('unfinished'))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索任务／成品名称')
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('搜索任务或成品名称')
        self.jump_button = QtWidgets.QPushButton('定位处理中')
        self.jump_button.setAutoDefault(False)
        self.jump_button.clicked.connect(self.jump_to_active)
        row.addWidget(self.filter)
        row.addWidget(self.search, 1)
        row.addWidget(self.jump_button)
        layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.unfinished_button = QtWidgets.QPushButton('勾选未完成')
        self.unfinished_button.setAutoDefault(False)
        self.unfinished_button.setToolTip('仅在当前项目勾选未完成／需处理任务，取消已完成任务的勾选；不删除记录或成品。')
        self.unfinished_button.clicked.connect(lambda: self.set_checks('unfinished'))
        self.selection_button = QtWidgets.QToolButton()
        self.selection_button.setText('批量勾选')
        self.selection_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.selection_button)
        for text, key in (('勾选全部任务', 'all'), ('取消全部勾选', 'none'), ('仅勾选筛选结果', 'visible')):
            menu.addAction(text, lambda _=False, k=key: self.set_checks(k))
        self.selection_button.setMenu(menu)
        self.result_count = QtWidgets.QLabel()
        row.addWidget(self.unfinished_button)
        row.addWidget(self.selection_button)
        row.addStretch()
        row.addWidget(self.result_count)
        layout.addLayout(row)
        self.detail = QtWidgets.QLabel()
        self.detail.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMaximumHeight(46)
        self.empty_hint = QtWidgets.QWidget()
        empty_row = QtWidgets.QHBoxLayout(self.empty_hint)
        empty_row.setContentsMargins(0, 0, 0, 0)
        self.empty_text = QtWidgets.QLabel('当前筛选没有任务。')
        self.clear_filter_button = QtWidgets.QPushButton('显示全部任务')
        self.clear_filter_button.setAutoDefault(False)
        self.clear_filter_button.clicked.connect(self.show_all)
        empty_row.addWidget(self.empty_text, 1)
        empty_row.addWidget(self.clear_filter_button)
        layout.addWidget(self.empty_hint)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        owner.jobs.itemChanged.connect(self._item_changed)
        owner.jobs.currentCellChanged.connect(self.refresh_detail)

    def status(self, row):
        item = self.owner.jobs.item(row, 1)
        return item.text() if item else ''

    def matches(self, row):
        status = self.status(row)
        mode = self.filter.currentData()
        if mode == 'completed' and status != '已完成': return False
        if mode == 'unfinished' and status == '已完成': return False
        if mode == 'failed' and not status.startswith('需') and status != '匹配失败': return False
        if mode == 'active' and not is_active(status): return False
        query = self.search.text().strip().casefold()
        if query:
            item = self.owner.jobs.item(row, 0)
            # Names/titles are already in memory. Filtering never opens media.
            job = self.owner.state['jobs'][row] if row < len(self.owner.state['jobs']) else {}
            text = ' '.join([item.text() if item else '', job.get('name', ''), job.get('title', '')])
            if query not in text.casefold(): return False
        return True

    def refresh(self, *_):
        self.visible_rows.clear()
        table = self.owner.jobs
        table.setUpdatesEnabled(False)
        try:
            for row in range(table.rowCount()): self.update_row(row, detail=False)
        finally:
            table.setUpdatesEnabled(True)
        self.refresh_empty_hint()
        self.refresh_detail()
        self.set_editable(not self.owner.is_batch_busy())

    def update_row(self, row, *, detail=True):
        visible = self.matches(row)
        if self.owner.jobs.isRowHidden(row) == visible:
            self.owner.jobs.setRowHidden(row, not visible)
        if visible: self.visible_rows.add(row)
        else: self.visible_rows.discard(row)
        self.result_count.setText(f'显示 {len(self.visible_rows)}/{self.owner.jobs.rowCount()}')
        self.refresh_empty_hint()
        if detail and self.owner.jobs.currentRow() == row: self.refresh_detail()

    def _item_changed(self, item):
        if not getattr(self.owner, '_loading', True) and item.column() in (1, 2):
            self.update_row(item.row())

    def refresh_detail(self, *_):
        row = self.owner.jobs.currentRow()
        item = self.owner.jobs.item(row, 0)
        self.detail.setText(f'{item.text()} · {self.status(row)}' if item else '')
        self.detail.setVisible(item is not None)

    def set_editable(self, editable):
        has_jobs = self.owner.jobs.rowCount() > 0
        self.unfinished_button.setEnabled(editable and has_jobs)
        self.selection_button.setEnabled(editable and has_jobs)
        self.jump_button.setEnabled(has_jobs)

    def refresh_empty_hint(self):
        has_jobs = self.owner.jobs.rowCount() > 0
        self.empty_text.setText('当前筛选没有任务。' if has_jobs else '还没有任务：添加素材后，点击顶部“批量搭配…”。')
        self.empty_text.setWordWrap(True)
        self.clear_filter_button.setVisible(has_jobs)
        self.empty_hint.setVisible(not self.visible_rows)

    def jump_to_active(self):
        table = self.owner.jobs
        for row in range(table.rowCount()):
            if is_active(self.status(row)):
                if table.isRowHidden(row):
                    self.show_all()
                table.setCurrentCell(row, 0)
                table.scrollToItem(table.item(row, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
                return
        self.owner.status.setText('当前项目没有正在处理的任务。')

    def show_all(self):
        with QtCore.QSignalBlocker(self.filter), QtCore.QSignalBlocker(self.search):
            self.filter.setCurrentIndex(0); self.search.clear()
        self.refresh()

    def set_checks(self, mode):
        owner = self.owner
        if owner.is_batch_busy() or owner._loading: return
        if mode == 'visible' and not self.visible_rows:
            owner.status.setText('没有筛选结果，现有勾选保持不变。')
            return
        previous = {job['id']: (job.get('render_checked'), 'render_checked' in job) for job in owner.state['jobs']}
        for row, job in enumerate(owner.state['jobs']):
            job['render_checked'] = (mode == 'all' or mode == 'unfinished' and self.status(row) != '已完成'
                                     or mode == 'visible' and row in self.visible_rows)
        if not owner.persist():
            for job in owner.state['jobs']:
                value, existed = previous[job['id']]
                if existed: job['render_checked'] = value
                else: job.pop('render_checked', None)
            owner.refresh_tables()
            return
        if not owner.sync_render_selection(): return
        owner.refresh_tables()
        count = sum(job.get('render_checked', False) for job in owner.state['jobs'])
        owner.status.setText(f'当前项目已勾选 {count} 个任务；点击“开始全部计划”导出。' if count else
            '当前项目没有勾选任务；可切换“全部任务”查看已有成品。')
