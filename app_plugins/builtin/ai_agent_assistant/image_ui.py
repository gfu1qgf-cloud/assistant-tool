"""Image-specific controls; the host dialog still owns submission and workers."""
from pathlib import Path
from qt_compat import QtCore, QtWidgets
from .tasks import spreadsheet_id, fingerprint, validate_payload
from .sheet_profiles import task_kind
from .image_tasks import image_snapshot, image_sources, bind_image_jobs


class ImageJobsPage(QtWidgets.QWidget):
    def __init__(self, plugin, parent=None):
        super().__init__(parent); self.plugin = plugin; self.jobs = {}; self._busy = False
        settings = plugin.store.settings(); layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.url = QtWidgets.QLineEdit(settings.get('image_url') or settings.get('url', ''), self)
        self.sheet = QtWidgets.QLineEdit(settings.get('image_sheet', '图片转动画'), self)
        form.addRow('图片任务表格', self.url); form.addRow('图片分页名称', self.sheet); layout.addLayout(form)
        bar = QtWidgets.QHBoxLayout(); self.buttons = []
        for text, callback in (('导入所选任务的原图', self.import_selected), ('选择任务图片…', self.choose_images),
                               ('接收表格已有任务…', self.bind_existing), ('提交所选', self.submit)):
            button = QtWidgets.QPushButton(text, self); button.clicked.connect(lambda _=False, fn=callback: self.run(fn))
            bar.addWidget(button); self.buttons.append(button)
        bar.addStretch(1); layout.addLayout(bar)
        self.cancel = QtWidgets.QPushButton('取消当前操作', self)
        self.cancel.clicked.connect(lambda: plugin.handle.requestInterruption() if plugin.handle else None)
        bar.addWidget(self.cancel); self.cancel.setVisible(False)
        note = QtWidgets.QLabel('一图一任务。先在主界面选所属任务，再导入原图或选择本地图片；上传前仍须预览确认。已有云端任务需明确绑定接收目录，不重新派发。', self)
        note.setWordWrap(True); layout.addWidget(note)
        self.table = QtWidgets.QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(['任务编号', '原图／本地图片', '子类别', '用户需求', '状态', '说明'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 130); self.table.setColumnWidth(1, 240); self.table.setColumnWidth(3, 180)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self.edit)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        layout.addWidget(self.table, 1); self.status = QtWidgets.QLabel('尚未导入图片任务。', self)
        self.status.setWordWrap(True); self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText); layout.addWidget(self.status)
        self.url.editingFinished.connect(lambda: self.run(lambda: plugin.store.save_settings(self.target())))
        self.sheet.editingFinished.connect(lambda: self.run(lambda: plugin.store.save_settings(self.target())))
        self.reload()

    def run(self, callback):
        try: callback()
        except Exception as error: self.status.setText(str(error))

    def target(self):
        identifier = spreadsheet_id(self.url.text()); sheet = self.sheet.text().strip()
        if not sheet: raise ValueError('请填写图片分页名称。')
        return {'url': 'https://docs.google.com/spreadsheets/d/' + identifier + '/edit', 'sheet': sheet, 'kind': 'image'}

    def selected_ids(self):
        ids = [self.table.item(index.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
            for index in sorted(self.table.selectionModel().selectedRows(), key=lambda item: item.row())]
        if not ids: raise ValueError('请选择图片任务；Ctrl+A全选、Shift连选。')
        return ids

    def reload(self, select=None):
        previous = set(self.selected_ids()) if self.table.selectionModel().selectedRows() else set()
        jobs = [job for job in self.plugin.store.jobs(limit=100000) if task_kind(job['payload']) == 'image']
        self.jobs = {job['id']: job for job in jobs}; deliveries = self.plugin.store.delivery_statuses()
        self.table.clearSelection()
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            value = job['payload']; receipt = deliveries.get(job['id'], {})
            texts = [value['task_id'], value.get('reference', ''), value.get('subcategory', ''), value.get('user_requirements', ''),
                receipt.get('status') or {'draft':'待提交','failed':'需处理','submitted':'已提交','sending':'待核对','uncertain':'待核对'}[job['state']],
                receipt.get('detail') or job['detail']]
            for col, text in enumerate(texts):
                item = QtWidgets.QTableWidgetItem(str(text)); item.setToolTip(str(text))
                if col == 0: item.setData(QtCore.Qt.ItemDataRole.UserRole, job['id'])
                self.table.setItem(row, col, item)
        chosen = set(select) if select is not None else previous
        for row, job in enumerate(jobs):
            if job['id'] in chosen:
                flags = QtCore.QItemSelectionModel.SelectionFlag
                self.table.selectionModel().select(self.table.model().index(row, 0), flags.Select | flags.Rows)

    def targets(self): return self.plugin.context.task_targets(tuple(self.plugin.context.selected_task_rows()))

    def stage(self, values):
        if self.plugin.handle is not None: raise ValueError('请先结束当前Agent操作。')
        if not values: raise ValueError('所选任务未提供原图，请用“选择任务图片”明确选择文件。')
        if len(values) > 1000: raise ValueError('每次最多导入1000张，请分批选择。')
        result = self.plugin.store.stage(values)
        self.reload(select=result['imported'] + result['unchanged'])
        self.status.setText(f'已导入{len(result["imported"])}张，已存在{len(result["unchanged"])}张；已提交记录不会覆盖。')

    def import_selected(self):
        values = []
        for target in self.targets():
            values.extend(image_snapshot(target, reference) for reference in image_sources(target))
        self.stage(values)

    def one_target(self):
        targets = self.targets()
        if len(targets) != 1: raise ValueError('请在主界面只选一个所属任务，以明确图片和成品的任务目录。')
        return targets[0]

    def choose_images(self):
        target = self.one_target()
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '选择本任务的图片', str(target['target_dir']),
            '图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif)')
        if files: self.stage([image_snapshot(target, name, title=Path(name).name) for name in files])

    def submit(self):
        target = self.target(); ids = [key for key in self.selected_ids() if self.jobs[key]['state'] in {'draft','failed'}]
        if not ids: raise ValueError('所选记录已提交；可直接检查成品，不重复派发。')
        for key in ids: validate_payload(self.jobs[key]['payload'], allow_local_references=True)
        self.plugin.submit(target, ids, 'submit')

    def bind_existing(self):
        self.plugin.query_existing_images(self.target(), self.one_target())

    def busy(self, active):
        self._busy = active
        for widget in [*self.buttons, self.table, self.url, self.sheet]: widget.setEnabled(not active)
        self.cancel.setVisible(active); self.cancel.setEnabled(active)

    def edit(self, row, col):
        def action():
            field = {1:'reference', 2:'subcategory', 3:'user_requirements'}.get(col)
            if not field: return
            job = self.jobs[self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)]
            text, accepted = QtWidgets.QInputDialog.getMultiLineText(self, '编辑图片任务', self.table.horizontalHeaderItem(col).text(), job['payload'].get(field, ''))
            if accepted: self.plugin.store.edit(job['id'], field, text); self.reload()
        self.run(action)

    def menu(self, position):
        index = self.table.indexAt(position)
        if index.isValid() and not self.table.selectionModel().isRowSelected(index.row(), QtCore.QModelIndex()): self.table.selectRow(index.row())
        menu = QtWidgets.QMenu(self)
        for title, callback in (('检查并收取成品', self.plugin.check_received),
                ('重新收取所选成品…', lambda: self.plugin.request_recollect(ids=self.selected_ids())),
                ('核对所选提交结果', lambda: self.plugin.submit(self.target(), self.selected_ids(), 'check')),
                ('移除所选本地记录…', lambda: self.plugin.remove_job_records(self.selected_ids(), self))):
            action = menu.addAction(title); action.triggered.connect(lambda _=False, fn=callback: self.run(fn))
        menu.exec(self.table.viewport().mapToGlobal(position)); menu.deleteLater()


