"""Queue search/grouping and explicit non-generating recovery actions."""
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from .submission_ui import GenerationOptions


class QueueTools:
    def __init__(self, dialog, page, layout):
        self.dialog = dialog
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit(page); self.search.setPlaceholderText('搜索任务、图片、提示词、账号或等待原因')
        self.status = QtWidgets.QComboBox(page)
        for label, value in [('所有状态', []), ('等待／生成中', ['queued','preparing','uploading','submitting','generating','downloading']),
                              ('失败／需要处理', ['failed','uncertain','download_failed','archive_failed']),
                              ('下载暂存', ['download_parked']), ('待复核／待调整', ['needs_review']), ('已完成', ['completed'])]:
            self.status.addItem(label, value)
        self.group = QtWidgets.QComboBox(page)
        for label, value in [('按提交批次分组', 'batch_id'), ('按所属任务分组', 'directory'), ('不分组', '')]: self.group.addItem(label, value)
        row.addWidget(self.search, 1); row.addWidget(self.status); row.addWidget(self.group); layout.addLayout(row)
        actions = dialog.queue_menu_button.menu()
        actions.addSeparator()
        for label, callback in [('重试全部失败项', lambda: dialog.send('retry', ids=[], only_failed=True)),
                                ('修改未提交项', self.edit),
                                ('暂存下载并释放账号', self.release), ('打开对应浏览器', self.open_browser),
                                ('调整本批积分上限', self.batch_limit)]:
            actions.addAction(label, callback)
        actions.addSeparator()
        actions.addAction('清理已完成／已取消记录…', lambda: dialog.delete_records([
            row['id'] for row in dialog.state.get('jobs', []) if row['status'] in {'completed', 'cancelled'}]))
        self.summary = QtWidgets.QLabel(page)
        self.search.textChanged.connect(self.refresh); self.status.currentIndexChanged.connect(self.refresh)
        self.group.currentIndexChanged.connect(self.refresh)

    def refresh(self, *_):
        if 'jobs' in self.dialog.state: self.dialog.update_state(self.dialog.state)

    def render(self, rows, labels):
        table = self.dialog.queue; chosen = set(self.dialog.selected_ids())
        spaces = self.dialog.state.get('spaces', [])
        self.summary.setText('磁盘可用：' + '  |  '.join(f'{item["path"]}：' +
            (f'{item["free"]/1024**3:.1f} GB' if item['free'] is not None else '不可用') for item in spaces))
        stamp = (self.search.text(), str(self.status.currentData()), self.group.currentData(),
                 str(self.dialog.state.get('batches', {})),
                 tuple((row['id'], row['status'], row['error'], row.get('wait_reason'), str(row['payload']), row['remote'].get('account')) for row in rows))
        if getattr(self, '_stamp', None) == stamp: return
        self._stamp = stamp
        needle, states = self.search.text().casefold().strip(), set(self.status.currentData() or [])
        visible = [row for row in rows if (not states or row['status'] in states) and
                   (not needle or needle in str((row['payload'], row['error'], row.get('wait_reason'), row['remote'].get('account'))).casefold())]
        key = self.group.currentData()
        if key:
            order = {value: index for index, value in enumerate(dict.fromkeys(row['payload'].get(key, '历史未分批') for row in visible))}
            visible.sort(key=lambda row: order[row['payload'].get(key, '历史未分批')])
        table.clearSpans(); table.setRowCount(0)
        group = object()
        batches = self.dialog.state.get('batches', {})
        for row in visible:
            job = row['payload']; value = job.get(key, '历史未分批') if key else None
            if key and value != group:
                group = value; index = table.rowCount(); table.insertRow(index)
                batch = batches.get(value, {})
                text = (job.get('batch_label', '历史未分批') + f' · 预留 {batch.get("reserved", 0):g} / ' +
                        (f'{batch["limit_total"]:g} 积分' if batch.get('limit_total') else '不限积分')) if key == 'batch_id' else f'{job["task_id"]} {job["task_name"]} · {value}'
                if key == 'batch_id' and not batch: text = '历史未分批 · 旧记录没有本批积分账目'
                if key == 'batch_id' and batch.get('estimated'): text += f' · {batch["estimated"]} 条按单条预算估算'
                if key == 'batch_id' and batch.get('fixed'): text += f' · {batch["fixed"]} 条按固定时长计费'
                item = QtWidgets.QTableWidgetItem(text); item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                item.setBackground(QtGui.QColor('#dce8f5')); table.setItem(index, 0, item); table.setSpan(index, 0, 1, 6)
            index = table.rowCount(); table.insertRow(index)
            duration = f'{job["duration"]}秒' if job.get('duration') else '默认时长'
            values = [f'{job["task_id"]} {job["task_name"]}', f'{Path(job["image"]).name} · {duration}',
                      labels.get(row['status'], row['status']),
                      row['remote'].get('account', ''),
                      ('已保存到任务目录：' if row['status'] in {'completed','needs_review'} else '目标：') + Path(job['output']).name,
                      ('已保存到：' + job['directory'] + '；' if row['status'] in {'completed','needs_review'} else '') + (row.get('wait_reason') or row['error'])]
            for column, text in enumerate(values):
                item = QtWidgets.QTableWidgetItem(text); item.setData(QtCore.Qt.ItemDataRole.UserRole, row['id'])
                item.setToolTip(job['output'] if column == 4 else text)
                item.setBackground(QtGui.QColor('#e0f4e8' if row['status'] == 'completed' else '#fff0e8' if row['status'] in {'needs_review','failed','uncertain','download_failed','archive_failed'} else '#ffffff'))
                table.setItem(index, column, item)
                if row['id'] in chosen: item.setSelected(True)
        table.resizeRowsToContents()

    def edit(self):
        row = self.dialog.selected_job()
        if not row: return
        ids = self.dialog.selected_ids()
        selected = [r for r in self.dialog.state.get('jobs', []) if r['id'] in ids]
        if any(r['status'] != 'queued' or r['attempt'] for r in selected):
            self.dialog.note.setText('只能修改尚未提交的排队任务；已生成的请“再做一版”。'); return
        editor = QtWidgets.QDialog(self.dialog); editor.setWindowTitle(f'修改 {len(ids)} 条尚未提交的任务')
        layout = QtWidgets.QVBoxLayout(editor)
        replace_prompt = QtWidgets.QCheckBox('统一替换所选条目的提示词（不勾选则各自保留）', editor)
        replace_prompt.setChecked(len(ids) == 1); layout.addWidget(replace_prompt)
        prompt = QtWidgets.QPlainTextEdit(row['payload']['prompt'], editor); layout.addWidget(prompt)
        options = GenerationOptions(editor); options.set_values(row['payload']); options.count.setEnabled(False)
        options.count.setToolTip('队列中的每项是一条展开后的视频；增加数量请另行提交变体。'); layout.addWidget(options)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(editor.accept); buttons.rejected.connect(editor.reject); layout.addWidget(buttons)
        if editor.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.dialog.send('edit', ids=ids, value={'prompt': prompt.toPlainText() if replace_prompt.isChecked() else None, **options.values()})

    def priority(self):
        value, ok = QtWidgets.QInputDialog.getInt(self.dialog, '排队优先级', '数值越大越先执行（只影响未提交项）：', 0, -100, 100)
        if ok: self.dialog.send('priority', ids=self.dialog.selected_ids(), value=value)

    def batch_limit(self):
        row = self.dialog.selected_job()
        if not row or not row['payload'].get('batch_id'): return
        batch_id = row['payload']['batch_id']; batch = self.dialog.state.get('batches', {}).get(batch_id, {})
        value, ok = QtWidgets.QInputDialog.getInt(self.dialog, '整批积分上限', f'已预留 {batch.get("reserved",0):g}；0 表示不限。不会退回已提交积分：', int(batch.get('limit_total', 0)), 0, 10**8)
        if ok: self.dialog.send('batch_limit', batch_id=batch_id, limit=value)

    def release(self):
        row = self.dialog.selected_job()
        if not row: return
        answer = QtWidgets.QMessageBox.question(self.dialog, '确认暂存原下载',
            '请先在原网页确认云端生成已结束。\n暂存将释放账号给下一条任务；保留原媒体绑定和记录，之后只重试下载。\n确认云端已结束并暂存选中的下载？')
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.dialog.send('release_download', ids=self.dialog.selected_ids(), confirmed_finished=True)

    def open_browser(self):
        row = self.dialog.selected_job()
        if not row: return
        if row['browser'] or row['remote'].get('profile'):
            self.dialog.send('open_browser', job=row['id']); return
        from .routing import dispatch_rank
        client = next((c for c in self.dialog.state.get('clients', [])
                       if dispatch_rank(c, row['payload'], self.dialog.state.get('preferences', {})) is not None), None)
        if client: self.dialog.send('open_browser', browser=client['id'])
        else: self.dialog.note.setText('尚无匹配的已连接浏览器；请在浏览器页选择配置并打开。')

    def repair_download_url(self):
        row = self.dialog.selected_job()
        if not row: return
        value, ok = QtWidgets.QInputDialog.getText(self.dialog, '更新原媒体地址并仅重试下载',
            '原签名地址过期时，可从原项目复制该视频的新地址。只接受原媒体 ID，绝不重新生成：')
        if ok and value.strip(): self.dialog.send('refresh_download_url', job=row['id'], url=value)
