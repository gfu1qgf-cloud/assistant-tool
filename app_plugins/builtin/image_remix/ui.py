from pathlib import Path
import time

from qt_compat import QtCore, QtGui, QtWidgets
from .prompts import MODELS, SIZES, innovation_note, validate_spec
from .store import Store, summary
from .skill_rules import SKILL_ID, SKILL_TITLE, load_snapshot
from .reference_inputs import recent_reference_items

LABELS = {'ready': '等待提交', 'submitting': '正在提交', 'unknown': '提交待核对',
          'running': '云端处理中', 'collecting': '正在收取', 'cancelling': '取消确认中',
          'attention': '需处理（检查可恢复）',
          'paused': '已暂停',
          'planning': '正在创新提示词', 'planning_unknown': '改写提交待核对', 'planning_failed': '提示词创新失败',
          'cancelled': '已取消', 'done': '已完成', 'partial': '部分完成', 'failed': '失败'}


def thumbnail(path, size):
    reader = QtGui.QImageReader(str(path))
    reader.setAutoTransform(True)
    dimensions = reader.size()
    if not dimensions.isValid() or dimensions.width() * dimensions.height() > 40000000:
        return QtGui.QPixmap()
    reader.setScaledSize(dimensions.scaled(size, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
    return QtGui.QPixmap.fromImage(reader.read())


class RemixDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        if getattr(plugin, 'submission_api_version', 0) < 5:
            raise RuntimeError('图片翻版已更新，请重启软件后使用；已阻止新旧提交代码混用。')
        self.plugin = plugin
        self.draft_loaded = False
        self.jobs = []
        self.preview_path = ''
        self.setWindowTitle('GPT 图片翻版 · 即时／Batch')
        self.resize(1130, 800)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        outer = QtWidgets.QVBoxLayout(self)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        left = QtWidgets.QWidget(split)
        left_outer = QtWidgets.QVBoxLayout(left); left_outer.setContentsMargins(0,0,0,0)
        scroll = QtWidgets.QScrollArea(left); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        body = QtWidgets.QWidget(scroll); scroll.setWidget(body); left_outer.addWidget(scroll)
        form = QtWidgets.QVBoxLayout(body)
        form.setContentsMargins(0, 0, 8, 0)
        form.addWidget(QtWidgets.QLabel('文字需求／原始图片提示词', left))
        self.prompt = QtWidgets.QPlainTextEdit(left)
        self.prompt.setPlaceholderText('直接写文字需求即可，参考图可选。GPT按SKILL规范和创新程度构思，不随意推翻人物、故事阶段与明确要求。')
        self.prompt.setMinimumHeight(110)
        self.prompt.setMaximumHeight(150)
        form.addWidget(self.prompt, 1)
        fields = QtWidgets.QFormLayout()
        fields.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        fields.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mode = QtWidgets.QComboBox(left)
        self.mode.addItem('即时生成 · 标准价，不等Batch排队', 'instant')
        self.mode.addItem('Batch · 优惠价，24小时处理窗口', 'batch')
        fields.addRow('提交方式', self.mode)
        self.prompt_skill = QtWidgets.QComboBox(left)
        self.prompt_skill.addItem('不使用SKILL · 通用创新', 'none')
        self.prompt_skill.addItem(SKILL_TITLE, SKILL_ID); self.prompt_skill.setCurrentIndex(1)
        skill_row=QtWidgets.QWidget(left); skill_bar=QtWidgets.QHBoxLayout(skill_row); skill_bar.setContentsMargins(0,0,0,0)
        view_skill=QtWidgets.QPushButton('查看规则',left); view_skill.clicked.connect(self.view_skill)
        skill_bar.addWidget(self.prompt_skill,1);skill_bar.addWidget(view_skill)
        fields.addRow('提示词SKILL',skill_row)
        self.skill_note=QtWidgets.QLabel('文字为主，图片可选；SKILL规范文字创新，点击生成后才继续生图。',left)
        self.skill_note.setWordWrap(True);fields.addRow('',self.skill_note)
        self.reference_mode=QtWidgets.QComboBox(left)
        self.reference_mode.addItem('整图参考 · 理解构图、动作和服装','scene')
        self.reference_mode.addItem('仅人物形象 · 场景和动作可创新','identity')
        fields.addRow('有图时的用途',self.reference_mode)
        self.prompt_skill.currentIndexChanged.connect(self.update_skill)
        self.rewrite_model = QtWidgets.QComboBox(left); self.rewrite_model.setEditable(True)
        self.rewrite_model.addItem('gpt-5-mini')
        self.rewrite_model.setToolTip('先由此文字模型批量改写提示词，再调用图片模型。可填写账号支持Responses与结构化输出的模型ID。')
        text_model_row = QtWidgets.QWidget(left); model_bar = QtWidgets.QHBoxLayout(text_model_row); model_bar.setContentsMargins(0, 0, 0, 0)
        self.refresh_models = QtWidgets.QPushButton('刷新账号模型', left)
        self.refresh_models.clicked.connect(lambda: plugin.launch('models'))
        model_bar.addWidget(self.rewrite_model, 1); model_bar.addWidget(self.refresh_models)
        fields.addRow('提示词模型', text_model_row)
        self.count = QtWidgets.QSpinBox(left); self.count.setRange(1, 500); self.count.setValue(4)
        fields.addRow('生成数量', self.count)
        self.innovation = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, left)
        self.innovation.setRange(0, 100); self.innovation.setValue(20)
        row = QtWidgets.QWidget(left); bar = QtWidgets.QHBoxLayout(row); bar.setContentsMargins(0, 0, 0, 0)
        self.innovation_number = QtWidgets.QLabel(row)
        bar.addWidget(self.innovation, 1); bar.addWidget(self.innovation_number)
        fields.addRow('创新程度', row)
        self.innovation_hint = QtWidgets.QLabel(left); self.innovation_hint.setWordWrap(True)
        fields.addRow('', self.innovation_hint)
        self.innovation.valueChanged.connect(self.update_innovation)
        self.update_innovation()
        self.size = QtWidgets.QComboBox(left)
        for text, size in zip(('竖图 2:3', '方图 1:1', '横图 3:2', '竖屏 9:16', '横屏 16:9'), SIZES):
            self.size.addItem(text + ' · ' + size, size)
        fields.addRow('输出比例', self.size)
        self.quality = QtWidgets.QComboBox(left)
        for title, value in (('低', 'low'), ('中', 'medium'), ('高', 'high')):
            self.quality.addItem(title, value)
        self.quality.setCurrentIndex(1); fields.addRow('质量', self.quality)
        self.model = QtWidgets.QComboBox(left)
        self.model.setEditable(True)
        for title, value in zip(('Flare · 日常生成', 'Sunburst · 精细生成'), MODELS):
            self.model.addItem(value, value)
        self.model.setToolTip('下拉选择，也可直接输入GPT图片模型ID。可用列表由“刷新账号模型”读取，不自动替换已提交任务的模型。')
        fields.addRow('图片模型', self.model)
        model_hint = QtWidgets.QLabel('两个模型都可下拉选择，或直接输入模型ID；刷新不会改动当前选择。', left)
        model_hint.setWordWrap(True); fields.addRow('', model_hint)
        form.addLayout(fields)
        reference_bar = QtWidgets.QHBoxLayout()
        reference_bar.addWidget(QtWidgets.QLabel('参考图（可选，最多4张）', left))
        add = QtWidgets.QPushButton('添加…', left); add.clicked.connect(self.add_references)
        reference_bar.addWidget(add)
        self.recent_references=QtWidgets.QPushButton('最近使用…',left)
        self.recent_references.setToolTip('复用最近提交任务已保存的参考图副本，不额外复制缓存。')
        self.recent_references.clicked.connect(self.choose_recent_references)
        reference_bar.addWidget(self.recent_references)
        self.references = QtWidgets.QListWidget(left)
        self.references.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.references.setIconSize(QtCore.QSize(90, 90)); self.references.setMaximumHeight(105)
        self.references.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.references.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.references.customContextMenuRequested.connect(self.reference_menu)
        self.references.itemDoubleClicked.connect(lambda item: self.preview(item.data(QtCore.Qt.ItemDataRole.UserRole)))
        output_bar = QtWidgets.QHBoxLayout()
        self.output = QtWidgets.QLineEdit(left); self.output.setPlaceholderText('选择结果保存目录；每批创建独立子目录')
        choose = QtWidgets.QPushButton('目录…', left); choose.clicked.connect(self.choose_output)
        output_bar.addWidget(self.output, 1); output_bar.addWidget(choose); form.addLayout(output_bar)
        right = QtWidgets.QWidget(split); preview_layout = QtWidgets.QVBoxLayout(right)
        preview_layout.setContentsMargins(4, 0, 0, 0)
        preview_layout.addLayout(reference_bar)
        preview_layout.addWidget(self.references)
        self.image = QtWidgets.QLabel('选择参考图或生成结果查看完整画面', right)
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(300, 260)
        self.image.setStyleSheet('background:palette(base);border:1px solid palette(mid);')
        preview_layout.addWidget(self.image, 1)
        self.results = QtWidgets.QListWidget(right); self.results.setMaximumHeight(110)
        self.results.itemClicked.connect(lambda item: self.preview(item.data(QtCore.Qt.ItemDataRole.UserRole)))
        self.results.itemDoubleClicked.connect(lambda item: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(item.data(QtCore.Qt.ItemDataRole.UserRole))))
        preview_layout.addWidget(self.results)
        split.setSizes([510, 610]); outer.addWidget(split, 3)
        actions = QtWidgets.QHBoxLayout()
        self.generate = QtWidgets.QPushButton('生成 · 提交Batch', self)
        self.generate.clicked.connect(self.generate_clicked); actions.addWidget(self.generate)
        self.check = QtWidgets.QPushButton('检查并收取结果', self)
        self.check.clicked.connect(lambda: plugin.check(manual=True)); actions.addWidget(self.check); actions.addStretch()
        self.stop_button = QtWidgets.QPushButton('停止后续图片', self)
        self.stop_button.clicked.connect(self.stop_instant); actions.addWidget(self.stop_button)
        self.mode_note = QtWidgets.QLabel(self)
        self.mode_note.setWordWrap(True); actions.addWidget(self.mode_note); outer.addLayout(actions)
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.update_mode()
        self.history = QtWidgets.QTableWidget(0, 5, self)
        self.history.setHorizontalHeaderLabels(['提交时间', '提示词', '状态／数量', '创新', '模型'])
        self.history.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.history.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.history.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.verticalHeader().hide()
        header = self.history.horizontalHeader()
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.history.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self.history_menu)
        self.history.itemSelectionChanged.connect(self.show_results)
        self.history.setMinimumHeight(145); outer.addWidget(self.history, 1)
        self.status = QtWidgets.QLabel(plugin.message, self); self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText); outer.addWidget(self.status)
        self.refresh(plugin.jobs, plugin.message, plugin.handle is not None)
        self.update_skill()

    def update_innovation(self):
        value = self.innovation.value()
        self.innovation_number.setText(str(value))
        note=innovation_note(value)
        if hasattr(self,'prompt_skill') and self.prompt_skill.currentData()!='none':
            note+=' SKILL约束始终有效：人物身份、故事阶段、人体受力和明确要求不因高创新而放宽。'
        self.innovation_hint.setText(note)

    def update_skill(self, *_args):
        enabled=self.prompt_skill.currentData()!='none'
        self.reference_mode.setEnabled(enabled)
        self.skill_note.setVisible(enabled)
        self.update_innovation()

    def view_skill(self):
        try:
            snapshot=load_snapshot(self.prompt_skill.currentData())
            if snapshot is None:
                self.status.setText('当前不使用SKILL，按通用创新规则处理。');return
            dialog=QtWidgets.QDialog(self);dialog.setWindowTitle(SKILL_TITLE+' · 文字创新规则');dialog.resize(780,650)
            layout=QtWidgets.QVBoxLayout(dialog);text=QtWidgets.QPlainTextEdit(dialog);text.setReadOnly(True)
            text.setPlainText('\n\n'.join('--- '+name+' ---\n'+body for name,body in snapshot['documents'].items()))
            layout.addWidget(text);dialog.exec()
        except Exception as error:self.status.setText(str(error))

    def update_mode(self):
        instant = self.mode.currentData() == 'instant'
        self.generate.setText('生成 · 即时提交' if instant else '生成 · 提交Batch')
        self.mode_note.setText('先文字创新，再生图；两步均按标准价计费。逐张保存，结果不明不自动重发。'
                              if instant else '两步均用Batch优惠价；先改写后生图，可能需要两轮24小时处理窗口。')

    def stop_instant(self):
        job = self.selected_job()
        if not job or job['spec'].get('mode', 'batch') != 'instant':
            return
        try:
            Store(self.plugin.context.storage.path).request_stop(job['id'])
            self.status.setText('已要求停止后续图片。当前已发送的请求不能保证撤回或退费；返回后仍会保存结果。')
            if not self.plugin.busy(): self.plugin.check(manual=True)
        except Exception as error:
            self.status.setText(str(error))

    def spec(self):
        return {'rewrite_enabled': True, 'rewrite_model': self.rewrite_model.currentText().strip(),
                'prompt_skill':self.prompt_skill.currentData(),
                'reference_mode':self.reference_mode.currentData() if self.prompt_skill.currentData()!='none' else 'identity',
                'mode': self.mode.currentData(), 'prompt': self.prompt.toPlainText(), 'count': self.count.value(),
                'innovation': self.innovation.value(), 'size': self.size.currentData(),
                'quality': self.quality.currentData(), 'model': self.model.currentText().strip(),
                'output': self.output.text().strip(),
                'references': [self.references.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                               for i in range(self.references.count())]}

    def load_draft(self, draft, *, preserve_legacy=False):
        self.draft_loaded = True
        if self.prompt.toPlainText().strip() or not draft:
            return
        self.prompt.setPlainText(str(draft.get('prompt', '')))
        profile=draft.get('prompt_skill','none' if preserve_legacy else SKILL_ID)
        index=self.prompt_skill.findData(profile)
        if index>=0:self.prompt_skill.setCurrentIndex(index)
        index=self.reference_mode.findData(draft.get('reference_mode','identity'))
        if index>=0:self.reference_mode.setCurrentIndex(index)
        self.rewrite_model.setCurrentText(str(draft.get('rewrite_model') or 'gpt-5-mini'))
        for key in ('count', 'innovation'):
            if type(draft.get(key)) is int:
                getattr(self, key).setValue(draft[key])
        self.model.setCurrentText(str(draft.get('model') or MODELS[0]))
        for key in ('mode', 'size', 'quality'):
            widget = getattr(self, key); index = widget.findData(draft.get(key, 'batch' if key == 'mode' else None))
            if index >= 0: widget.setCurrentIndex(index)
        self.output.setText(str(draft.get('output', '')))
        for path in draft.get('references', [])[:4]:
            if isinstance(path, str) and Path(path).is_file(): self.add_reference(path)

    def choose_output(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, '结果保存目录', self.output.text())
        if directory: self.output.setText(directory)

    def set_models(self, models):
        for widget, key in ((self.rewrite_model, 'text'), (self.model, 'image')):
            selected = widget.currentText()
            widget.blockSignals(True)
            widget.clear()
            values = list(dict.fromkeys([selected, *models.get(key, [])]))
            for value in values:
                if value: widget.addItem(value, value)
            widget.setCurrentText(selected); widget.blockSignals(False)

    def add_references(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '添加参考图', '', '图片 (*.png *.jpg *.jpeg *.webp)')
        for path in files:
            if self.references.count() >= 4: break
            if path not in self.spec()['references']: self.add_reference(path)

    def add_reference(self, path):
        item = QtWidgets.QListWidgetItem(QtGui.QIcon(thumbnail(path, QtCore.QSize(90, 90))), Path(path).name)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, path); item.setToolTip(path)
        self.references.addItem(item)

    def choose_recent_references(self):
        rows=recent_reference_items(self.jobs,self.plugin.context.storage.path)
        if not rows:
            self.status.setText('还没有已提交任务的可用参考图；添加并提交一次后就能在这里找回。');return
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('最近使用的参考图');dialog.resize(650,440)
        layout=QtWidgets.QVBoxLayout(dialog)
        tip=QtWidgets.QLabel('选择最近提交使用过的图片；复用原快照，不要求原文件仍在。最多选择到合计4张。',dialog)
        tip.setWordWrap(True);layout.addWidget(tip)
        images=QtWidgets.QListWidget(dialog);images.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        images.setIconSize(QtCore.QSize(110,110));images.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        images.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        for row in rows:
            item=QtWidgets.QListWidgetItem(QtGui.QIcon(thumbnail(row['path'],QtCore.QSize(110,110))),row['name'])
            item.setData(QtCore.Qt.ItemDataRole.UserRole,row['path']);item.setToolTip(row['name'])
            images.addItem(item)
        layout.addWidget(images)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel,dialog)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('使用所选')
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        images.itemDoubleClicked.connect(lambda _item:dialog.accept())
        if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted:
            for item in images.selectedItems():
                path=item.data(QtCore.Qt.ItemDataRole.UserRole)
                if self.references.count()>=4:break
                if path not in self.spec()['references']:self.add_reference(path)
            if images.selectedItems():self.preview(images.selectedItems()[0].data(QtCore.Qt.ItemDataRole.UserRole))

    def reference_menu(self, position):
        menu = QtWidgets.QMenu(self)
        action = menu.addAction('移除所选参考图')
        if menu.exec(self.references.mapToGlobal(position)) == action:
            for item in self.references.selectedItems(): self.references.takeItem(self.references.row(item))

    def preview(self, path):
        self.preview_path = str(path or '')
        if not self.preview_path: return
        pixmap = thumbnail(self.preview_path, self.image.size())
        if pixmap.isNull(): self.image.setText('图片暂时无法读取，请检查文件是否存在。')
        else: self.image.setPixmap(pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.preview_path: self.preview(self.preview_path)

    def generate_clicked(self):
        try:
            spec = validate_spec(self.spec())
            if not Path(spec['output']).is_absolute() or not Path(spec['output']).is_dir():
                raise ValueError('请先选择有效的结果保存目录。')
            answer = QtWidgets.QMessageBox.question(self, '提交付费图片批次',
                f'生成 {spec["count"]} 张，创新程度 {spec["innovation"]}，{self.quality.currentText()}质量。\n'
                f'模型：{spec["model"]}\n参考图：{len(spec["references"])} 张（会上传给OpenAI）。\n' +
                ('SKILL：'+SKILL_TITLE+'；有图时文字模型也会读取缩略图，产生图片输入费用。\n' if spec['prompt_skill']!='none' else '') +
                f'先用 {spec["rewrite_model"]} 创新提示词（另有文字费用），完成并校验后才生图。\n' +
                ('即时生成：使用普通API标准价格，不享受Batch折扣。逐张提交，按实际用量计费。确认提交？'
                 if spec['mode'] == 'instant' else '两步使用Batch优惠价，可能需要两轮24小时处理窗口。确认提交？'),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                if self.plugin.submit(spec):
                    self.status.setText('正在保存输入快照并提交；请勿重复点击。关闭此窗口不取消。')
        except Exception as error:
            self.status.setText(str(error))

    def selected_job(self):
        row = self.history.currentRow()
        if 0 <= row < len(self.jobs): return self.jobs[row]
        return None

    def refresh(self, jobs, message, busy):
        selected = self.selected_job()
        identifier = selected['id'] if selected else None
        self.jobs = jobs
        self.history.blockSignals(True)
        self.history.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            done, failed, counts = summary(job)
            cloud = counts.get('completed', 0)
            status = LABELS.get(job['status'], job['status'])
            mode = '即时' if job['spec'].get('mode', 'batch') == 'instant' else 'Batch'
            text = f'{mode} · {status} · 已收取 {done}/{len(job["items"])}'
            if job.get('planning') and job['planning']['state'] != 'done':
                planned = sum(len(c['indices']) for c in job['planning']['chunks'] if c['state'] == 'done')
                text += f' · 创意方案 {planned}/{len(job["items"])}'
            if mode == 'Batch' and job['status'] in ('running', 'cancelling'): text += f' · 云端完成 {cloud}'
            values = [time.strftime('%m-%d %H:%M', time.localtime(job['created'])),
                      job['spec']['prompt'].replace('\n', ' ')[:120], text,
                      str(job['spec']['innovation']), job['spec']['model'].removeprefix('gpt-image-')]
            detail = job.get('error') or '\n'.join(i['error'] for i in job['items'] if i.get('error'))[:2500]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(detail or value); self.history.setItem(row, column, item)
            if job['id'] == identifier: self.history.selectRow(row)
        if identifier is None and jobs: self.history.selectRow(0)
        self.history.blockSignals(False)
        self.generate.setEnabled(not busy); self.check.setEnabled(not busy)
        self.refresh_models.setEnabled(not busy)
        self.status.setText(message)
        self.show_results()

    def show_results(self):
        job = self.selected_job(); self.results.clear()
        self.results.setVisible(False)
        self.stop_button.setEnabled(bool(job and job['spec'].get('mode', 'batch') == 'instant'
                                        and job['status'] in ('ready', 'running', 'submitting', 'planning')))
        if not job: return
        for result in job['items']:
            if result['status'] == 'done':
                item = QtWidgets.QListWidgetItem(f'{result["index"] + 1:04d}.png')
                item.setData(QtCore.Qt.ItemDataRole.UserRole, result['path']); self.results.addItem(item)
        self.results.setVisible(self.results.count()>0)
        if self.results.count() and self.preview_path not in [self.results.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                                                            for i in range(self.results.count())]:
            self.preview(self.results.item(0).data(QtCore.Qt.ItemDataRole.UserRole))

    def history_menu(self, position):
        row = self.history.rowAt(position.y())
        if row >= 0: self.history.selectRow(row)
        job = self.selected_job()
        if not job: return
        menu = QtWidgets.QMenu(self)
        inspect = menu.addAction('查看提示词与错误详情')
        folder = menu.addAction('打开结果目录')
        reuse = menu.addAction('用这套参数再做一批…')
        menu.addSeparator()
        instant = job['spec'].get('mode', 'batch') == 'instant'
        retry = menu.addAction('继续未提交／重试失败图片…' if instant else '仅重试失败图片…')
        retry.setEnabled((job['status'] in ('partial', 'failed', 'cancelled', 'paused', 'unknown') if instant
                          else job['status'] in ('partial', 'failed', 'cancelled')) and not self.plugin.busy())
        resume = menu.addAction('仅继续尚未提交的图片…') if instant else None
        uncertain = menu.addAction('确认重试未确认图片（可能重复计费）…') if instant else None
        if instant:
            resume.setEnabled(any(i['status'] == 'pending' for i in job['items']) and not self.plugin.busy())
            uncertain.setEnabled(any(i['status'] == 'unknown' for i in job['items']) and not self.plugin.busy())
        cancel = menu.addAction('停止后续图片…' if instant else '取消云端批次…')
        cancel.setEnabled(job['status'] not in ('done', 'partial', 'failed', 'cancelled') and not self.plugin.busy())
        remove = menu.addAction('删除记录（保留图片）…')
        remove.setEnabled(job['status'] in ('done', 'partial', 'failed', 'cancelled') and not self.plugin.busy())
        bind = menu.addAction('关联控制台批次ID（提交不明时备用）…')
        bind.setEnabled(not instant and job['status'] in ('unknown', 'submitting', 'planning_unknown') and not self.plugin.busy())
        planning_retry = menu.addAction('重试失败／未确认的提示词创新…')
        planning_retry.setEnabled(job.get('planning', {}).get('state') in ('failed', 'unknown')
                                  and not job['attempts'] and not self.plugin.busy())
        chosen = menu.exec(self.history.viewport().mapToGlobal(position))
        try:
            if chosen == folder: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(job['output']))
            elif chosen == inspect:
                dialog = QtWidgets.QDialog(self); dialog.setWindowTitle('图片批次详情'); dialog.resize(750, 600)
                layout = QtWidgets.QVBoxLayout(dialog); text = QtWidgets.QPlainTextEdit(dialog); text.setReadOnly(True)
                content = ['原始提示词：', job['spec']['prompt'], '\n批次：', *[a.get('batch_id') or a['state'] for a in job['attempts']],
                           '\n最近提示：', job.get('error', '')]
                if job.get('skill'):content += ['\nSKILL快照：',job['skill']['id'],job['skill']['version'],job['skill']['sha256']]
                content += [f'\n{i["index"] + 1:04d} · {i["status"]}\n{i["prompt"]}\n{i["error"]}' for i in job['items']]
                text.setPlainText('\n'.join(content)); layout.addWidget(text); dialog.exec()
            elif chosen == reuse:
                self.prompt.clear(); self.references.clear(); self.load_draft(job['spec'],preserve_legacy=True)
                # Prefer persistent snapshots when original reference files moved.
                self.references.clear()
                for ref in job['references']:
                    self.add_reference(str(self.plugin.context.storage.path / job['id'] / 'references' / ref['file']))
            elif chosen == bind:
                identifier, accepted = QtWidgets.QInputDialog.getText(self, '关联已存在批次',
                    '从OpenAI控制台复制本次batch_开头的ID。只关联身份匹配的原批次，不重新生成：')
                if accepted and identifier.strip():
                    self.plugin.launch('bind', id=job['id'], batch_id=identifier.strip())
            elif chosen == planning_retry:
                if QtWidgets.QMessageBox.question(self, '重试提示词创新',
                    '仅重试未完成／失败的文字改写，已接受方案保留，不重新生图。未确认的文字请求可能已计费，重试可能产生额外文字费用。继续？',
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No) == QtWidgets.QMessageBox.StandardButton.Yes:
                    self.plugin.launch('retry_planning', id=job['id'])
            elif chosen is not None and chosen in (retry, cancel, remove, resume, uncertain):
                action = {retry: 'retry', cancel: 'cancel', remove: 'delete', resume: 'resume', uncertain: 'retry_unknown'}[chosen]
                message = {'retry': ('按标准价继续未提交和已明确失败的图片；未确认请求不重发，已成功图片不重做。' if instant
                                     else '仅重新生成失败图片，会产生新的Batch费用；已成功图片不会重做。'),
                           'resume': '仅提交尚未发送的图片，按标准价计费；已完成和结果未确认的请求都不重发。',
                           'retry_unknown': '警告：未确认的请求可能已在云端成功并计费。继续会重新生成这些图片，可能重复计费；也会继续未提交／失败项。确认承担这笔费用？',
                           'cancel': ('停止后续图片，当前已发送请求不能保证撤回或退费，返回后仍保存。' if instant
                                      else '请求取消剩余云端生成。已执行的图片仍可能计费并会继续收取；取消可能需要数分钟。'),
                           'delete': '删除本地任务记录，但保留所有已保存图片及参考图。'}[action]
                if QtWidgets.QMessageBox.question(self, '确认操作', message,
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No) == QtWidgets.QMessageBox.StandardButton.Yes:
                    self.plugin.launch(action, id=job['id'])
        except Exception as error:
            self.status.setText(str(error))

    def closeEvent(self, event):
        # Hiding does not cancel a submitted batch or its short check worker.
        try:
            Store(self.plugin.context.storage.path).draft(self.spec())
        except Exception as error:
            self.plugin.failed('草稿保存失败：' + str(error))
        event.accept()
