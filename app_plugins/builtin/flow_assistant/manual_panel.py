"""Per-image drafts and acknowledgement-driven manual submission workflow."""
import copy
from pathlib import Path
import time
import uuid

from qt_compat import QtCore, QtGui, QtWidgets
from .suspicious_download import output_path
from .policy import draft_key, cost_key, estimated_cost
from .pricing import fixed_cost
from .routing import dispatch_rank
from .timing import value as timing_value
from .connection_status import configured_profiles, browser_status
from .status_ui import status_area


def estimate_text(items, state):
    costs = {tuple(item['key']): item['cost'] for item in state.get('preferences', {}).get('observed_costs', [])}
    total, unknown, count, fixed_count = 0, 0, 0, 0
    for item in items:
        copies = len(item.get('images', [])) * int(item.get('count', 1))
        count += copies
        job = {'model': 'omni_1_1_flash', **item}
        cost = estimated_cost(job, costs)
        if fixed_cost(job) is not None: fixed_count += copies
        if cost is None:
            unknown += copies
        else:
            total += cost * copies
    return f'展开后 {count} 条视频 · 积分估算 {total:g}' + (f'（{fixed_count} 条按固定时长规则）' if fixed_count else '') + (f' · {unknown} 条费用待页面确认' if unknown else '')


