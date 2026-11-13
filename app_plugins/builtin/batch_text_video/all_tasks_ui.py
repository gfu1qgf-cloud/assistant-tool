"""One live, flat export list for every saved project, without switching editors."""
from pathlib import Path
import time
from qt_compat import QtCore, QtGui, QtWidgets
from .row_progress import install_row_progress, set_row_progress
from .render_selection import checked
from .queue_selection import ACTIVE
from .render_timing import clock_text, item_details


class AllTasksPanel(QtWidgets.QWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel, self.owner = panel, panel.owner
        self.entries, self.rows, self.state = {}, {}, {}
        self.visible = set()
        self.wave_runs = []
        self._building = False
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        row = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QComboBox()
        for text,key in (('全部任务','all'),('未完成','unfinished'),('处理中','active'),('需处理','failed'),('已完成','completed')):self.filter.addItem(text,key)
        self.search = QtWidgets.QLineEdit();self.search.setPlaceholderText('搜索项目、任务或文件名');self.search.setClearButtonEnabled(True)
        self.jump = QtWidgets.QPushButton('定位处理中');self.jump.clicked.connect(self.jump_to_active)
        row.addWidget(self.filter);row.addWidget(self.search,1);layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        panel.start_button.setText('开始导出已勾选')
        panel.start_button.setToolTip('直接导出总列表里所有项目已勾选的任务；筛选和选中行不会改变勾选。')
        row.addWidget(panel.start_button)
        self.unfinished = QtWidgets.QToolButton();self.unfinished.setText('批量勾选')
        self.unfinished.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.unfinished.setToolTip('批量修改当前筛选范围的导出勾选；运行中的项目会跳过。')
        self.bulk_menu = QtWidgets.QMenu(self.unfinished)
        self.bulk_actions = {}
        for text,mode in (('仅勾选已完成','completed'),('仅勾选未完成','unfinished'),('勾选当前筛选结果','visible'),('取消当前筛选勾选','clear')):
            self.bulk_actions[mode]=self.bulk_menu.addAction(text,lambda _=False,m=mode:self.bulk(m))
        self.unfinished.setMenu(self.bulk_menu)
        self.uncheck = QtWidgets.QPushButton('取消所选勾选');self.uncheck.clicked.connect(lambda:self.bulk('none'))
        self.stop = QtWidgets.QPushButton('停止全部');self.stop.clicked.connect(self.stop_all)
        self.retry = QtWidgets.QPushButton('重试失败');self.retry.clicked.connect(self.retry_failed)
        self.retry.setToolTip('仅重试当前筛选范围内的失败任务；保留原素材分配和其他任务勾选。运行中的项目需等本批结束。')
        self.delete = QtWidgets.QPushButton('删除所选');self.delete.clicked.connect(self.delete_selected)
        self.delete.setToolTip('删除选中任务的队列记录；保留原素材和已导出视频。快捷键：Delete')
        row.addWidget(self.retry);row.addWidget(self.stop);row.addWidget(self.jump);row.addStretch();layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        for button in (self.unfinished,self.uncheck,self.delete):row.addWidget(button)
        for button in (panel.start_button,self.uncheck,self.stop,self.delete,self.jump,self.retry):button.setAutoDefault(False)
        row.addStretch();layout.addLayout(row)
        self.summary = QtWidgets.QLabel();self.summary.setWordWrap(True);layout.addWidget(self.summary)
        self.table = QtWidgets.QTableWidget(0,5)
        self.table.setObjectName('allExportTasks')
        self.table.setHorizontalHeaderLabels(['任务／成品文件名','项目','状态','进度','耗时'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(44)
        for col,width in ((1,72),(2,86),(3,48),(4,66)):self.table.setColumnWidth(col,width)
        self.table.verticalHeader().setVisible(False)
        install_row_progress(self.table)
        self.table.itemChanged.connect(self.changed)
        self.table.currentCellChanged.connect(self.detail_changed)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _:self.preview_current())
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        self.delete_action = QtGui.QAction('删除所选',self.table)
        self.delete_action.setShortcut(QtGui.QKeySequence('Delete'))
        self.delete_action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_action.triggered.connect(self.delete_selected)
        self.table.addAction(self.delete_action)
        self.empty = QtWidgets.QWidget()
        empty_row = QtWidgets.QHBoxLayout(self.empty);empty_row.setContentsMargins(0,0,0,0)
        self.empty_text = QtWidgets.QLabel();empty_row.addWidget(self.empty_text,1)
        self.reset_filter = QtWidgets.QPushButton('显示全部任务')
        self.reset_filter.clicked.connect(lambda:(self.filter.setCurrentIndex(0),self.search.clear()))
        self.add_tasks = QtWidgets.QPushButton('批量搭配…');self.add_tasks.clicked.connect(self.owner.compose_queue)
        empty_row.addWidget(self.reset_filter);empty_row.addWidget(self.add_tasks)
        layout.addWidget(self.empty)
        layout.addWidget(self.table,1)
        self.detail = QtWidgets.QLabel();self.detail.setWordWrap(True);self.detail.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.detail.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMaximumHeight(52);layout.addWidget(self.detail)
        self.filter.currentIndexChanged.connect(self.apply_filter)
        self.search.textChanged.connect(self.apply_filter)
        self.timer=QtCore.QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.tick);self.timer.start()

    def local_busy(self):
        owner=self.owner
        return owner.is_batch_busy() and owner.state['active_profile'] not in owner._host_plans

    def locked(self,profile_id):
        return self.local_busy() or profile_id in self.owner._host_plans or any(
            p['profile_id']==profile_id and p['status'] in ACTIVE for p in self.state.get('render_plans',[]))

    def refresh(self,state):
        self.state=state
        entries={}
        from .export_destination import settings_for
        for profile in state['profiles']:
            data=state if profile['id']==state['active_profile'] else profile.get('data') or {}
            for job in data.get('jobs',[]):
                entries[(profile['id'],job['id'])]={'profile':profile,'job':job,'settings':data.get('settings',{})}
        keys=list(entries)
        selection={tuple(self.table.item(row,0).data(QtCore.Qt.ItemDataRole.UserRole)) for row in
            {i.row() for i in self.table.selectedIndexes()} if self.table.item(row,0)}
        scroll=self.table.verticalScrollBar().value()
        self._building=True;self.table.setUpdatesEnabled(False)
        try:
            if keys!=list(self.rows):
                self.table.clearSelection()
                self.table.setRowCount(len(keys));self.rows={key:row for row,key in enumerate(keys)}
                for key,row in self.rows.items():
                    for column in range(5):
                        item=QtWidgets.QTableWidgetItem();item.setData(QtCore.Qt.ItemDataRole.UserRole,key)
                        self.table.setItem(row,column,item)
            self.entries=entries
            for key in keys:
                self.update_row(key)
                if key in selection:
                    for column in range(5):self.table.item(self.rows[key],column).setSelected(True)
            self.table.verticalScrollBar().setValue(scroll)
        finally:self.table.setUpdatesEnabled(True);self._building=False
        self.apply_filter();self.tick()
        self.panel.start_button.setEnabled(any(not self.locked(key[0]) and self.is_checked(key) for key in keys))

    def name(self,entry):
        job=entry['job']
        if job.get('status')=='已完成' and job.get('output'):
            filename=Path(job['output']).name
            return str(job['task_id'])+' · '+filename if job.get('task_id') else filename
        from .export_destination import output_stem
        settings=dict(entry['settings'])
        control=self.owner.controls.get('output_project_prefix')
        settings['output_project_prefix']=control.isChecked() if control is not None else self.state.get('render_settings',{}).get('output_project_prefix',False)
        # Filename preview does not resolve destinations or read media.
        settings['_output_project_name']=entry['profile']['name']
        try:raw=output_stem(job,settings)
        except ValueError as error:return '文件名需调整：'+str(error)
        import re
        raw=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',raw).strip(' .')
        raw=raw if raw.lower().endswith('.mp4') else raw+'.mp4'
        label=str(job.get('task_id') or job.get('label') or '')
        return label+' · '+raw if label and label not in {job.get('name'),raw} else raw

    def refresh_names(self):
        if not self.entries:return
        self._building=True
        try:
            for key,entry in self.entries.items():self.table.item(self.rows[key],0).setText(self.name(entry))
        finally:self._building=False
        self.apply_filter()

    def is_checked(self,key):return checked(self.entries[key]['job'],self.state.get('render_plans',()),key[0])

    def status(self,key):
        job=self.entries[key]['job'];pid,jid=key
        stages=self.owner._render_stages.get(pid,{})
        plans=[p for p in self.state.get('render_plans',[]) if p['profile_id']==pid and jid in p['ids']]
        live=next((p for p in reversed(plans) if p['status'] in ACTIVE),None)
        live_item = jid in self.owner._render_progress.get(pid,{})
        local = self.owner.worker is not None and pid==self.owner.state['active_profile']
        if live and pid not in self.owner._host_plans:
            return '需核对（上次未收尾）'
        if live or live_item and (pid in self.owner._host_plans or local):
            return stages.get(jid,'排队中')
        if any(p.get('error') and p['status']=='staged' for p in plans):return '需处理'
        if any(p['status']=='staged' for p in plans) and self.is_checked(key) and job.get('status')=='已完成':
            return '待开始（重新导出）'
        return job.get('status','待生成')

    def update_row(self,key):
        if key not in self.rows:return
        row=self.rows[key];entry=self.entries[key];pid,jid=key
        item=self.table.item(row,0)
        item.setText(self.name(entry));item.setToolTip(entry['job'].get('output') or item.text())
        flags=QtCore.Qt.ItemFlag.ItemIsSelectable|QtCore.Qt.ItemFlag.ItemIsEnabled
        if not self.locked(pid):flags|=QtCore.Qt.ItemFlag.ItemIsUserCheckable
        item.setFlags(flags)
        item.setCheckState(QtCore.Qt.CheckState.Checked if self.is_checked(key) else QtCore.Qt.CheckState.Unchecked)
        self.table.item(row,1).setText(entry['profile']['name'])
        text=self.status(key);self.table.item(row,2).setText(text)
        self.table.item(row,2).setToolTip(self.error(key) or text)
        failed=text.startswith('需') or text=='匹配失败'
        self.table.item(row,2).setForeground(QtGui.QBrush(QtGui.QColor('#b91c1c')) if failed else QtGui.QBrush())
        percent=self.owner._render_progress.get(pid,{}).get(jid)
        if text=='已完成':percent=100
        elif failed or text in {'待开始（重新导出）','待生成','待匹配','已取消'}:percent=None
        elif percent is not None:percent=min(99,percent)
        set_row_progress(self.table,row,percent,column=3)
        self.update_time(key)

    def error(self,key):
        job=self.entries[key]['job']
        if job.get('error'):
            if job['error'].strip()=='内存AI抠像失败：':return '视频处理失败；旧记录未保存详细原因，请查看日志或重试。'
            return job['error']
        return next((p['error'] for p in reversed(self.state.get('render_plans',[]))
            if p['profile_id']==key[0] and key[1] in p['ids'] and p.get('error') and p['status']=='staged'),'')

    def update_time(self,key):
        value=self.panel.timing.item_value(key[0],self.entries[key]['job'])
        item=self.table.item(self.rows[key],4);item.setText(clock_text(value['elapsed_seconds']) if value else '—');item.setToolTip(item_details(value))

    def progress(self,profile_id,job_id=None):
        keys=[(profile_id,job_id)] if job_id is not None else [k for k in self.rows if k[0]==profile_id]
        self._building=True
        try:
            for key in keys:
                if key in self.rows:self.update_row(key);self.filter_row(key)
        finally:self._building=False
        self.update_empty();self.detail_changed()

    def matches(self,key):
        text=self.status(key);mode=self.filter.currentData()
        done=text=='已完成';failed=text.startswith('需') or text=='匹配失败'
        active=key[0] in self.owner._host_plans and text not in {'已完成','需处理','已取消','排队中'}
        if (mode=='unfinished' and done or mode=='completed' and not done or mode=='failed' and not failed or mode=='active' and not active):return False
        query=self.search.text().strip().casefold();entry=self.entries[key]
        return not query or query in ' '.join((entry['profile']['name'],entry['job'].get('name',''),entry['job'].get('title',''),self.table.item(self.rows[key],0).text())).casefold()

    def filter_row(self,key):
        visible=self.matches(key)
        self.table.setRowHidden(self.rows[key],not visible)
        if visible:self.visible.add(key)
        else:self.visible.discard(key)

    def update_empty(self):
        empty=not self.visible
        self.empty.setVisible(empty)
        self.empty_text.setText('暂无导出任务。' if not self.entries else '没有匹配的任务。')
        self.reset_filter.setVisible(bool(self.entries))
        self.add_tasks.setVisible(not self.entries)
        self.unfinished.setEnabled(any(not self.locked(k[0]) for k in self.visible) and not self.local_busy())
        self.uncheck.setEnabled(any(not self.locked(k[0]) for k in self.selected_keys()) and not self.local_busy())
        deletable = bool(self.selected_keys()) and all(not self.locked(k[0]) for k in self.selected_keys())
        self.delete.setEnabled(deletable);self.delete_action.setEnabled(deletable)
        self.retry.setEnabled(any(not self.locked(k[0]) and self.entries[k]['job'].get('status')=='需处理' for k in self.visible))

    def apply_filter(self,*_):
        self.visible.clear()
        for key in self.rows:self.filter_row(key)
        self.update_empty();self.detail_changed()

    def selected_keys(self):return [key for key,row in self.rows.items() if row < self.table.rowCount() and not self.table.isRowHidden(row) and any(self.table.item(row,c) is not None and self.table.item(row,c).isSelected() for c in range(5))]

    def selection_changed(self):
        if not self._building:self.update_empty()

    def current_key(self):
        item=self.table.item(self.table.currentRow(),0)
        return tuple(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else None

    def changed(self,item):
        if self._building or getattr(self.owner,'_loading',True) or item.column()!=0:return
        key=tuple(item.data(QtCore.Qt.ItemDataRole.UserRole))
        if self.locked(key[0]):self.progress(*key);return
        value=item.checkState()==QtCore.Qt.CheckState.Checked
        if value!=self.is_checked(key):self.commit({key[0]:{key[1]:value}})

    def commit(self,changes):
        owner=self.owner
        if self.local_busy():return False
        try:
            owner.edit_timer.stop()
            if not owner.is_batch_busy() and not owner.persist():return False
            state=owner.store.load()
            expected={p['id']:p.get('revision',0) for p in self.state['profiles'] if p['id'] in changes}
            current=next(p for p in state['profiles'] if p['id']==state['active_profile'])
            if current['id'] in expected:expected[current['id']]=current.get('revision',0)
            owner.state=owner.store.set_render_checks(changes,expected_profile_revisions=expected)
            owner.refresh_tables()
            return True
        except Exception as error:
            owner.status.setText('总列表未更改：'+str(error));self.panel.refresh();return False

    def bulk(self,mode):
        if mode not in {'none','unfinished','completed','visible','clear'}:raise ValueError('批量勾选方式无效。')
        keys=self.selected_keys() if mode=='none' else [k for k in self.rows if not self.table.isRowHidden(self.rows[k])]
        changes={}
        for key in keys:
            if not self.locked(key[0]):
                done=self.entries[key]['job'].get('status')=='已完成'
                value={'none':False,'clear':False,'visible':True,'completed':done,'unfinished':not done}[mode]
                changes.setdefault(key[0],{})[key[1]]=value
        if changes:self.commit(changes)

    def delete_selected(self):
        keys=self.selected_keys()
        if not keys:return
        if any(self.locked(key[0]) for key in keys):
            QtWidgets.QMessageBox.warning(self,'无法删除','所选任务包含正在渲染的项目，请先停止该项目。');return
        answer=QtWidgets.QMessageBox.question(self,'删除任务',
            f'删除选中的 {len(keys)} 条任务记录？\n原素材和已导出视频都会保留。',
            QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer!=QtWidgets.QMessageBox.StandardButton.Yes:return
        owner=self.owner
        try:
            if any(self.locked(key[0]) for key in keys):
                raise ValueError('所选项目已开始渲染，请先停止该项目。')
            owner.edit_timer.stop()
            if not owner.is_batch_busy() and not owner.persist():
                raise ValueError(owner.status.text())
            state=owner.store.load()
            selections={}
            for pid,jid in keys:selections.setdefault(pid,[]).append(jid)
            expected={p['id']:p.get('revision',0) for p in self.state['profiles'] if p['id'] in selections}
            current=next(p for p in state['profiles'] if p['id']==state['active_profile'])
            if current['id'] in expected:expected[current['id']]=current.get('revision',0)
            owner.state=owner.store.delete_render_jobs(selections,expected_profile_revisions=expected)
            if (owner.state['active_profile'],owner.current_id) in keys:
                owner.current_id=''
                if not owner.current_copy_id:owner.clear_editor()
            owner.refresh_tables()
            owner.status.setText(f'已删除 {len(keys)} 条任务记录；原素材和成品保留。')
        except Exception as error:
            owner.status.setText('删除失败：'+str(error));self.panel.refresh()
            QtWidgets.QMessageBox.warning(self,'未删除任务',str(error))

    def start(self):
        # Explicit one-click export includes checked jobs in inactive projects,
        # even if their old per-project pending plan has not been created yet.
        changes={}
        for key in self.entries:
            if not self.locked(key[0]):changes.setdefault(key[0],{})[key[1]]=self.is_checked(key)
        if changes and not self.commit(changes):return
        self.owner.start_staged_plans()

    def retry_failed(self):
        selections={}
        for key in self.rows:
            if key in self.visible and not self.locked(key[0]) and self.entries[key]['job'].get('status')=='需处理':
                selections.setdefault(key[0],[]).append(key[1])
        if not selections:return
        owner=self.owner
        try:
            owner.edit_timer.stop()
            if not owner.save_render_settings():return
            if not owner.is_batch_busy() and not owner.persist():return
            current=owner.store.load()
            expected={p['id']:p.get('revision',0) for p in self.state['profiles'] if p['id'] in selections}
            active=next(p for p in current['profiles'] if p['id']==current['active_profile'])
            if active['id'] in expected:expected[active['id']]=active.get('revision',0)
            owner.state=owner.store.stage_failed_retries(selections,expected_profile_revisions=expected)
            plans=[p for p in owner.state['render_plans'] if p['profile_id'] in selections and p['status']=='staged']
            for plan in plans:owner._launch_batch(plan['ids'],plan=plan)
            self.panel.refresh()
        except Exception as error:
            owner.status.setText('重试未开始：'+str(error))
            QtWidgets.QMessageBox.warning(self,'未开始重试',str(error))

    def stop_all(self):
        for handle in list(self.owner._host_plans.values()):handle.requestInterruption()
        active=any(handle.isRunning() for handle in self.owner._host_plans.values())
        self.owner.status.setText(('正在停止全部导出' if active else '已停止全部导出')+'；已保存的成品保留。')

    def jump_to_active(self):
        for key in self.entries:
            if key[0] in self.owner._host_plans and self.status(key) not in {'已完成','需处理','已取消','排队中'}:
                if self.table.isRowHidden(self.rows[key]):self.filter.setCurrentIndex(0);self.search.clear()
                self.table.setCurrentCell(self.rows[key],0);self.table.scrollToItem(self.table.item(self.rows[key],0),QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter);return
        self.owner.status.setText('当前没有正在处理的任务。')

    def detail_changed(self,*_):
        key=self.current_key();entry=self.entries.get(key)
        self.detail.setText((self.error(key) or entry['job'].get('output') or '') if entry else '')
        self.detail.setVisible(bool(self.detail.text()))

    def preview_current(self,*,rerender=False):
        key=self.current_key()
        if not key:return
        try:
            from .task_preview_ui import open_task
            open_task(self,key,rerender=rerender)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self,'无法预览任务',str(error))

    def open_result(self):
        key=self.current_key();entry=self.entries.get(key)
        if not entry:return
        path=entry['job'].get('output')
        if path and Path(path).is_file():QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        else:self.edit_current()

    def edit_current(self):
        key=self.current_key()
        if not key:return
        if self.locked(key[0]):
            self.owner.status.setText('此项目正在渲染，暂不能编辑；总列表仍可查看进度。');return
        self.owner.switch_profile(key[0])
        for row,job in enumerate(self.owner.state['jobs']):
            if job['id']==key[1]:self.owner.jobs.setCurrentCell(row,0);break
        self.owner.show_design()

    def menu(self,pos):
        item=self.table.itemAt(pos)
        if item and not item.isSelected():self.table.selectRow(item.row())
        menu=QtWidgets.QMenu(self)
        menu.addAction('预览任务配置',self.preview_current)
        rerender=menu.addAction('重新渲染／更换素材…',lambda:self.preview_current(rerender=True))
        key=self.current_key();rerender.setEnabled(key is not None and not self.locked(key[0]))
        menu.addAction('打开成品',self.open_result)
        edit=menu.addAction('编辑此任务',self.edit_current)
        key=self.current_key()
        edit.setEnabled(key is not None and not self.locked(key[0]))
        menu.addAction('取消所选勾选',lambda:self.bulk('none'))
        retry=menu.addAction('重试失败',self.retry_failed);retry.setEnabled(self.retry.isEnabled())
        menu.addSeparator()
        action=menu.addAction('删除所选',self.delete_selected);action.setEnabled(self.delete.isEnabled())
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def batch_clock(self):
        # Hold run objects, not just project IDs: one project may be retried
        # while another is still running. Overlapping durations are never added.
        timing=self.panel.timing
        active=[]
        for profile,run in timing.runs.items():
            if run['ended'] is None:
                timing._activate(profile,run)
                active.append(run)
        if active and not any(run['ended'] is None for run in self.wave_runs):self.wave_runs=[]
        for run in active:
            if not any(run is previous for previous in self.wave_runs):self.wave_runs.append(run)
        if not self.wave_runs:return ''
        starts=[run['started'] for run in self.wave_runs if run['started'] is not None]
        if not starts:
            endpoint=time.monotonic() if active else max(run['ended'] or run['submitted'] for run in self.wave_runs)
            elapsed=endpoint-min(run['submitted'] for run in self.wave_runs)
            return ' · 后台排队 '+clock_text(elapsed)
        endpoint=time.monotonic() if active else max(run['ended'] or run['submitted'] for run in self.wave_runs)
        elapsed=max(0.,endpoint-min(starts))
        saved=sum((run.get('result') or {}).get('completed',sum(item.get('status')=='已完成' for item in run['items'].values())) for run in self.wave_runs)
        text=' · 本批用时 '+clock_text(elapsed)+' · 已保存 '+str(saved)
        if saved:text+=f' · 平均{elapsed/saved:.2f}秒/条'
        return text

    def tick(self):
        if not self.isVisible():return
        statuses=[self.status(k) for k in self.entries]
        done=sum(s=='已完成' for s in statuses);failed=sum(s.startswith('需') or s=='匹配失败' for s in statuses)
        count=sum(self.is_checked(k) for k in self.entries)
        self.summary.setText(f'共 {len(self.entries)} 条 · 已勾选 {count} · 已完成 {done}'+(f' · 需处理 {failed}' if failed else '')+self.batch_clock())
        self.stop.setEnabled(bool(self.owner._host_plans))
        self.update_empty()
        first=self.table.rowAt(0);last=self.table.rowAt(max(0,self.table.viewport().height()-1))
        if first>=0:
            self._building=True
            try:
                for row in range(first,(last if last>=0 else min(self.table.rowCount()-1,first+40))+1):
                    item=self.table.item(row,0)
                    if item:self.update_time(tuple(item.data(QtCore.Qt.ItemDataRole.UserRole)))
            finally:self._building=False