def confirm_image_bind(plugin, result):
    dialog = QtWidgets.QDialog(plugin.dialog or plugin.context.parent_widget); dialog.setWindowTitle('绑定已有图片任务的接收目录'); dialog.resize(850, 520)
    layout = QtWidgets.QVBoxLayout(dialog); binding = result['binding']; rows = result['image_bind_choices']
    label = QtWidgets.QLabel('所属主任务：' + binding['parent_task_id'] + '\n成品接收到：' + str(Path(binding['target_dir'])/'result') + '\n请只选择属于这个主任务的图片行；不会默认把全表交付绑定到同一目录。\n只建立本地接收关系，不重新提交、不改云端状态。Ctrl/Shift多选。', dialog)
    label.setTextFormat(QtCore.Qt.TextFormat.PlainText); label.setWordWrap(True); layout.addWidget(label)
    table = QtWidgets.QTableWidget(len(rows), 4, dialog); table.setHorizontalHeaderLabels(['任务编号','原图','结果','质检'])
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers); table.horizontalHeader().setStretchLastSection(True)
    for row, item in enumerate(rows):
        for col, field in enumerate(('task_id','title','status','quality')): table.setItem(row, col, QtWidgets.QTableWidgetItem(item[field]))
    layout.addWidget(table, 1); buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog)
    buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('确认绑定并收取')
    buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setDefault(True)
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
    if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        chosen = [rows[index.row()] for index in table.selectionModel().selectedRows()]
        if not chosen: raise ValueError('未选择已有图片任务，未绑定。')
        ids = bind_image_jobs(plugin.store, chosen, binding)
        plugin.dialog.image_page.reload(select=ids)
        plugin.receipts.check(manual=True, ids=ids)
    dialog.deleteLater()