class RecentSubmissions(QtWidgets.QWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel, self.rows, self.icons = panel, [], {}
        layout = QtWidgets.QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QtWidgets.QLabel('最近提交／成品回看'))
        self.list = QtWidgets.QListWidget(self); self.list.setIconSize(QtCore.QSize(60, 60))
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.ActionsContextMenu)
        remove = QtGui.QAction('删除这组任务记录…', self.list)
        remove.triggered.connect(lambda: self.panel.dialog.delete_records([row['id'] for row in self.selected()]))
        self.list.addAction(remove)
        layout.addWidget(self.list, 1)
        self.outputs = QtWidgets.QComboBox(self); layout.addWidget(self.outputs)
        self.list.currentItemChanged.connect(self.update_outputs)
        for label, callback in [('预览成品', self.preview), ('再做一版（恢复参数）', self.variant),
                                ('重试下载', lambda: self.panel.dialog.send('retry_download_selected', ids=[row['id'] for row in self.selected()])),
                                ('采用', lambda: self.review(True)), ('待调整', lambda: self.review(False))]:
            button = QtWidgets.QPushButton(label, self); button.clicked.connect(callback); layout.addWidget(button)

    def selected(self):
        item = self.list.currentItem()
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item else []

    def update_rows(self, rows):
        from .ui import STATUS
        stamp = tuple((row['id'], row['status'], row['error'], *(row['payload'].get(key) for key in
            ('batch_id', 'image', 'prompt', 'duration', 'aspect', 'resolution', 'budget'))) for row in rows)
        if getattr(self, '_stamp', None) == stamp: return
        self._stamp = stamp
        retained_images = {row['payload']['image'] for row in rows}
        self.icons = {key: icon for key, icon in self.icons.items() if key in retained_images}
        groups = {}
        for row in rows:
            job = row['payload']
            groups.setdefault((job.get('batch_id') or row['id'], job['image'], job['prompt'],
                job.get('duration'), job.get('aspect'), job.get('resolution'), job.get('budget')), []).append(row)
        selected = self.selected()
        selected_id = selected[0]['id'] if selected else ''
        self.list.clear()
        for group in list(groups.values())[-8:][::-1]:
            row, job = group[0], group[0]['payload']
            path = Path(job['image']); key = str(path)
            if key not in self.icons:
                reader = QtGui.QImageReader(str(path)); reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid(): reader.setScaledSize(size.scaled(60, 60, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
                self.icons[key] = QtGui.QIcon(QtGui.QPixmap.fromImage(reader.read()))
                if len(self.icons) > 24: self.icons.pop(next(iter(self.icons)))
            statuses = ' / '.join(dict.fromkeys(STATUS.get(r['status'], r['status']) for r in group))
            text = f'{job["task_id"]} · {path.name}\n{job.get("duration") or "默认"}秒 × {len(group)} · {statuses}\n{job["prompt"][:65]}'
            item = QtWidgets.QListWidgetItem(self.icons.get(key, QtGui.QIcon()), text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, group); item.setToolTip(job['prompt'])
            self.list.addItem(item)
            if any(r['id'] == selected_id for r in group): self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count(): self.list.setCurrentRow(0)

    def update_outputs(self, *_):
        self.outputs.clear()
        for row in self.selected():
            if row['status'] in {'completed', 'needs_review'} and Path(output_path(row)).is_file():
                self.outputs.addItem(Path(output_path(row)).name, row)
        if not self.outputs.count(): self.outputs.addItem('尚无已归档成品', None)

    def preview(self):
        row = self.outputs.currentData()
        if row: self.open_row(row)

    def open_row(self, row):
        if not row['remote'].get('media', {}).get('duration'):
            from .ui import open_path
            open_path(output_path(row))
            return
        from .result_preview import ResultPreview
        if hasattr(self, 'preview_dialog'): self.preview_dialog.close()
        self.preview_dialog = ResultPreview(row, self.panel.dialog)
        self.preview_dialog.show(); self.preview_dialog.raise_()

    def variant(self):
        if self.selected():
            self.panel.restore_job(self.outputs.currentData() or self.selected()[0], count=len(self.selected()))

    def review(self, accepted):
        row = self.outputs.currentData()
        if row: self.panel.dialog.send('review', ids=[row['id']], accepted=accepted)


class ManualWorkflowMixin:
    def __init__(self, dialog):
        super().__init__(dialog)
        from .submission_ui import TaskImagePicker, GenerationOptions
        self.dialog, self.drafts, self.current_key = dialog, {}, ''
        self.request_id, self.pending_command = None, None
        self.pending_editor = None
        self.current_target, self.current_image = {}, ''
        self.loading, self.dirty_keys, self.previous_prompt = False, set(), ''
        self.next_after_ack = False
        layout = QtWidgets.QVBoxLayout(self)
        self.picker = TaskImagePicker(self); layout.addWidget(self.picker, 3)
        if getattr(dialog,'unified_submission',False):
            self.picker.preview.setMinimumHeight(100)
            self.picker.setMaximumHeight(260)
            self.picker.images.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            self.picker.images.setToolTip('点击预览当前图片；Ctrl／Shift可多选，再用清单的“更多 → 所选图片加入清单”。多段识别始终使用当前预览图片。')
        lower = QtWidgets.QHBoxLayout()
        text = QtWidgets.QVBoxLayout()
        editor_header = QtWidgets.QHBoxLayout()
        title=QtWidgets.QLabel('提示词',self);title.setToolTip('按任务和图片保存草稿；清单中的每一条也单独保存。')
        self.editor_title=title
        editor_header.addWidget(title, 1)
        text.addLayout(editor_header)
        self.prompt = QtWidgets.QPlainTextEdit(self); text.addWidget(self.prompt, 1)
        from .prompt_review import SpeechReview
        self.speech_review=SpeechReview(self);text.addWidget(self.speech_review)
        if getattr(dialog,'unified_submission',False):
            text.removeWidget(self.prompt);text.removeWidget(self.speech_review)
            self.editor_tabs=QtWidgets.QTabWidget(self);self.editor_tabs.setDocumentMode(True)
            self.editor_tabs.addTab(self.prompt,'提示词');self.editor_tabs.addTab(self.speech_review,'台词校对')
            self.speech_review.label.hide();self.speech_review.text.setMaximumHeight(16777215)
            text.addWidget(self.editor_tabs,1)
        from .editor_width import EditorWidthLock
        self.editor_width_lock = EditorWidthLock(self, editor_header,companions=(self.speech_review,))
        copy_button = QtWidgets.QPushButton('复制上一张提示词', self)
        self.copy_button=copy_button
        copy_button.clicked.connect(lambda: self.prompt.setPlainText(self.previous_prompt) if self.previous_prompt else None)
        if getattr(dialog,'unified_submission',False):
            copy_button.setText('复制上一条');copy_button.hide()
        else:text.addWidget(copy_button)
        lower.addLayout(text, 2)
        self.options = GenerationOptions(self, manual=True)
        self.batch_limit = QtWidgets.QSpinBox(self); self.batch_limit.setRange(0, 10**8); self.batch_limit.setSpecialValueText('不限整批')
        self.options.layout().addRow('本次展开的整批积分上限', self.batch_limit)
        if getattr(dialog,'unified_submission',False):
            self.parameter_popup=QtWidgets.QFrame(self,QtCore.Qt.WindowType.Popup)
            popup_layout=QtWidgets.QVBoxLayout(self.parameter_popup);popup_layout.addWidget(self.options)
            quick=QtWidgets.QHBoxLayout()
            for label,widget in [('时长',self.options.duration),('数量',self.options.count)]:
                taken=self.options.layout().takeRow(widget)
                if taken.labelItem and taken.labelItem.widget():taken.labelItem.widget().deleteLater()
                quick.addWidget(QtWidgets.QLabel(label,self));quick.addWidget(widget)
            self.parameters_button=QtWidgets.QPushButton('参数…',self)
            self.parameters_button.setToolTip('画幅、分辨率、单条积分预算和整批积分上限。')
            def open_parameters():
                self.parameter_popup.adjustSize()
                self.parameter_popup.move(self.parameters_button.mapToGlobal(QtCore.QPoint(0,self.parameters_button.height())))
                self.parameter_popup.show()
            self.parameters_button.clicked.connect(open_parameters);quick.addWidget(self.parameters_button);quick.addStretch()
            text.addLayout(quick)
        else:lower.addWidget(self.options)
        self.recent = RecentSubmissions(self); self.recent.setMinimumWidth(260)
        if not getattr(dialog,'unified_submission',False):lower.addWidget(self.recent, 1)
        layout.addLayout(lower, 2)
        self.route_note = QtWidgets.QLabel(self); layout.addWidget(status_area(self.route_note, 2))
        self.estimate = QtWidgets.QLabel(self); layout.addWidget(status_area(self.estimate, 1))
        from .collection_controls import CollectionControls
        self.collection=CollectionControls(dialog,self)
        self.collect_only,self.collect_submit,self.collect_discard=self.collection.collect,self.collection.submit,self.collection.discard
        self.collect_only.setObjectName('flow_collect_before_submit')
        self.collect_submit.setObjectName('flow_submit_collected');self.collect_discard.setObjectName('flow_discard_collected')
        if not getattr(dialog,'unified_submission',False):layout.addWidget(self.collection)
        row = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel('草稿按任务＋图片分别保存；普通回车换行，Ctrl+Enter 提交并切换下一张。', self)
        self.status.setToolTip('草稿自动保存；普通回车换行，Ctrl+Enter 加入清单并切换下一张。')
        if getattr(dialog,'unified_submission',False):self.status.setText('录入新条目')
        row.addWidget(status_area(self.status, 1), 1)
        self.submit = QtWidgets.QPushButton('提交这一条', self); self.submit.clicked.connect(lambda: self.enqueue())
        self.submit_next = QtWidgets.QPushButton('提交并切换下一张', self); self.submit_next.clicked.connect(lambda: self.enqueue(advance=True))
        row.addWidget(self.submit); row.addWidget(self.submit_next); layout.addLayout(row)
        if getattr(dialog,'unified_submission',False):self.submit_next.hide()
        self.shortcut = QtGui.QShortcut(QtGui.QKeySequence('Ctrl+Return'), self)
        self.shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.shortcut.activated.connect(lambda: self.enqueue(advance=True) if self.isEnabled() else None)
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True)
        self.timer.setInterval(round(timing_value(self.dialog.state.get('preferences', {}), 'draft_save_seconds') * 1000))
        self.timer.timeout.connect(self.flush_draft)
        self.picker.image_changed.connect(self.image_changed)
        self.prompt.textChanged.connect(self.prompt_edited)
        for widget in (self.options.duration, self.options.aspect, self.options.resolution):
            widget.currentIndexChanged.connect(self.edited)
        for widget in (self.options.count, self.options.budget, self.batch_limit): widget.valueChanged.connect(self.edited)
        self.update_collect_controls()
        self.update_route()
        if getattr(dialog,'unified_submission',False):self.route_note.hide();self.estimate.hide()

    def collect_mode_changed(self, checked):
        self.dialog.send('preferences', value={'collect_before_submit':checked})
        self.update_collect_controls()

    def update_collect_controls(self):
        if getattr(self.dialog,'batch',None):
            self.dialog.batch.update_controls();return
        collecting = self.collect_only.isChecked()
        self.submit.setText('添加任务记录' if collecting else '提交这一条')
        self.submit_next.setText('添加并切换下一张' if collecting else '提交并切换下一张')
        count = sum(r['status']=='collected' for r in self.dialog.state.get('jobs', []))
        self.collect_submit.setText(f'统一提交已添加任务（{count}）')
        self.collect_submit.setEnabled(count > 0)
        self.collect_discard.setText(f'放弃待提交任务（{count}）')
        self.collect_discard.setEnabled(count > 0)

    def discard_collected(self):
        if self.request_id: return
        ids = [row['id'] for row in self.dialog.state.get('jobs', []) if row['status']=='collected']
        if not ids: return
        self.dialog.send('discard_collected', ids=ids)
        self.status.setText(f'正在放弃 {len(ids)} 条待统一提交任务；任务记录和草稿会保留。')

    def update_prompt_submission(self):
        draft = self.drafts.get(self.current_key, {})
        submitted = (draft.get('submitted') is True and draft.get('prompt') == self.prompt.toPlainText()
                     and draft.get('submitted_options',self.options.values()) == self.options.values())
        stage = draft.get('submission_stage','queue') if submitted else ''
        batch = getattr(self.dialog,'batch',None)
        if batch:
            row = batch.entry()
            matches = lambda value: (value['image']==self.current_image and value['prompt']==self.prompt.toPlainText()
                                     and value['options']==self.options.values())
            if row:
                from .batch_workspace import signature
                stage = 'table' if matches(row) and row.get('staged_signature',row.get('saved_signature'))==signature(row) else ''
            elif stage=='table' and not any(matches(row) for row in batch.rows):stage=''
        submitted = bool(stage)
        if self.prompt.property('flowSubmitted') != submitted:
            self.prompt.setProperty('flowSubmitted', submitted)
            self.prompt.setStyleSheet('QPlainTextEdit { background-color: #ffe3e3; color: #7f1d1d; '
                                     'border: 2px solid #dc2626; }' if submitted else '')
        self.prompt.setProperty('flowSubmissionStage',stage)
        self.prompt.setToolTip(('当前内容已加入表格，等待“提交表格任务”。' if stage=='table' else
            '当前内容已提交到生成队列。')+'修改文字或生成参数后清除红色标记。' if submitted else '')

    def mark_submission(self,snapshots,stage):
        """Mark only exact saved content; never overwrite a newer per-image draft."""
        for row in snapshots:
            key = draft_key(row['target']['target_dir'],row['image'])
            draft = self.drafts.get(key,{})
            if draft.get('prompt')!=row['prompt'] or draft.get('options')!=row['options']:continue
            value = {**draft,'submitted':True,'submission_stage':stage,
                     'submitted_options':copy.deepcopy(row['options']),'updated':time.time()}
            self.drafts[key]=value;self.dirty_keys.add(key)
            self.dialog.send('draft_save',key=key,value=value)
        self.update_prompt_submission()

    def prompt_edited(self):
        draft = self.drafts.get(self.current_key, {})
        if not self.loading and draft.get('submitted') and draft.get('prompt') != self.prompt.toPlainText():
            self.drafts[self.current_key] = {key: value for key, value in draft.items() if key != 'submitted'}
        self.update_prompt_submission()
        self.edited()

    def edited(self, *_):
        self.update_route()
        if not self.loading and self.current_key:
            self.dirty_keys.add(self.current_key)
            self.timer.start(round(timing_value(self.dialog.state.get('preferences', {}), 'draft_save_seconds') * 1000))

    def flush_draft(self):
        self.timer.stop()
        if not self.current_key: return
        value = {'target': copy.deepcopy(self.current_target), 'image': self.current_image,
                 'prompt': self.prompt.toPlainText(), 'options': self.options.values(),
                 'batch_limit': self.batch_limit.value(), 'updated': time.time()}
        previous = self.drafts.get(self.current_key, {})
        if previous.get('submitted') is True and previous.get('prompt') == value['prompt']:
            value['submitted'] = True
            for key in ('submission_stage','submitted_options'):
                if key in previous:value[key]=copy.deepcopy(previous[key])
        self.drafts[self.current_key] = value
        self.dialog.send('draft_save', key=self.current_key, value=value)

    def image_changed(self, image):
        if self.current_key:
            self.previous_prompt = self.prompt.toPlainText(); self.flush_draft()
        self.current_target = copy.deepcopy(self.picker.target.currentData() or {})
        self.current_image = image
        self.current_key = draft_key(self.current_target['target_dir'], image) if image and self.current_target else ''
        self.restore_current()

    def restore_current(self):
        value = self.drafts.get(self.current_key, {})
        self.loading = True
        try:
            self.prompt.setPlainText(value.get('prompt', self.current_target.get('task', {}).get('task_video_prompt', '')))
            self.options.set_values(value.get('options', self.dialog.state.get('preferences', {}).get('manual_defaults', {})))
            self.batch_limit.setValue(int(value.get('batch_limit', 0)))
        finally:
            self.loading = False
        self.update_prompt_submission()
        self.update_route()

    def apply_state(self, state):
        self.editor_width_lock.apply(state.get('preferences', {}))
        with QtCore.QSignalBlocker(self.collect_only):
            self.collect_only.setChecked(state.get('preferences', {}).get('collect_before_submit', True))
        self.update_collect_controls()
        changed = False
        for key, value in state.get('drafts', {}).items():
            if key not in self.dirty_keys and self.drafts.get(key) != value:
                self.drafts[key] = value
                changed |= key == self.current_key
        if changed: self.restore_current()
        self.recent.update_rows(state.get('jobs', []))
        if not self.request_id and getattr(self, 'last_submitted_ids', None):
            rows = [row for row in state.get('jobs', []) if row['id'] in self.last_submitted_ids]
            waiting = next((row.get('wait_reason') for row in rows if row['status'] == 'queued' and row.get('wait_reason')), '')
            if rows:
                from .ui import STATUS
                self.status.setText('最近提交：' + (waiting or '、'.join(dict.fromkeys(STATUS.get(row['status'], row['status']) for row in rows))))
        self.update_route()

    def update_route(self):
        if not hasattr(self, 'route_note'): return
        duration = self.options.duration.currentData()
        self.speech_review.update_prompt(self.prompt.toPlainText(),duration)
        if hasattr(self,'parameters_button'):
            self.parameters_button.setText(f'{self.options.aspect.currentText()} · {self.options.resolution.currentText()}…')
        state = self.dialog.state; preferences = state.get('preferences', {})
        job = {'duration': duration}
        clients = [client for client in state.get('clients', []) if dispatch_rank(client, job, preferences) is not None]
        ready = sum(browser_status(client, preferences, state.get('jobs', []))['state'] == '可接单' for client in clients)
        backup = sum(dispatch_rank({'profile': profile}, job, preferences) is not None for profile in configured_profiles(preferences))
        label = f'{duration} 秒任务（自动安排空闲浏览器）' if duration else '模型默认时长'
        self.route_note.setText(f'{label}：已连接 {len(clients)}，可接单 {ready}，可按需启动的配置 {backup}。' +
            ('添加仅保存任务记录，点击“统一提交已添加任务”后分包执行。' if self.collect_only.isChecked() else
             '请先在“浏览器状态”连接账号或设置备用配置。' if not clients and not backup else '提交后自动调度，等待原因会实时显示在下方。'))
        self.estimate.setText(estimate_text([{'images': [self.current_image], **self.options.values()}], self.dialog.state))

    def enqueue(self, *, advance=False):
        try:
            if self.request_id: return
            if getattr(self.dialog,'batch',None):
                self.dialog.batch.add_manual(advance=advance);return
            target, image = self.picker.selection()
            prompt = self.prompt.toPlainText().strip()
            if not prompt: raise ValueError('请填写本条提示词。')
            self.flush_draft(); self.dialog.save_preferences()
            self.pending_editor = self.current_key, self.prompt.toPlainText()
            self.request_id = uuid.uuid4().hex; self.next_after_ack = advance
            self.pending_command = {'source': 'manual', 'request_id': self.request_id,
                'batch_limit': self.batch_limit.value(),
                'items': [{'target': target, 'images': [image], 'prompt': prompt, **self.options.values()}]}
            self.status.setText('正在可靠保存提交内容；成功回执后才会切换图片。')
            self.setEnabled(False); self.dialog.send('enqueue', **self.pending_command)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def acknowledged(self, result):
        if not self.request_id or result.get('request_id') != self.request_id: return
        key, text = self.pending_editor or ('', '')
        draft = self.drafts.get(key, {})
        if key and draft.get('prompt') == text and (key != self.current_key or self.prompt.toPlainText() == text):
            value = {**draft, 'submitted': True, 'updated': time.time()}
            self.drafts[key] = value; self.dirty_keys.add(key)
            self.dialog.send('draft_save', key=key, value=value)
        self.pending_editor = None
        self.update_prompt_submission()
        self.setEnabled(True); self.request_id = None
        self.last_submitted_ids = set(result.get('ids', ()))
        self.status.setText(f'已可靠保存 {result["count"]} 条；' + ('等待点击“统一提交已添加任务”。' if result.get('collected') else
            '等待确认继续。' if result.get('held') else '后台负责生成和归档。'))
        if self.next_after_ack:
            index = self.picker.images.currentRow() + 1
            if index < self.picker.images.count(): self.picker.images.setCurrentRow(index)
            elif self.picker.target.currentIndex() + 1 < self.picker.target.count():
                self.picker.target.setCurrentIndex(self.picker.target.currentIndex() + 1)
            else: self.status.setText('已提交，当前所选任务的图片已到末尾。')
        self.next_after_ack = False

    def failed(self, result):
        if not self.request_id or result.get('request_id') != self.request_id: return
        self.setEnabled(True); self.request_id = None
        self.status.setText(result['error'])
        if result.get('duplicate_ids') and self.dialog.confirm_duplicate(result['duplicate_ids']):
            self.request_id = self.pending_command['request_id']; self.setEnabled(False)
            self.dialog.send('enqueue', **self.pending_command, allow_duplicates=True)
        else:
            self.pending_editor = None

    def restore_job(self, row, *, count=None, for_edit=False):
        if not for_edit and getattr(self.dialog,'batch',None):self.dialog.batch.new_item()
        job = row['payload']; target = {'target_dir': job['directory'],
            'label': f'{job["task_id"]} {job["task_name"]}', 'task': {key: job.get(key, '') for key in ('task_id', 'task_name', 'task_date', 'task_type')}}
        targets = list(self.dialog.targets)
        if not any(item['target_dir'] == target['target_dir'] for item in targets): targets.append(target)
        self.dialog.set_targets(targets)
        index = next(i for i in range(self.picker.target.count()) if self.picker.target.itemData(i)['target_dir'] == target['target_dir'])
        self.picker.target.setCurrentIndex(index)
        item = next((self.picker.images.item(i) for i in range(self.picker.images.count())
                     if self.picker.images.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == job['image']), None)
        if item: self.picker.images.setCurrentItem(item)
        copies = count if count is not None else job.get('copies_per_image', 1)
        self.prompt.setPlainText(job['prompt']); self.options.set_values({**job, 'count': copies})
        if not for_edit:self.batch_limit.setValue(int(self.dialog.state.get('batches', {}).get(job.get('batch_id'), {}).get('limit_total', 0)))
        self.dialog.show_flow_page(self)
