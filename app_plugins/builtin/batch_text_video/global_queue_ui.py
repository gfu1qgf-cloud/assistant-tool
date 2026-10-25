"""An embedded render queue: global plans above the current project's jobs."""
from qt_compat import QtCore, QtGui, QtWidgets
from .property_sections import PropertySection
from .row_progress import install_row_progress, set_row_progress
from .render_progress import summary


STATUS = {'staged': '待开始', 'submitted': '已提交', 'succeeded': '已完成',
          'failed': '需处理', 'canceled': '已取消', 'interrupted': '需核对',
          'queued': '排队中', 'running': '渲染中', 'canceling': '正在取消'}


class GlobalRenderQueuePanel(QtWidgets.QWidget):
    def __init__(self, owner, settings_widget):
        super().__init__(owner)
        self.owner = owner
        self.plans = {}
        self.pending_removals = set()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        settings_scroll = QtWidgets.QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setWidget(settings_widget)
        settings_scroll.setMaximumHeight(270)
        self.settings_section = PropertySection('渲染选项', settings_scroll, expanded=False)
        self.settings_section.toggle.setToolTip('展开设置并发上限（1–64路）、编码质量、同名文件覆盖及FFmpeg。')
        layout.addWidget(self.settings_section)
        global_widget = QtWidgets.QWidget()
        global_layout = QtWidgets.QVBoxLayout(global_widget)
        global_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['项目／视频数', '状态', '进度', '用时'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 65)
        self.table.setColumnWidth(3, 82)
        self.table.setMinimumHeight(95)
        self.table.setMaximumHeight(185)
        self.table.itemActivated.connect(self.open_selected_project)
        self.table.setToolTip('双击计划查看对应项目的任务。')
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.ActionsContextMenu)
        self.delete_action = QtGui.QAction('删除渲染计划…', self.table)
        self.delete_action.setShortcut(QtGui.QKeySequence('Delete'))
        self.delete_action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_action.triggered.connect(self.delete_selected)
        self.table.addAction(self.delete_action)
        install_row_progress(self.table)
        global_layout.addWidget(self.table)
        self.empty_label = QtWidgets.QLabel('没有勾选的视频。请在下方生成列表勾选，计划会自动刷新。')
        self.empty_label.setWordWrap(True)
        global_layout.addWidget(self.empty_label)
        row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton('开始全部计划')
        self.start_button.setAutoDefault(False)
        self.start_button.setToolTip('开始所有项目中已勾选、尚未提交的渲染计划。仅查看、搜索或选中行不会开始导出。')
        self.start_button.clicked.connect(self.start)
        remove = QtWidgets.QPushButton('取消该项目勾选')
        remove.clicked.connect(self.remove)
        self.delete_button = QtWidgets.QPushButton('删除计划…')
        self.delete_button.setToolTip('从全局列表删除所选计划；保留项目、任务、素材及视频。执行中可先停止，结束后自动移除。')
        self.delete_button.clicked.connect(self.delete_selected)
        self.table.currentItemChanged.connect(self.update_delete_enabled)
        refresh = QtWidgets.QToolButton()
        refresh.setText('↻')
        refresh.setToolTip('刷新所有项目的渲染计划')
        refresh.clicked.connect(self.refresh)
        row.addWidget(self.start_button)
        row.addWidget(remove)
        row.addWidget(self.delete_button)
        row.addStretch()
        row.addWidget(refresh)
        global_layout.addLayout(row)
        self.global_section = PropertySection('全部项目计划', global_widget, expanded=True)
        layout.addWidget(self.global_section)
        self.current_label = QtWidgets.QLabel()
        self.current_label.setWordWrap(True)
        layout.addWidget(self.current_label)
        from .render_timing_ui import RenderTimingPanel
        self.timing = RenderTimingPanel(owner, self)
        layout.addWidget(self.timing)
        from .queue_tools import QueueTools
        self.tools = QueueTools(owner, self)
        layout.addWidget(self.tools)
        layout.addWidget(owner.jobs, 1)
        layout.addWidget(self.tools.detail)
        self.refresh()

    def refresh(self):
        state = self.owner.store.load()
        plans = state.get('render_plans', [])
        self.plans = {plan['id']: plan for plan in plans}
        selected_id = self.selected_id()
        self.table.setRowCount(len(plans))
        self.table.setVisible(bool(plans))
        self.empty_label.setVisible(not plans)
        self.empty_label.setText('尚未创建渲染计划。' if not self.owner.state['jobs'] else
            '未勾选任务：可用下方“勾选未完成”创建渲染计划。')
        self.table.setFixedHeight(self.table.horizontalHeader().height() + min(5, max(1, len(plans)))*28 + 3)
        for row, plan in enumerate(plans):
            profile = next((p for p in state['profiles'] if p['id'] == plan['profile_id']), None)
            data = state if state['active_profile'] == plan['profile_id'] else (profile or {}).get('data') or {}
            completed = sum(job['status'] == '已完成' for job in data.get('jobs', []) if job['id'] in plan['ids'])
            percent = 100 * completed / max(1, len(plan['ids']))
            status = plan['status']
            handle = self.owner._host_plans.get(plan['profile_id'])
            if handle is not None:
                record = handle.manager.get(handle.task_id)
                status = record['status']
                percent = record.get('percent') or 0
                values = self.owner._render_progress.get(plan['profile_id'], {})
                if values:
                    percent = sum(values.values()) / len(values)
            label = STATUS.get(status, status)
            if status == 'running':
                label = summary(getattr(self.owner, '_render_stages', {}).get(plan['profile_id'], {}))
            if status == 'submitted' and handle is None:
                label = '上次未收尾'
            elif plan.get('error') and status == 'staged':
                label = '待重试'
            if plan['id'] in self.pending_removals:
                label = '正在停止，结束后移除'
            values = [f"{plan['name']} · {len(plan['ids'])} 条", label, '', '']
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, plan['id'])
                item.setToolTip(f"{plan['name']} · {len(plan['ids'])} 个视频\n{plan.get('error', '')}\n双击查看此项目")
                if column == 1: item.setToolTip(label)
                self.table.setItem(row, column, item)
            set_row_progress(self.table, row, None if status == 'staged' else percent)
            if plan['id'] == selected_id:
                self.table.selectRow(row)
        self.refresh_current_label()
        self.timing.refresh()
        self.refresh_settings_summary()
        self.tools.refresh()
        self.start_button.setEnabled(any(plan['status'] == 'staged' for plan in plans))
        self.update_delete_enabled()

    def refresh_settings_summary(self):
        controls = self.owner.controls
        encoder = {'cpu': 'CPU', 'nvenc': 'NVIDIA', 'qsv': 'Intel', 'auto': '自动'}.get(controls['video_encoder'].currentData(), '')
        quality = {'high': '高画质', 'medium': '均衡', 'low': '小体积'}.get(controls['export_quality'].currentData(), '')
        self.settings_section.toggle.setText(f"渲染选项 · 上限{controls['export_workers'].value()}路 · {encoder} · {quality}")

    def update_delete_enabled(self, *_):
        enabled = bool(self.selected_id()) and self.selected_id() not in self.pending_removals
        self.delete_action.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def delete_selected(self):
        identifier = self.selected_id()
        if not identifier or identifier in self.pending_removals: return
        plan = self.plans.get(identifier)
        if plan is None: return
        try:
            if not self.owner.is_batch_busy() and not self.owner.persist(): return
            handle = self.owner._host_plans.get(plan['profile_id'])
            active = (handle is not None and handle.task_id == plan.get('task_id') and handle.isRunning())
            message = ('项目“'+plan['name']+'”正在渲染。是否停止该任务，待结束后自动删除渲染计划？'
                if active else '确定从全局列表删除“'+plan['name']+'”的渲染计划吗？')
            answer = QtWidgets.QMessageBox.question(self, '删除渲染计划',
                message+'\n\n项目配置、任务、素材和已导出的视频均保留。',
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
            if active:
                self.pending_removals.add(identifier)
                handle.requestInterruption()
                if identifier in self.pending_removals:
                    self.owner.status.setText('正在停止所选渲染任务，结束后自动移除计划。')
            else:
                self._delete_saved_plan(identifier)
                self.owner.state = self.owner.store.load()
                self.owner.refresh_tables()
                self.owner.status.setText('已删除渲染计划；项目、任务和视频均保留。')
            self.refresh()
        except Exception as error:
            self.pending_removals.discard(identifier)
            QtWidgets.QMessageBox.warning(self, '无法删除计划', str(error))

    def _delete_saved_plan(self, identifier, *, finished_task_ids=()):
        from model.BackgroundTasks import TERMINAL
        finished = set(finished_task_ids)
        manager = getattr(self.owner.context, 'background_tasks', None)
        states = getattr(manager, 'completion_states', None)
        if callable(states):
            finished.update(key for key, status in states().items() if status in TERMINAL)
        active = {handle.task_id for handle in self.owner._host_plans.values() if handle.isRunning()}
        self.owner.store.remove_render_plan(identifier, active_task_ids=active, finished_task_ids=finished)

    def finish_pending_removal(self, plan, task_id):
        if not plan or plan['id'] not in self.pending_removals: return
        try:
            self._delete_saved_plan(plan['id'], finished_task_ids={task_id})
            self.owner.status.setText('渲染已停止，计划已移除；项目和已有成品保留。')
        except Exception as error:
            self.owner._log('任务已停止，但计划未移除：'+str(error))
        finally:
            self.pending_removals.discard(plan['id'])

    def update_progress(self, profile_id):
        if profile_id == self.owner.state['active_profile']:
            self.refresh_current_label()
        values = self.owner._render_progress.get(profile_id, {})
        if not values:
            return
        for row in range(self.table.rowCount()):
            identifier = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            if self.plans.get(identifier, {}).get('profile_id') == profile_id:
                set_row_progress(self.table, row, sum(values.values()) / len(values))
                text = summary(getattr(self.owner, '_render_stages', {}).get(profile_id, {}))
                self.table.item(row, 1).setText(text)
                self.table.item(row, 1).setToolTip(text)

    def refresh_current_label(self):
        profile_id = self.owner.state['active_profile']
        profile = next(p for p in self.owner.state['profiles'] if p['id'] == profile_id)
        values = self.owner._render_progress.get(profile_id, {})
        if values:
            stages = getattr(self.owner, '_render_stages', {}).get(profile_id, {})
            completed = sum(stages.get(key) == '已完成' for key in values)
            failed = sum(stages.get(key) == '需处理' for key in values)
            waiting = sum(stages.get(key, '排队中') == '排队中' for key in values)
            active = len(values) - completed - failed - waiting
            text = (f"已保存 {completed}/{len(values)} · 处理中 {active} · 排队 {waiting}"
                    + (f" · 需处理 {failed}" if failed else '')
                    + '\n渲染中可滚动查看全部任务；勾选与任务内容暂时锁定。')
        else:
            from .render_selection import identifiers
            text = (f"{len(self.owner.state['jobs'])} 条视频 · 已勾选 {len(identifiers(self.owner.state))} 条"
                    '\n勾选才渲染；取消勾选不删除任务或成品。')
        self.current_label.setText(f"当前项目：{profile['name']} · {text}")

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
                plan=self.plans[identifier]
                if not self.owner.is_batch_busy() and not self.owner.persist(): return
                self.owner.store.clear_render_selection(plan['profile_id'])
                self.owner.state=self.owner.store.load()
                self.owner.refresh_tables()
            except Exception as error:
                QtWidgets.QMessageBox.warning(self, '无法移除', str(error))
            self.refresh()
