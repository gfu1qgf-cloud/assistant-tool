"""Group chat UI; one member request at a time with a bounded schedule."""
import copy
import functools
import json
from uuid import uuid4
from qt_compat import QtCore, QtGui, QtWidgets
from model.AIProviderClient import PROVIDERS
from model.AIProviderKeys import pin_provider_config
from model.OfflineMode import is_offline
from .ui import AIClientDialog, StreamBuffer, safe_ui
from .group_service import default_group, validate_group, preflight_group, run_member
from .version import APP_VERSION


class GroupSettings(QtWidgets.QDialog):
    def __init__(self, config, parent):
        super().__init__(parent)
        self.setWindowTitle('群聊成员与讨论上限')
        self.resize(840,640)
        self.value = copy.deepcopy(config)
        self.index = None
        self.loading = False
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel('每轮按列表顺序，每位成员发言一次。服务商密钥沿用连接设置；修改这些设置不会调用模型。',self)
        note.setWordWrap(True); layout.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('每次讨论最多',self))
        self.round_limit = QtWidgets.QSpinBox(self); self.round_limit.setRange(1,5)
        self.round_limit.setSuffix(' 轮'); self.round_limit.setValue(self.value.get('round_limit',2))
        row.addWidget(self.round_limit)
        self.cost = QtWidgets.QLabel(self); row.addWidget(self.cost,1); layout.addLayout(row)
        body = QtWidgets.QHBoxLayout(); layout.addLayout(body,1)
        left = QtWidgets.QVBoxLayout(); body.addLayout(left)
        self.members = QtWidgets.QListWidget(self); self.members.setFixedWidth(175); left.addWidget(self.members,1)
        buttons = QtWidgets.QHBoxLayout(); left.addLayout(buttons)
        add = QtWidgets.QPushButton('添加',self); remove = QtWidgets.QPushButton('移除',self)
        buttons.addWidget(add); buttons.addWidget(remove)
        order = QtWidgets.QHBoxLayout(); left.addLayout(order)
        up = QtWidgets.QPushButton('上移',self); down = QtWidgets.QPushButton('下移',self)
        order.addWidget(up); order.addWidget(down)
        form = QtWidgets.QFormLayout(); body.addLayout(form,1)
        self.name = QtWidgets.QLineEdit(self); self.name.setMaxLength(24)
        self.provider = QtWidgets.QComboBox(self)
        for key,title in PROVIDERS.items(): self.provider.addItem(title,key)
        self.model = QtWidgets.QComboBox(self); self.model.setEditable(True)
        self.model.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.model.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model.setMinimumContentsLength(14)
        self.model.lineEdit().setPlaceholderText('填写具体模型ID，或从已加载列表选择')
        self.role = QtWidgets.QPlainTextEdit(self); self.role.setMinimumHeight(180)
        form.addRow('昵称',self.name); form.addRow('服务商',self.provider)
        form.addRow('模型',self.model); form.addRow('角色要求',self.role)
        bulk = QtWidgets.QPushButton('所有成员使用群聊窗口顶部的服务商与模型',self)
        form.addRow(bulk)
        self.error = QtWidgets.QLabel(self); self.error.setWordWrap(True)
        self.error.setTextFormat(QtCore.Qt.TextFormat.PlainText); layout.addWidget(self.error)
        footer = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel,self)
        footer.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText('保存群聊设置')
        footer.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        layout.addWidget(footer)
        footer.accepted.connect(self.accept_config); footer.rejected.connect(self.reject)
        self.members.currentRowChanged.connect(self.select)
        self.provider.currentIndexChanged.connect(self.fill_models)
        self.round_limit.valueChanged.connect(self.update_cost)
        add.clicked.connect(self.add_member); remove.clicked.connect(self.remove_member)
        up.clicked.connect(lambda:self.move(-1)); down.clicked.connect(lambda:self.move(1))
        bulk.clicked.connect(self.use_current)
        self.reload(0)

    def save_row(self):
        if self.index is not None and not self.loading:
            self.value['members'][self.index].update(name=self.name.text().strip(),role=self.role.toPlainText().strip(),
                provider=self.provider.currentData(),model=self.model.currentText().strip())
            self.members.item(self.index).setText(self.name.text().strip() or '未命名成员')

    def fill_models(self,*_args):
        selected = self.model.currentText() if self.loading else ''
        self.model.clear()
        for row in self.parent().catalogs.get(self.provider.currentData(),[]):
            self.model.addItem(row['id'])
        self.model.setCurrentText(selected)

    def select(self,index):
        self.save_row()
        self.index=index if index>=0 else None
        if self.index is None:return
        member=self.value['members'][index]
        self.loading=True
        self.name.setText(member['name'])
        self.provider.setCurrentIndex(max(0,self.provider.findData(member['provider'])))
        self.fill_models(); self.model.setCurrentText(member['model'])
        self.role.setPlainText(member['role']); self.loading=False

    def reload(self,index):
        self.members.blockSignals(True); self.members.clear()
        for member in self.value['members']: self.members.addItem(member['name'] or '未命名成员')
        self.index=None
        self.members.setCurrentRow(index); self.members.blockSignals(False)
        self.select(index); self.update_cost()

    def update_cost(self,*_args):
        self.cost.setText(f"{len(self.value['members'])}位成员 × {self.round_limit.value()}轮 = 最多{len(self.value['members'])*self.round_limit.value()}次生成")

    def add_member(self):
        self.save_row()
        if len(self.value['members'])>=6:
            self.error.setText('最多6位成员。');return
        self.value['members'].append(dict(id=uuid4().hex,name='成员'+str(len(self.value['members'])+1),
            role='提出自己的观点，并回应已有讨论。',provider=self.parent().provider.currentData(),model=self.parent().model_id()))
        self.reload(len(self.value['members'])-1)

    def remove_member(self):
        if len(self.value['members'])<=2:
            self.error.setText('至少保留2位成员。');return
        index=self.members.currentRow(); self.value['members'].pop(index); self.reload(max(0,index-1))

    def move(self,offset):
        self.save_row(); index=self.members.currentRow(); target=index+offset
        if 0<=target<len(self.value['members']):
            self.value['members'][index],self.value['members'][target]=self.value['members'][target],self.value['members'][index]
            self.reload(target)

    def use_current(self):
        self.save_row()
        for member in self.value['members']:
            member.update(provider=self.parent().provider.currentData(),model=self.parent().model_id())
        self.reload(max(0,self.members.currentRow()))

    def accept_config(self):
        self.save_row(); self.value['round_limit']=self.round_limit.value()
        try:self.value=validate_group(self.value)
        except ValueError as error:self.error.setText(str(error));return
        self.accept()


