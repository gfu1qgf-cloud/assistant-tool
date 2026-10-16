"""An embedded render queue: global plans above the current project's jobs."""
from qt_compat import QtCore, QtWidgets
from .property_sections import PropertySection
from .row_progress import install_row_progress, set_row_progress


STATUS = {'staged': '待开始', 'submitted': '已提交', 'succeeded': '已完成',
          'failed': '需处理', 'canceled': '已取消', 'interrupted': '需核对',
          'queued': '排队中', 'running': '渲染中', 'canceling': '正在取消'}


class GlobalRenderQueuePanel(QtWidgets.QWidget):
    def __init__(self, owner, settings_widget):
        super().__init__(owner)
        self.owner = owner
        self.plans = {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        settings_scroll = QtWidgets.QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setWidget(settings_widget)
        settings_scroll.setMaximumHeight(270)
        self.settings_section = PropertySection('全局渲染设置', settings_scroll)
        layout.addWidget(self.settings_section)
        global_widget = QtWidgets.QWidget()
        global_layout = QtWidgets.QVBoxLayout(global_widget)
        global_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['项目／视频数', '状态', '进度'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 55)
        self.table.setMinimumHeight(95)
        self.table.setMaximumHeight(185)
        self.table.itemActivated.connect(self.open_selected_project)
        self.table.setToolTip('双击计划查看对应项目的任务。')
        install_row_progress(self.table)
        global_layout.addWidget(self.table)
        self.empty_label = QtWidgets.QLabel('还没有渲染计划。可在下方把当前项目加入队列。')
        self.empty_label.setWordWrap(True)
        global_layout.addWidget(self.empty_label)
        row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton('开始全部')
        self.start_button.clicked.connect(self.start)
        remove = QtWidgets.QPushButton('移除计划')
        remove.clicked.connect(self.remove)
        refresh = QtWidgets.QToolButton()
        refresh.setText('↻')
        refresh.setToolTip('刷新所有项目的渲染计划')
        refresh.clicked.connect(self.refresh)
        row.addWidget(self.start_button)
        row.addWidget(remove)
        row.addStretch()
        row.addWidget(refresh)
        global_layout.addLayout(row)
        self.global_section = PropertySection('全部项目计划', global_widget, expanded=True)
        layout.addWidget(self.global_section)
        self.current_label = QtWidgets.QLabel()
        self.current_label.setWordWrap(True)
        layout.addWidget(self.current_label)
        layout.addWidget(owner.jobs, 1)
        self.refresh()

    def refresh(self):
        state = self.owner.store.load()
        plans = state.get('render_plans', [])
        self.plans = {plan['id']: plan for plan in plans}
        selected_id = self.selected_id()
        self.table.setRowCount(len(plans))
        self.table.setVisible(bool(plans))
        self.empty_label.setVisible(not plans)
        self.table.setFixedHeight(self.table.horizontalHeader().height() + min(5, max(1, len(plans)))*28 + 3)
        for row, plan in enumerate(plans):
            profile = next((p for p in state['profiles'] if p['id'] == plan['profile_id']), None)
            data = state if state['active_profile'] == plan['profile_id'] else (profile or {}).get('data') or {}
            completed = sum(job['status'] == '已完成' for job in data.get('jobs', []) if job['id'] in plan['ids'])
            percent = round(100 * completed / max(1, len(plan['ids'])))
            status = plan['status']
            handle = self.owner._host_plans.get(plan['profile_id'])
            if handle is not None:
                record = handle.manager.get(handle.task_id)
                status = record['status']
                percent = record.get('percent') or 0
            label = STATUS.get(status, status)
            if status == 'submitted' and handle is None:
                label = '上次未收尾'
            elif plan.get('error') and status == 'staged':
                label = '待重试'
            values = [f"{plan['name']} · {len(plan['ids'])} 条", label, '']
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, plan['id'])
                item.setToolTip(f"{plan['name']} · {len(plan['ids'])} 个视频\n{plan.get('error', '')}\n双击查看此项目")
                self.table.setItem(row, column, item)
            set_row_progress(self.table, row, None if status == 'staged' else percent)
            if plan['id'] == selected_id:
                self.table.selectRow(row)
        profile = next(p for p in self.owner.state['profiles'] if p['id'] == self.owner.state['active_profile'])
        self.current_label.setText(f"当前项目：{profile['name']} · {len(self.owner.state['jobs'])} 条视频")
        self.start_button.setEnabled(any(plan['status'] == 'staged' for plan in plans))

    def update_progress(self, profile_id):
        values = self.owner._render_progress.get(profile_id, {})
        if not values:
            return
        for row in range(self.table.rowCount()):
            identifier = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            if self.plans.get(identifier, {}).get('profile_id') == profile_id:
                set_row_progress(self.table, row, sum(values.values()) / len(values))
                self.table.item(row, 1).setText('渲染中')

    def selected_id(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item else ''

    def open_selected_project(self, *_args):
        plan = self.plans.get(self.selected_id())
        if plan is not None:
            self.owner.switch_profile(plan['profile_id'])
            self.owner.right_tabs.setCurrentWidget(self)

    def start(self):
        self.owner.start_staged_plans()
        self.refresh()

    def remove(self):
        identifier = self.selected_id()
        if identifier:
            try:
                active_ids = [handle.task_id for handle in self.owner._host_plans.values()]
                self.owner.store.remove_render_plan(identifier, active_task_ids=active_ids)
            except Exception as error:
                QtWidgets.QMessageBox.warning(self, '无法移除', str(error))
            self.refresh()
