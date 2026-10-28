"""One manual editor, an editable staged list and explicit receipt-bound submission."""
import copy
import hashlib
import json
from pathlib import Path
import uuid
from qt_compat import QtCore,QtGui,QtWidgets
from .manual_panel import estimate_text
from .status_ui import status_area

INDEX=hashlib.sha256(b'flow-batch-compose-index-v1').hexdigest()
def row_key(value):return hashlib.sha256(('flow-batch-row-v1:'+value).encode()).hexdigest()
def core(row):return {k:copy.deepcopy(row[k]) for k in ('target','image','prompt','options')}
def signature(row):return hashlib.sha256(json.dumps(core(row),sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class BatchWorkspace(QtWidgets.QWidget):
    def __init__(self,dialog):
        super().__init__(dialog);self.dialog=dialog;self.manual=dialog.manual
        self.rows=[];self.editing='';self.loading=False;self.dirty=False;self.loaded=False;self.pending=None;self.request_id=''
        self.written={};self.defaults={'duration':10,'aspect':'9:16','resolution':'720p','count':1,'budget':0}
        layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        split=QtWidgets.QSplitter(self)
        side=QtWidgets.QWidget(self);self.side=side;right=QtWidgets.QVBoxLayout(side);right.setContentsMargins(0,0,0,0)
        top=QtWidgets.QHBoxLayout()
        self.new_button=QtWidgets.QPushButton('新建条目',self);self.new_button.clicked.connect(self.new_item)
        self.import_button=QtWidgets.QPushButton('粘贴多段提示词…',self);self.import_button.clicked.connect(self.import_prompts)
        self.new_button.setMaximumWidth(120);self.import_button.setMaximumWidth(220)
        top.addWidget(self.new_button);top.addWidget(self.import_button);top.addStretch();right.addLayout(top)
        self.more=QtWidgets.QToolButton(self);self.more.setText('更多');self.more.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu=QtWidgets.QMenu(self.more)
        for label,callback in [('所选图片加入清单',dialog.add_selected_images),('全部任务图片加入清单',self.add_all_images),('复制选中条目为新条目',self.clone_selected),
                               ('左侧参数应用到选中条目',self.apply_options),('左侧提示词应用到选中条目',self.apply_prompt)]:menu.addAction(label,callback)
        menu.addAction('复制上一条提示词',self.manual.copy_button.click)
        self.more.setMenu(menu);top.addWidget(self.more)
        self.mode=QtWidgets.QLabel('录入新条目；加入清单后点选右侧记录即可修改。',self)
        self.mode.hide()
        self.views=QtWidgets.QTabWidget(self);self.views.setDocumentMode(True)
        self.compose=QtWidgets.QTableWidget(0,4,self);self.compose.setHorizontalHeaderLabels(['任务','图片','完整提示词','秒数'])
        self.compose.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.compose.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.compose.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.compose.setColumnWidth(0,65);self.compose.setColumnWidth(1,95);self.compose.setColumnWidth(3,70)
        self.compose.horizontalHeader().setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.compose.verticalHeader().setVisible(False);self.compose.setMinimumWidth(340)
        self.views.addTab(self.compose,'待提交清单');self.views.addTab(self.manual.recent,'最近任务／成品')
        right.addWidget(self.views,1)
        actions=QtWidgets.QHBoxLayout()
        remove=QtWidgets.QPushButton('删除选中条目',self);remove.clicked.connect(self.remove_selected)
        actions.addWidget(remove);actions.addStretch()
        self.manual.collection.layout().removeWidget(self.manual.collect_submit)
        actions.addWidget(self.manual.collect_submit);right.addLayout(actions)
        limits=QtWidgets.QHBoxLayout()
        self.batch_label=QtWidgets.QLineEdit(self);self.batch_label.setPlaceholderText('批次名称（可选）')
        self.save_only=QtWidgets.QCheckBox('只保存记录，暂不生成',self)
        self.save_only.setToolTip('仅需保存而暂不生成时使用。取消勾选后点“提交表格任务”，会放行本表已保存的记录。')
        saved_action=QtWidgets.QWidgetAction(menu);saved_action.setDefaultWidget(self.save_only);menu.addSeparator();menu.addAction(saved_action)
        self.manual.parameter_popup.layout().addWidget(QtWidgets.QLabel('批次名称',self.manual.parameter_popup))
        self.manual.parameter_popup.layout().addWidget(self.batch_label)
        self.estimate=QtWidgets.QLabel(self)
        self.status=QtWidgets.QLabel('手动与多段识别共用编辑器，默认10秒。',self)
        # Wide list above its single editor; the portrait reference has a narrow,
        # tall column rather than consuming the list's horizontal reading space.
        left=QtWidgets.QWidget(self);left_layout=QtWidgets.QVBoxLayout(left);left_layout.setContentsMargins(0,0,0,0)
        self.manual.layout().removeWidget(self.manual.picker)
        self.manual.picker.layout().removeWidget(self.manual.picker.task_selector)
        left_layout.addWidget(self.manual.picker.task_selector)
        vertical=QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical,self);vertical.addWidget(side);vertical.addWidget(self.manual)
        self.vertical=vertical;self.balanced=False
        vertical.setSizes([245,440]);vertical.setStretchFactor(0,1);vertical.setStretchFactor(1,2)
        left_layout.addWidget(vertical,1)
        preview=QtWidgets.QWidget(self);preview_layout=QtWidgets.QVBoxLayout(preview);preview_layout.setContentsMargins(0,0,0,0)
        self.manual.picker.use_portrait_layout();preview_layout.addWidget(self.manual.picker,1)
        self.batch_label.setMaximumWidth(180);self.batch_label.setToolTip('可选的提交批次名称。')
        preview_layout.addWidget(status_area(self.estimate,2));preview_layout.addWidget(status_area(self.status,2))
        split.addWidget(left);split.addWidget(preview);split.setStretchFactor(0,3);split.setStretchFactor(1,1);split.setSizes([880,310]);layout.addWidget(split,1)
        self.manual.collect_only.setText('先加入清单，最后统一提交（默认开启）')
        self.manual.collect_only.setText('先加入清单')
        self.manual.collect_only.setToolTip('开启后手动添加只进入可编辑清单，不生成；关闭后手动按钮直接保存到生成队列。')
        self.manual.collect_submit.clicked.disconnect();self.manual.collect_submit.clicked.connect(self.enqueue)
        self.manual.collect_discard.clicked.disconnect();self.manual.collect_discard.clicked.connect(self.discard)
        self.manual.collection.hide();self.manual.collect_only.hide();self.manual.collect_discard.hide()
        self.timer=QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(500);self.timer.timeout.connect(self.flush)
        self.compose.currentCellChanged.connect(self.select_row);self.compose.itemChanged.connect(self.table_changed)
        self.manual.prompt.textChanged.connect(self.editor_changed)
        for widget in (self.manual.options.duration,self.manual.options.aspect,self.manual.options.resolution):widget.currentIndexChanged.connect(self.editor_changed)
        for widget in (self.manual.options.count,self.manual.options.budget):widget.valueChanged.connect(self.editor_changed)
        self.manual.picker.image_changed.connect(self.image_changed)
        self.manual.batch_limit.valueChanged.connect(self.changed);self.batch_label.textChanged.connect(self.changed)
        self.save_only.toggled.connect(self.update_controls);self.save_only.toggled.connect(self.changed)
        self.update_controls()

    def showEvent(self,event):
        super().showEvent(event)
        if not self.balanced:
            self.balanced=True
            QtCore.QTimer.singleShot(0,lambda:self.vertical.setSizes([max(150,int(self.vertical.height()*.32)),max(280,int(self.vertical.height()*.68))]))

    def entry(self,value=None):return next((r for r in self.rows if r['id']==(value or self.editing)),None)
    def pending_rows(self):return [r for r in self.rows if not r.get('discarded') and r.get('saved_signature')!=signature(r)]
    def changed(self,*_):
        if self.loading:return
        self.dirty=True;self.timer.start();self.update_controls()

    def add_entry(self,target,image,prompt,options=None,*,title='',warning='',entry=None):
        if len(self.rows)>=300 and entry is None:raise ValueError('清单最多300条，请先提交或移除部分记录。')
        if not str(prompt).strip():raise ValueError('提示词为空，请先填写。')
        if len(prompt)>60000:raise ValueError('单条提示词最多6万字符，请拆分后添加。')
        row=copy.deepcopy(entry) if entry else {'id':uuid.uuid4().hex,'target':copy.deepcopy(target),'image':str(image),
            'prompt':prompt,'options':dict(options or self.defaults),'title':title,'warning':warning}
        if entry is None:row['staged_signature']=signature(row)
        self.rows.append(row);self.render_row(len(self.rows)-1);self.changed();return row

    def render_row(self,index):
        row=self.rows[index];table=self.compose
        with QtCore.QSignalBlocker(table):
            if table.rowCount()<=index:table.insertRow(index)
            task=row['target'].get('task',{});flag='已放弃' if row.get('discarded') else '已保存' if row.get('saved_signature')==signature(row) else '待提交'
            for col,text in enumerate((str(task.get('task_id') or row['target'].get('label','')),Path(row['image']).name,row['prompt'])):
                item=QtWidgets.QTableWidgetItem(text);item.setData(QtCore.Qt.ItemDataRole.UserRole,{'target':row['target'],'image':row['image'],'entry_id':row['id']})
                item.setToolTip((row.get('title','')+'\n'+row.get('warning','')+'\n'+flag+'\n'+text).strip())
                item.setBackground(QtGui.QColor('#e2f3e8' if flag=='已保存' else '#f0f0f0' if flag=='已放弃' else '#ffffff'));table.setItem(index,col,item)
            duration=QtWidgets.QComboBox(table);duration.addItem('默认',None)
            for seconds in (4,6,8,10):duration.addItem(str(seconds),seconds)
            duration.setCurrentIndex(duration.findData(row['options'].get('duration',10)))
            duration.currentIndexChanged.connect(lambda _i,rid=row['id'],combo=duration:self.set_duration(rid,combo.currentData()))
            table.setCellWidget(index,3,duration);table.setRowHeight(index,42)

    def set_duration(self,value,seconds):
        row=self.entry(value)
        if not row or self.loading or self.request_id:return
        row['options']['duration']=seconds;row.pop('discarded',None)
        if self.editing==value:
            with QtCore.QSignalBlocker(self.manual.options.duration):self.manual.options.duration.setCurrentIndex(self.manual.options.duration.findData(seconds))
            self.manual.update_route()
        self.changed()

    def table_changed(self,item):
        # Keep the legacy programmatic table editing seam, with one real editor.
        if self.loading or item.column()!=2 or item.row()>=len(self.rows):return
        row=self.rows[item.row()];row['prompt']=item.text()
        if row['id']==self.editing:
            self.loading=True
            try:self.manual.prompt.setPlainText(row['prompt'])
            finally:self.loading=False
        self.changed()

    def select_row(self,index,*_):
        if self.loading or not 0<=index<len(self.rows):return
        row=self.rows[index];self.loading=True
        try:
            self.manual.previous_prompt=self.manual.prompt.toPlainText();self.editing=row['id']
            job={'directory':row['target']['target_dir'],'task_id':row['target'].get('task',{}).get('task_id',''),
                'task_name':row['target'].get('task',{}).get('task_name',''),'image':row['image'],'prompt':row['prompt'],**row['options']}
            self.manual.restore_job({'payload':job},count=row['options'].get('count',1),for_edit=True)
        finally:self.loading=False
        self.update_controls()
        self.manual.status.setText(f'编辑清单第{index+1}条')

    def image_changed(self,image):
        if not self.loading and self.entry() and self.entry()['image']!=image:self.new_item()

    def editor_changed(self,*_):
        if self.loading or self.request_id:return
        row=self.entry()
        if not row or row['image']!=self.manual.current_image:return
        row['prompt']=self.manual.prompt.toPlainText();row['options']=self.manual.options.values();row.pop('discarded',None)
        self.render_row(self.rows.index(row));self.changed()

    def new_item(self,*_):
        self.editing=''
        with QtCore.QSignalBlocker(self.compose):self.compose.clearSelection();self.compose.setCurrentCell(-1,-1)
        self.update_controls()
        self.manual.status.setText('新条目')

    def add_manual(self,*,advance=False):
        if self.request_id:return
        try:
            target,image=self.manual.picker.selection();prompt=self.manual.prompt.toPlainText()
            if not prompt.strip():raise ValueError('请填写本条提示词。')
            existing=self.entry()
            if existing and existing['image']==image:
                self.editor_changed();existing['staged_signature']=signature(existing);row=existing
                self.status.setText('已保存表格修改，等待提交表格任务。')
                if advance:
                    index=self.rows.index(existing)+1
                    if index<len(self.rows):self.compose.setCurrentCell(index,0)
                    else:self.new_item()
            else:
                row=self.add_entry(target,image,prompt,self.manual.options.values());self.status.setText('已加入待提交清单，尚未生成。')
                if advance:
                    self.new_item();picker=self.manual.picker
                    if picker.images.currentRow()+1<picker.images.count():picker.images.setCurrentRow(picker.images.currentRow()+1)
                    elif picker.target.currentIndex()+1<picker.target.count():picker.target.setCurrentIndex(picker.target.currentIndex()+1)
                else:self.new_item()
            self.manual.flush_draft();self.manual.mark_submission([row],'table');self.flush()
            self.manual.status.setText('已保存到表格，尚未生成。');self.update_controls()
        except (OSError,ValueError) as error:self.manual.status.setText(str(error));self.status.setText(str(error))

    def import_prompts(self):
        try:target,image=self.manual.picker.selection()
        except (OSError,ValueError) as error:self.status.setText(str(error));return
        from .prompt_import_ui import PromptImportDialog
        editor=PromptImportDialog(Path(image).name,self)
        if editor.exec()!=QtWidgets.QDialog.DialogCode.Accepted:return
        self.import_segments(editor.segments,target,image)

    def import_segments(self,segments,target,image):
        if self.request_id:return
        if len(self.rows)+len(segments)>300:raise ValueError('清单合计超过300条，请分批添加。')
        if any(not part.prompt.strip() or len(part.prompt)>60000 for part in segments):raise ValueError('存在空段或超过6万字符的段落，请先核对。')
        options={**self.manual.options.values(),'duration':10};first=len(self.rows)
        for part in segments:
            reference=f'标题标注{part.detected_duration}秒；当前默认10秒。' if part.detected_duration else ''
            self.add_entry(target,image,part.prompt,options,title=part.title,warning=' '.join(filter(None,[reference,part.warning])))
        if segments:self.compose.setCurrentCell(first,0)
        self.status.setText(f'已加入{len(segments)}段，全部使用当前图片、默认10秒；可逐条编辑。');self.flush()

    def add_image(self,target,path):
        return self.add_entry(target,path,target.get('task',{}).get('task_video_prompt','') or self.manual.prompt.toPlainText(),
                              {**self.manual.options.values(),'duration':10})

    def add_all_images(self):
        from .store import IMAGE_SUFFIXES
        try:
            for target in self.dialog.targets:
                for path in sorted(Path(target['target_dir']).iterdir()):
                    if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:self.add_image(target,path)
        except (OSError,ValueError) as error:self.status.setText(str(error))

    def selected_indices(self):return sorted({item.row() for item in self.compose.selectedItems()})
    def remove_selected(self):
        if self.request_id:return
        if self.pending:self.status.setText('请先核对上次保存结果，再删除清单条目。');return
        selected=self.selected_indices()
        for index in reversed(selected):self.rows.pop(index);self.compose.removeRow(index)
        self.new_item();self.changed();self.status.setText('仅移除了填写清单中的条目；已保存到生成队列的记录不删除。')

    def clone_selected(self):
        if self.request_id:return
        for index in self.selected_indices():
            row=self.rows[index];self.add_entry(row['target'],row['image'],row['prompt'],row['options'],title=row.get('title',''))

    def apply_options(self):
        options=self.manual.options.values()
        for index in self.selected_indices():
            self.rows[index]['options']=dict(options);self.rows[index].pop('discarded',None);self.render_row(index)
        self.changed()

    def apply_prompt(self):
        for index in self.selected_indices():
            self.rows[index]['prompt']=self.manual.prompt.toPlainText();self.rows[index].pop('discarded',None);self.render_row(index)
        self.changed()

    def update_controls(self,*_):
        pending=self.pending_rows();stored=len(self.stored_ids())
        self.manual.collect_submit.setText('核对添加结果' if self.pending and not self.request_id else '提交表格任务')
        self.manual.collect_submit.setToolTip(f'尚未提交：{len(pending)}条；仅保存未生成：{stored}条。点击一次即可提交并安排生成，成功后清空对应行；失败时保留。')
        self.manual.collect_submit.setEnabled(not self.request_id and bool(pending or stored or self.pending))
        self.manual.collect_discard.setText(f'放弃待提交任务（{len(pending)+stored}）');self.manual.collect_discard.setEnabled(not self.request_id and bool(pending or stored))
        editing=self.entry()
        self.manual.submit.setText('保存修改' if editing else '追加任务')
        self.manual.submit_next.setText('保存并查看下一条' if editing else '追加并切换下一张')
        self.manual.editor_title.setText(f'提示词 · 正在修改第{self.rows.index(editing)+1}条' if editing else '提示词')
        self.manual.editor_title.setStyleSheet('color:#1763a6;font-weight:600;' if editing else '')
        self.mode.setText(f'正在修改清单第{self.rows.index(editing)+1}条；修改自动留在清单。' if editing else '录入新条目；加入清单后点选右侧记录即可修改。')
        self.manual.update_prompt_submission()
        self.estimate.setText(estimate_text([{'images':[r['image']],**r['options']} for r in pending],self.dialog.state))
        self.new_button.setEnabled(not self.request_id);self.import_button.setEnabled(not self.request_id)
        self.new_button.setVisible(bool(editing));self.new_button.setText('返回追加')

    def stored_ids(self):
        owned={job for row in self.rows for job in row.get('queue_ids',[])}
        return [r['id'] for r in self.dialog.state.get('jobs',[]) if r['id'] in owned and r['status']=='collected']

    def discard(self):
        if self.request_id:return
        if self.pending:
            self.pending['release_after_ack']=False;self.pending['discard_after_lookup']=True
        for row in self.pending_rows():row['discarded']=True;self.render_row(self.rows.index(row))
        self.manual.collection.discard_current();self.changed();self.flush()
        self.status.setText('待提交清单已放弃；保留文本与草稿，已执行任务不受影响。')

    def enqueue(self):
        if self.request_id:return
        if not self.loaded and getattr(self.dialog.plugin,'service',None) is not None:
            self.status.setText('正在读取上次填写的清单，请稍后再提交。');return
        if self.pending:
            self.request_id=self.pending['request_id'];self.set_busy(True)
            if self.pending.get('phase')=='release':
                self.dialog.send('submit_selected',source='batch',request_id=self.request_id,ids=self.pending['release_ids'])
            else:self.dialog.send('submission_lookup',request_id=self.request_id,source='batch')
            return
        pending=self.pending_rows()
        stored=self.stored_ids()
        if not pending:
            if stored and not self.save_only.isChecked():
                self.request_id=uuid.uuid4().hex
                self.pending={'request_id':self.request_id,'phase':'release','snapshots':[{**core(r),'id':r['id']} for r in self.rows],
                              'release_ids':stored,'release_after_ack':True}
                self.flush();self.set_busy(True)
                self.dialog.send('submit_selected',source='batch',request_id=self.request_id,ids=stored)
            return
        if any(not r['prompt'].strip() for r in pending):self.status.setText('清单中有空提示词，请先填写或删除。');return
        items=[{'target':r['target'],'images':[r['image']],'prompt':r['prompt'],**r['options']} for r in pending]
        self.request_id=uuid.uuid4().hex
        submitting=[r for r in self.rows if r in pending or (not self.save_only.isChecked() and set(r.get('queue_ids',[])) & set(stored))]
        self.pending={'request_id':self.request_id,'snapshots':[{**core(r),'id':r['id']} for r in submitting],
                      'release_ids':stored,'release_after_ack':not self.save_only.isChecked()}
        self.dialog.batch_request={'source':'batch','request_id':self.request_id,'items':items,'collect_only':self.save_only.isChecked(),
            'compose_rows':[r['id'] for r in pending],
            'table_release_ids':stored if not self.save_only.isChecked() else [],
            'start_after_save':not self.save_only.isChecked(),'batch_limit':self.manual.batch_limit.value(),'batch_label':self.batch_label.text().strip()}
        self.manual.flush_draft();self.dialog.save_preferences();self.flush();self.set_busy(True)
        self.status.setText('正在提交表格任务；后台保存并安排生成，确认成功后清空对应表格行。')
        self.dialog.send('enqueue',**self.dialog.batch_request)

    def set_busy(self,busy):
        self.manual.setEnabled(not busy);self.side.setEnabled(not busy)
        self.manual.picker.setEnabled(not busy);self.manual.picker.task_selector.setEnabled(not busy);self.update_controls()

    def acknowledged(self,result):
        if not self.request_id or result.get('request_id')!=self.request_id:return
        pending=self.pending
        for snapshot in pending['snapshots']:
            row=self.entry(snapshot['id'])
            if row:
                row['saved_signature']=signature(snapshot)
                if snapshot['id'] in result.get('row_receipts',{}):
                    row['queue_ids']=list(result['row_receipts'][snapshot['id']])
        if pending.get('discard_after_lookup'):
            self.dialog.send('discard_collected',ids=list(dict.fromkeys(pending.get('release_ids',[])+result.get('ids',[]))))
        elif pending.get('release_after_ack') and result.get('collected',False):
            # Old 0.6.10 pending saves remain recoverable, without another user click.
            ids=list(dict.fromkeys(pending.get('release_ids',[])+result.get('ids',[])))
            if ids:
                pending['phase']='release';pending['release_ids']=ids;self.flush()
                self.status.setText('记录已保存，正在确认放行生成。')
                self.dialog.send('submit_selected',source='batch',request_id=self.request_id,ids=ids);return
        released=pending.get('release_after_ack') and not pending.get('discard_after_lookup')
        if released:
            snapshots=pending['snapshots']
            completed={r['id']:signature(r) for r in snapshots}
            self.manual.mark_submission(snapshots,'queue')
            self.manual.last_submitted_ids=set(result.get('ids',()))
            self.loading=True
            try:
                self.rows=[r for r in self.rows if not (completed.get(r['id'])==signature(r) or
                    set(r.get('queue_ids',[])) & set(pending.get('release_ids',[])) and r.get('saved_signature')==signature(r))]
                self.compose.setRowCount(0);self.editing=''
                for index in range(len(self.rows)):self.render_row(index)
            finally:self.loading=False
        self.pending=None;self.request_id='';self.set_busy(False);self.changed();self.flush()
        count=max(result['count'],len(set(result.get('ids',[])+pending.get('release_ids',[]))))
        self.status.setText((f'已提交{count}条任务，已清理对应表格行；'+('当前队列处于安全暂停，点击队列的“开始／继续等待项”。' if result.get('held') else '后台继续生成。'))
            if released else f'已保存{result["count"]}条记录，暂不生成；取消“只保存记录”后点击“提交表格任务”。')
        self.manual.status.setText(self.status.text());self.manual.update_prompt_submission()
        for index in range(len(self.rows)):self.render_row(index)

    def failed(self,result):
        if not self.request_id or result.get('request_id')!=self.request_id:return
        self.request_id='';self.set_busy(False);self.status.setText(result['error'])
        if result.get('code')=='FLOW_SUBMISSION_NOT_FOUND':self.pending=None;self.changed();self.flush();return
        if result.get('duplicate_ids'):
            if self.dialog.confirm_duplicate(result['duplicate_ids']):
                self.request_id=self.pending['request_id'];self.set_busy(True);self.dialog.send('enqueue',**self.dialog.batch_request,allow_duplicates=True);return
            self.pending=None;self.changed();self.flush()

    def flush(self):
        if self.loading:return
        if not self.loaded and not self.dirty and not self.rows and not self.pending:return
        self.timer.stop()
        values=[]
        for row in self.rows:
            key=row_key(row['id']);value={'kind':'flow_batch_row_v1',**copy.deepcopy(row)}
            if self.written.get(key)!=value:self.dialog.send('draft_save',key=key,value=value);self.written[key]=copy.deepcopy(value)
            values.append(key)
        # Large pending prompt snapshots live in their own bounded row drafts.
        pending=copy.deepcopy(self.pending)
        if pending:
            pending['snapshots']=[{'id':r['id']} for r in self.pending['snapshots']]
            for snapshot in self.pending['snapshots']:
                key=hashlib.sha256(('flow-batch-pending:'+snapshot['id']+pending['request_id']).encode()).hexdigest()
                self.dialog.send('draft_save',key=key,value={'kind':'flow_batch_pending_v1',**snapshot});pending['snapshots'][self.pending['snapshots'].index(snapshot)]['key']=key
        index={'kind':'flow_batch_index_v1','rows':values,'selected':self.editing,'batch_label':self.batch_label.text(),
               'batch_limit':self.manual.batch_limit.value(),'save_only':self.save_only.isChecked(),'pending':pending}
        if not self.loaded and getattr(self.dialog.plugin,'service',None) is not None:return
        if self.written.get(INDEX)!=index:self.dialog.send('draft_save',key=INDEX,value=index);self.written[INDEX]=copy.deepcopy(index)

    def apply_state(self,state):
        drafts=state.get('drafts',{});index=drafts.get(INDEX)
        if not self.loaded and 'drafts' in state:
            self.loaded=True
            if index and not self.dirty and not self.rows:
                self.loading=True
                try:
                    for key in index.get('rows',[]):
                        row=drafts.get(key,{})
                        if row.get('kind')=='flow_batch_row_v1' and all(k in row for k in ('id','target','image','prompt','options')):
                            self.add_entry(row['target'],row['image'],row['prompt'],row['options'],entry=row);self.written[key]=copy.deepcopy(row)
                    self.batch_label.setText(index.get('batch_label',''));self.manual.batch_limit.setValue(index.get('batch_limit',0));self.save_only.setChecked(index.get('save_only',False))
                    pending=copy.deepcopy(index.get('pending'))
                    if pending:
                        pending['snapshots']=[drafts[r['key']] for r in pending.get('snapshots',[]) if r.get('key') in drafts]
                        self.pending=pending;self.status.setText('上次保存回执未确认；点击“核对上次添加结果”，不会重复生成。')
                    self.written[INDEX]=copy.deepcopy(index)
                finally:self.loading=False
                selected=next((i for i,r in enumerate(self.rows) if r['id']==index.get('selected')),0)
                if self.rows:self.compose.setCurrentCell(selected,0)
            elif index and self.rows:
                # A slow first snapshot must not replace the new entry being
                # typed, nor let an empty/partial local index erase older drafts.
                current={row['id'] for row in self.rows};restored=[]
                for key in index.get('rows',[]):
                    row=drafts.get(key,{})
                    if row.get('kind')=='flow_batch_row_v1' and all(k in row for k in ('id','target','image','prompt','options')) and row['id'] not in current:
                        restored.append(copy.deepcopy(row));self.written[key]=copy.deepcopy(row)
                if restored:
                    self.loading=True
                    try:
                        self.rows=restored+self.rows;self.compose.setRowCount(0)
                        for n in range(len(self.rows)):self.render_row(n)
                    finally:self.loading=False
                    self.status.setText(f'已合并上次清单{len(restored)}条；当前正在填写的内容保留。')
                if index.get('pending') and not self.pending:
                    self.pending=copy.deepcopy(index['pending'])
                    self.pending['snapshots']=[drafts[r['key']] for r in self.pending.get('snapshots',[]) if r.get('key') in drafts]
                self.changed()
            if self.pending:QtCore.QTimer.singleShot(0,self.enqueue)
        self.update_controls()