class GroupChatDialog(AIClientDialog):
    def __init__(self,context,store,parent=None):
        self.is_group=True
        seed_provider=parent.provider.currentData() if isinstance(parent,AIClientDialog) else 'gemini'
        seed_model=parent.model_id() if isinstance(parent,AIClientDialog) else ''
        self.group_config=default_group(seed_provider,seed_model)
        self._seed=(seed_provider,seed_model)
        self._group_plan=None; self._queued_interjection=None; self._group_consent_signature=None
        super().__init__(context,store,parent)
        self.setWindowTitle(f'AI群聊 {APP_VERSION} · '+('无痕' if self.is_private else 'LZX'))
        self.findChild(QtWidgets.QLabel,'sidebarBrand').setText('无痕AI群聊' if self.is_private else 'AI 群聊')
        self.new_button.setText('＋  新建群聊')
        self.group_button.hide()
        if self._new_group_config:
            self.provider.setCurrentIndex(max(0,self.provider.findData(seed_provider)))
            self.model.setCurrentText(seed_model)
        self.request_mode.setCurrentIndex(0); self.request_mode.setEnabled(False)
        self.request_mode.setToolTip('群聊依次调用成员，后面的成员需要前面的发言，因此使用即时请求。')
        self.rounds.setToolTip('携带此前多少次用户提问及讨论；0不带旧话题，但本次讨论的成员发言仍然共享。')
        self.retry_button.setText('重试本轮讨论')
        model_note=QtWidgets.QLabel('成员可分别选模型；顶部模型可在“成员 / 轮数”中一键应用给全部成员。',self.header)
        model_note.setWordWrap(True);model_note.setObjectName('muted')
        self.header.layout().insertWidget(1,model_note)
        panel=QtWidgets.QWidget(self)
        tools=QtWidgets.QHBoxLayout(panel); tools.setContentsMargins(20,10,20,10)
        self.group_summary=QtWidgets.QLabel(panel); self.group_summary.setWordWrap(True)
        self.group_summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        tools.addWidget(self.group_summary,1)
        self.group_settings_button=QtWidgets.QPushButton('成员 / 轮数',panel)
        self.group_pause_button=QtWidgets.QPushButton('我来插话 / 暂停',panel)
        self.group_settings_button.clicked.connect(self.configure_group)
        self.group_pause_button.clicked.connect(self.pause_group)
        tools.addWidget(self.group_settings_button);tools.addWidget(self.group_pause_button)
        self.header.layout().addWidget(panel)
        self.group_timer=QtCore.QTimer(self); self.group_timer.setSingleShot(True)
        self.group_timer.timeout.connect(self.next_member)
        self.maximum.valueChanged.connect(self.update_group_summary)
        self.input.setPlaceholderText('提出问题，让成员轮流讨论；讨论中也可以输入补充并发送。')
        self.update_group_summary()

    def conversation_options_loaded(self,options):
        raw=options.get('group_chat')
        self._new_group_config=raw is None
        self.group_config=copy.deepcopy(raw) if isinstance(raw,dict) else default_group(*self._seed)
        if raw is None:options.update(max_tokens=1024,context_chars=24000,history_images=False)
        for row in self.store.messages(self.current_id):
            if row['role']=='user' and row['status']=='interrupted' and row['metadata'].get('group_discussion'):
                self.store.finish_group(self.current_id,row['id'],'interrupted','上次讨论中断，未自动续跑。')
        self.update_group_summary()

    def options(self):
        return {**super().options(),'group_chat':copy.deepcopy(self.group_config),'request_mode':'instant'}

    def update_group_summary(self,*_args):
        if not hasattr(self,'group_summary'):return
        members=self.group_config.get('members',[]); rounds=self.group_config.get('round_limit',2)
        self.group_summary.setText(' → '.join(m['name'] for m in members)+f'\n每轮每人发言一次 · 最多{rounds}轮 / {rounds*len(members)}次生成；每次最多{self.maximum.value()}输出Token。')
        self.group_pause_button.setEnabled(self._group_plan is not None)
        self.group_summary.setToolTip('\n'.join(m['name']+'：'+m['provider']+' / '+m['model'] for m in members))

    def update_request_hint(self):
        if not hasattr(self,'request_hint'):return
        count=len(self.group_config.get('members',[]))*self.group_config.get('round_limit',2)
        limit=self.context_chars.value()
        self.request_hint.setText(f'每次发言最多{count}次生成 · 每次输出≤{self.maximum.value()} Token · 当前输入{len(self.input.toPlainText())}字符 · '+
            (f'每次请求文字≤{limit}字符' if limit else '请求文字未设总字符上限'))

    def request_mode_changed(self,*_args):
        super().request_mode_changed(*_args)
        self.send_button.setText('发送 / 开始讨论')

    def sync_capabilities(self):
        result=super().sync_capabilities()
        self.request_mode.model().item(1).setEnabled(False)
        self.request_mode.setCurrentIndex(0)
        return result

    @safe_ui
    def configure_group(self,_checked=False):
        if self._group_plan or self.handle:
            self.status.setText('请先暂停讨论，再修改群成员。');return
        dialog=GroupSettings(self.group_config,self)
        if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted:
            self.group_config=dialog.value;self.save_draft();self.update_group_summary()
            self.update_request_hint()

    @safe_ui
    def send(self,_checked=False,retry_id=None,regenerate_id=None,edit_id=None,edited_text=None,interjection=None):
        if self._group_plan:
            if edit_id is not None or retry_id is not None:
                self.status.setText('请先暂停讨论再编辑或重试。');return
            if self._queued_interjection:
                self.status.setText('已有补充消息排队；当前成员发言结束后会发送。');return
            text=self.input.toPlainText().strip()
            if not text and not self.pending_images:
                self.pause_group();return
            self._queued_interjection=(text,copy.deepcopy(self.pending_images))
            self._group_plan['pause']=True
            self.status.setText('补充消息已排队，当前成员结束后会发送；点击“我来插话 / 暂停”可取消排队并保留草稿。')
            return
        if self.handle:return
        if is_offline():raise ValueError('离线模式下不能开始群聊。')
        config=validate_group(self.group_config)
        source_id=edit_id or retry_id
        if source_id is not None:
            source=self.store.editable_prompt(self.current_id,source_id)
            text=edited_text.strip() if edit_id is not None and isinstance(edited_text,str) else source['text']
            images=source['images']
        else:
            text,images=interjection if interjection is not None else (self.input.toPlainText().strip(),copy.deepcopy(self.pending_images))
        if not text and not images:self.status.setText('先输入一个讨论话题。');return
        if len(text)>128000:raise ValueError('问题超过128000字符，请缩短；没有截断原文。')
        options=self.options()
        preflight_group(text,config,options)
        # Pin every credential before the first member can incur generation cost.
        connections={provider:pin_provider_config(self.connection_snapshot(),provider) for provider in {m['provider'] for m in config['members']}}
        signature=json.dumps(config,ensure_ascii=False,sort_keys=True)
        if not self.consent or (self._group_consent_signature is not None and self._group_consent_signature!=signature):
            box=QtWidgets.QMessageBox(self);box.setWindowTitle('开始AI群聊')
            box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            box.setText('问题、图片与相关群聊历史会发给以下成员的服务商：\n'+ '\n'.join(m['name']+'：'+PROVIDERS[m['provider']]+' / '+m['model'] for m in config['members'])+
                f"\n\n本次最多{config['round_limit']*len(config['members'])}次生成，每次最多{options['max_tokens']}输出Token；API可能计费。是否开始？")
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No)
            box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
            if box.exec()!=QtWidgets.QMessageBox.StandardButton.Yes:return
            self.consent=True
        self._group_consent_signature=signature
        self.store.context_preview(self.current_id,text,images,options,before_prompt_id=source_id)
        if self.current_id is None:self.current_id=self.store.create(self.provider.currentData(),self.model_id(),options)
        self.save_draft()
        prompt=self.store.begin_request(self.current_id,text,images,self.provider.currentData(),self.model_id(),retry_id=retry_id,edit_id=edit_id)
        self.store.begin_group(self.current_id,prompt,config)
        self._group_plan=dict(conversation=self.current_id,prompt_id=prompt,config=config,connections=connections,options=options,
                             step=0,pause=False,failed=False)
        if edit_id is None and retry_id is None and (interjection is None or (self.input.toPlainText().strip()==text and self.pending_images==images)):
            self.loading=True;self.input.clear();self.pending_images=[];self.render_attachments();self.loading=False
        self.reload_history();self.render_transcript();self.update_group_summary()
        self.group_timer.start(0)
        return prompt

    @safe_ui
    def next_member(self):
        try:self._advance_member()
        except Exception as error:
            if self._group_plan:self._group_plan.update(failed=True,error=str(error))
            self.finish_discussion('failed')
            self.status.setText('讨论未继续：'+str(error))

    def _advance_member(self):
        plan=self._group_plan
        if not plan:return
        total=plan['config']['round_limit']*len(plan['config']['members'])
        if plan['failed'] or plan['pause'] or plan['step']>=total or not self.isVisible():
            state=('paused' if plan.get('canceled') else 'failed') if plan['failed'] else ('complete' if plan['step']>=total else 'paused')
            self.finish_discussion(state);return
        member=plan['config']['members'][plan['step']%len(plan['config']['members'])]
        round_number=plan['step']//len(plan['config']['members'])+1
        rows=self.store.messages(plan['conversation'])
        self.stream=StreamBuffer() if plan['options'].get('stream',True) else None
        self.stream_displayed=0
        payload=dict(conversation=plan['conversation'],prompt_id=plan['prompt_id'],parent_id=rows[-1]['id'],step=plan['step'],
            member=copy.deepcopy(member),round_number=round_number,options=plan['options'],config=plan['connections'][member['provider']])
        self.submit('AI群聊 · '+member['name'],functools.partial(run_member,store=self.store,client=self.client,stream=self.stream),payload,
            dict(type='group_member',conversation=plan['conversation'],prompt_id=plan['prompt_id'],provider=member['provider'],model=member['model'],
                 group_member=member['name'],group_round=round_number))
        self.render_transcript()
        self.status.setText(f'第{round_number}轮 · {member["name"]}发言中 · 第{plan["step"]+1}/{total}次生成')
        if self.stream:self.stream_timer.start()

    def set_busy(self,busy):
        super().set_busy(busy)
        discussing=self._group_plan is not None
        self.history.setEnabled(not discussing)
        self.new_button.setEnabled(not discussing)
        self.delete_button.setEnabled(not busy and not discussing)
        self.retry_button.setEnabled(not busy and not discussing and not is_offline())
        self.transcript.set_busy(busy or discussing)
        self.send_button.setEnabled(not is_offline() and (not busy or discussing))
        if hasattr(self,'group_settings_button'):self.group_settings_button.setEnabled(not busy and not discussing)

    def completed(self,handle,output):
        if handle is self.handle and self.operation.get('type')=='group_member':
            if self._group_plan:self._group_plan['step']+=1
        super().completed(handle,output)

    def failed(self,handle,message):
        if handle is self.handle and self.operation.get('type')=='group_member' and self._group_plan:
            self._group_plan.update(failed=True,error=message,canceled='取消' in message)
        super().failed(handle,message)
        if not self._privacy_closing:self.render_transcript()

    def finished(self,handle):
        group = handle is self.handle and self.operation and self.operation.get('type')=='group_member'
        super().finished(handle)
        if self._privacy_closing:
            self._group_plan=None;self._queued_interjection=None;return
        if group and self._group_plan:self.group_timer.start(0)

    def finish_discussion(self,state):
        plan=self._group_plan
        if not plan:return
        self.group_timer.stop()
        self.store.finish_group(plan['conversation'],plan['prompt_id'],state,plan.get('error',''))
        queued=self._queued_interjection
        self._group_plan=None;self._queued_interjection=None
        self.set_busy(False);self.render_transcript();self.update_group_summary();self.update_request_hint()
        self.status.setText('讨论已达到本次上限。你可以继续发言。' if state=='complete' else ('讨论遇到错误，已停止；未自动重试或继续收费调用。' if state=='failed' else '讨论已暂停。你可以修改问题或继续发言。'))
        if queued and state!='failed' and self.isVisible():self.send(interjection=queued)

    def pause_group(self,_checked=False):
        self._queued_interjection=None
        if self._group_plan:
            self._group_plan['pause']=True
            self.status.setText('当前成员发言结束后暂停；草稿保留。')
            if not self.handle:self.finish_discussion('paused')
        self.input.setFocus()

    def stop_request(self,_checked=False):
        self._queued_interjection=None
        if self._group_plan:self._group_plan['pause']=True
        super().stop_request(_checked)

    def regenerate_answer(self,assistant_id):
        if self._group_plan:self.status.setText('请先暂停当前讨论。');return
        row=next((r for r in self.store.messages(self.current_id,all_branches=True) if r['id']==assistant_id),None)
        root_id=(row['metadata'].get('group_run') if row else None)
        if root_id:
            root=self.store.editable_prompt(self.current_id,root_id)
            return self.send(edit_id=root_id,edited_text=root['text'])
        return super().regenerate_answer(assistant_id)

    def retry_request(self,_checked=False):
        if self._group_plan:self.status.setText('请先暂停当前讨论。');return
        rows=self.store.messages(self.current_id) if self.current_id else []
        if rows and rows[-1]['role']=='assistant' and rows[-1]['metadata'].get('group_run'):
            return self.regenerate_answer(rows[-1]['id'])
        return super().retry_request(_checked)

    def new_conversation(self,_checked=False):
        if self._group_plan:self.status.setText('请先暂停讨论。');return
        return super().new_conversation(_checked)

    def select_conversation(self,item,_previous=None):
        if self._group_plan and item and item.data(QtCore.Qt.ItemDataRole.UserRole)!=self.current_id:
            self.status.setText('请先暂停讨论，再切换群聊。');return
        return super().select_conversation(item,_previous)

    def switch_branch(self,branch_id):
        if self._group_plan:self.status.setText('请先暂停讨论，再切换分支。');return
        return super().switch_branch(branch_id)

    def delete_conversation(self,_checked=False):
        if self._group_plan:self.status.setText('请先暂停讨论，再彻底删除。');return
        return super().delete_conversation(_checked)

    def closeEvent(self,event):
        if hasattr(self,'group_timer'):self.group_timer.stop()
        self._queued_interjection=None
        if self._group_plan:
            self._group_plan['pause']=True
            if not self.handle:self.finish_discussion('paused')
        super().closeEvent(event)
