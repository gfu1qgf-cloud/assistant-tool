"""Per-image drafts and acknowledgement-driven manual submission workflow."""
import copy
from pathlib import Path
import time
import uuid

from qt_compat import QtCore, QtGui, QtWidgets
from .policy import draft_key, cost_key, estimated_cost
from .pricing import fixed_cost
from .routing import dispatch_rank
from .timing import value as timing_value
from .connection_status import configured_profiles, browser_status


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
            if row['status'] in {'completed', 'needs_review'} and Path(row['payload']['output']).is_file():
                self.outputs.addItem(Path(row['payload']['output']).name, row)
        if not self.outputs.count(): self.outputs.addItem('尚无已归档成品', None)

    def preview(self):
        row = self.outputs.currentData()
        if row: self.open_row(row)

    def open_row(self, row):
        if not row['remote'].get('media', {}).get('duration'):
            from .ui import open_path
            open_path(row['payload']['output'])
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
        lower = QtWidgets.QHBoxLayout()
        text = QtWidgets.QVBoxLayout(); text.addWidget(QtWidgets.QLabel('当前图片的提示词（自动保存草稿）'))
        self.prompt = QtWidgets.QPlainTextEdit(self); text.addWidget(self.prompt, 1)
        copy_button = QtWidgets.QPushButton('复制上一张提示词', self)
        copy_button.clicked.connect(lambda: self.prompt.setPlainText(self.previous_prompt) if self.previous_prompt else None)
        text.addWidget(copy_button); lower.addLayout(text, 2)
        self.options = GenerationOptions(self, manual=True)
        self.batch_limit = QtWidgets.QSpinBox(self); self.batch_limit.setRange(0, 10**8); self.batch_limit.setSpecialValueText('不限整批')
        self.options.layout().addRow('本次展开的整批积分上限', self.batch_limit)
        lower.addWidget(self.options)
        self.recent = RecentSubmissions(self); self.recent.setMinimumWidth(260); lower.addWidget(self.recent, 1)
        layout.addLayout(lower, 2)
        self.route_note = QtWidgets.QLabel(self); self.route_note.setWordWrap(True); layout.addWidget(self.route_note)
        self.estimate = QtWidgets.QLabel(self); self.estimate.setWordWrap(True); layout.addWidget(self.estimate)
        row = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel('草稿按任务＋图片分别保存；普通回车换行，Ctrl+Enter 提交并切换下一张。', self)
        self.status.setWordWrap(True); row.addWidget(self.status, 1)
        self.submit = QtWidgets.QPushButton('提交这一条', self); self.submit.clicked.connect(lambda: self.enqueue())
        self.submit_next = QtWidgets.QPushButton('提交并切换下一张', self); self.submit_next.clicked.connect(lambda: self.enqueue(advance=True))
        row.addWidget(self.submit); row.addWidget(self.submit_next); layout.addLayout(row)
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
        self.update_route()

    def update_prompt_submission(self):
        draft = self.drafts.get(self.current_key, {})
        submitted = draft.get('submitted') is True and draft.get('prompt') == self.prompt.toPlainText()
        if self.prompt.property('flowSubmitted') == submitted: return
        self.prompt.setProperty('flowSubmitted', submitted)
        self.prompt.setStyleSheet('QPlainTextEdit { background-color: #ffe3e3; color: #7f1d1d; '
                                 'border: 2px solid #dc2626; }' if submitted else '')
        self.prompt.setToolTip('当前提示词已提交到队列；修改文字后清除红色标记。' if submitted else '')

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
        state = self.dialog.state; preferences = state.get('preferences', {})
        job = {'duration': duration}
        clients = [client for client in state.get('clients', []) if dispatch_rank(client, job, preferences) is not None]
        ready = sum(browser_status(client, preferences, state.get('jobs', []))['state'] == '可接单' for client in clients)
        backup = sum(dispatch_rank({'profile': profile}, job, preferences) is not None for profile in configured_profiles(preferences))
        label = f'{duration} 秒任务（自动安排空闲浏览器）' if duration else '模型默认时长'
        self.route_note.setText(f'{label}：已连接 {len(clients)}，可接单 {ready}，可按需启动的配置 {backup}。' +
            ('请先在“浏览器状态”连接账号或设置备用配置。' if not clients and not backup else '提交后自动调度，等待原因会实时显示在下方。'))
        self.estimate.setText(estimate_text([{'images': [self.current_image], **self.options.values()}], self.dialog.state))

    def enqueue(self, *, advance=False):
        try:
            if self.request_id: return
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
        self.status.setText(f'已可靠保存 {result["count"]} 条；' + ('等待确认继续。' if result.get('held') else '后台负责生成和归档。'))
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

    def restore_job(self, row, *, count=None):
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
        self.batch_limit.setValue(int(self.dialog.state.get('batches', {}).get(job.get('batch_id'), {}).get('limit_total', 0)))
        self.dialog.tabs.setCurrentWidget(self)
