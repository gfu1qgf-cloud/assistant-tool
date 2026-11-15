"""Explicit-Profile, extension-free Firefox container creation panel."""
from pathlib import Path
from uuid import uuid4
from qt_compat import QtCore,QtGui,QtWidgets
from model.BackgroundTasks import BackgroundTaskManager,directory_resource
from model import FirefoxContainers as service

COLOR_LABELS={'blue':'蓝色','turquoise':'青绿色','cyan':'青色','green':'绿色','yellow':'黄色',
              'orange':'橙色','red':'红色','pink':'粉色','purple':'紫色','violet':'紫罗兰','gray':'灰色','toolbar':'灰色'}


class FirefoxContainersPanel(QtWidgets.QWidget):
    def __init__(self,parent=None,*,storage=None,task_manager=None):
        super().__init__(parent)
        self._own_manager=task_manager is None
        self._manager=task_manager or BackgroundTaskManager(parent=QtWidgets.QApplication.instance())
        self._storage=storage;self._extra=[];self._handle=None;self._loaded=False
        self._profiles=[];self._state=None;self._next_inspect=None;self._job='';self._receipt=None
        self._key=uuid4().hex
        layout=QtWidgets.QVBoxLayout(self)
        note=QtWidgets.QLabel('无扩展批量创建 · 先选中一个Firefox Profile。只修改它的容器定义，不改登录数据。写入时目标Profile必须退出，其他Profile可以继续使用。',self)
        note.setWordWrap(True);layout.addWidget(note)
        toolbar=QtWidgets.QHBoxLayout()
        self.refresh_button=QtWidgets.QPushButton('刷新Profile列表',self)
        self.add_button=QtWidgets.QPushButton('添加Profile目录…',self)
        self.folder_button=QtWidgets.QPushButton('打开选中目录',self)
        for button in (self.refresh_button,self.add_button,self.folder_button):toolbar.addWidget(button)
        toolbar.addStretch(1);layout.addLayout(toolbar)
        self.refresh_button.clicked.connect(self.refresh)
        self.add_button.clicked.connect(self.add_directory)
        self.folder_button.clicked.connect(self.open_directory)
        self.profiles=QtWidgets.QTableWidget(0,4,self)
        self.profiles.setHorizontalHeaderLabels(['Profile名称','完整路径','容器数','状态'])
        self.profiles.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.profiles.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.profiles.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.profiles.horizontalHeader().setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.profiles.horizontalHeader().setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.profiles.horizontalHeader().setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.profiles.setMinimumHeight(150)
        self.profiles.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.profiles,2)
        self.target=QtWidgets.QLineEdit(self);self.target.setReadOnly(True)
        self.target.setPlaceholderText('尚未选择Profile；不会自动修改默认Profile')
        layout.addWidget(self.target)
        options=QtWidgets.QGridLayout()
        self.mode=QtWidgets.QComboBox(self);self.mode.addItems(['顺序编号','逐行填写名称'])
        self.prefix=QtWidgets.QLineEdit('账号',self)
        self.start=QtWidgets.QSpinBox(self);self.start.setRange(0,100000000);self.start.setValue(1)
        self.count=QtWidgets.QSpinBox(self);self.count.setRange(1,1000);self.count.setValue(10)
        self.digits=QtWidgets.QSpinBox(self);self.digits.setRange(1,8);self.digits.setValue(2)
        for column,(label,widget) in enumerate((('命名方式',self.mode),('前缀',self.prefix),('起始编号',self.start),('数量',self.count),('编号位数',self.digits))):
            options.addWidget(QtWidgets.QLabel(label,self),0,column);options.addWidget(widget,1,column)
        layout.addLayout(options)
        self.names=QtWidgets.QPlainTextEdit(self);self.names.setPlaceholderText('一行一个名称；重名会略过，不会覆盖旧容器')
        self.names.setMaximumHeight(85);self.names.hide();layout.addWidget(self.names)
        row=QtWidgets.QHBoxLayout()
        self.color=QtWidgets.QComboBox(self);self.color.addItem('自动轮换颜色','auto')
        row.addWidget(QtWidgets.QLabel('颜色',self));row.addWidget(self.color)
        self.icon=QtWidgets.QComboBox(self)
        for title,key in (('公文包','briefcase'),('指纹','fingerprint'),('圆形','circle')):self.icon.addItem(title,key)
        row.addWidget(QtWidgets.QLabel('图标',self));row.addWidget(self.icon);row.addStretch(1)
        self.create_button=QtWidgets.QPushButton('先选择Profile',self);self.create_button.setEnabled(False)
        row.addWidget(self.create_button);layout.addLayout(row)
        self.preview=QtWidgets.QTableWidget(0,4,self)
        self.preview.setHorizontalHeaderLabels(['状态','容器名称','内部编号','颜色'])
        self.preview.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.horizontalHeader().setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.preview,3)
        self.status=QtWidgets.QLabel('打开此页后读取Profile列表；不会自动创建容器。',self);self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.result=QtWidgets.QLabel(self);self.result.setWordWrap(True);layout.addWidget(self.result)
        self.create_button.clicked.connect(self.create)
        for widget,signal in ((self.mode,'currentIndexChanged'),(self.prefix,'textChanged'),(self.start,'valueChanged'),
                              (self.count,'valueChanged'),(self.digits,'valueChanged'),(self.names,'textChanged'),
                              (self.color,'currentIndexChanged'),(self.icon,'currentIndexChanged')):
            getattr(widget,signal).connect(self._preview)
        if storage is not None:
            try:
                file=storage.file('firefox/custom-profiles.json')
                if file.exists():
                    value=file.read_json()
                    if not isinstance(value,list) or len(value)>2000 or any(not isinstance(path,str) or not Path(path).is_absolute() for path in value):
                        raise ValueError('自定义Firefox Profile清单格式无效。')
                    self._extra=value
            except (OSError,ValueError) as error:self.result.setText('自定义目录清单未加载：'+str(error))

    def showEvent(self,event):
        super().showEvent(event)
        if not self._loaded and not self.is_busy():self.refresh()

    def is_busy(self):return self._handle is not None and self._handle.isRunning()

    def dispose(self):
        if self.is_busy():return False
        if self._own_manager:self._manager.shutdown()
        return True

    def _selected(self):
        rows=self.profiles.selectionModel().selectedRows()
        return self._profiles[rows[0].row()] if len(rows)==1 else None

    def _enable(self,enabled):
        for widget in (self.profiles,self.refresh_button,self.add_button,self.folder_button,self.mode,self.prefix,self.start,
                       self.count,self.digits,self.names,self.color,self.icon):widget.setEnabled(enabled)
        self.create_button.setEnabled(False)
        if enabled:self._preview()

    def _submit(self,payload):
        if self.is_busy():return
        self._job=payload['action'];self._receipt=None;self._enable(False)
        self.status.setText({'scan':'读取Profile列表中…','inspect':'读取所选Profile容器中…','create':'锁定所选Profile、备份并创建中…'}[self._job])
        try:
            self._handle=self._manager.submit(owner='chrome_launcher',title='Firefox容器 · '+self._job,
                runner=service.run_action,payload=payload,kind='io',
                resources=[directory_resource(payload['path'])] if payload.get('path') else [],
                dedup_key='firefox-containers:'+self._key,retryable=False)
            self._handle.completed.connect(self._completed);self._handle.failed.connect(self._failed)
            self._handle.finished.connect(self._finished)
        except (OSError,ValueError,RuntimeError) as error:self.status.setText(str(error));self._enable(True)

    def refresh(self):
        if self.is_busy():return
        self._loaded=True;self._state=None
        self._submit({'action':'scan','extra':list(self._extra)})

    def add_directory(self):
        path=QtWidgets.QFileDialog.getExistingDirectory(self,'选择Firefox Profile目录')
        if not path:return
        if not any((Path(path)/name).is_file() for name in ('prefs.js','compatibility.ini','containers.json')):
            QtWidgets.QMessageBox.warning(self,'目录不符','请选择实际Firefox Profile目录，不是Firefox安装目录。');return
        if not any(service.profile_key(path)==service.profile_key(existing) for existing in self._extra):
            updated=[*self._extra,path]
            try:
                if self._storage is not None:self._storage.file('firefox/custom-profiles.json').write_json(updated,indent=2)
                self._extra=updated
            except (OSError,ValueError) as error:QtWidgets.QMessageBox.warning(self,'未保存目录',str(error));return
        self.refresh()

    def open_directory(self):
        selected=self._selected()
        if selected:QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(selected['path']))

    def _selection_changed(self):
        if self.is_busy():return
        self._state=None;self._preview();selected=self._selected()
        self.target.setText(selected['name']+'  |  '+selected['path'] if selected else '')
        self.target.setToolTip(self.target.text())
        if selected:self._submit({'action':'inspect','path':selected['path']})

    def _names(self):
        if self.mode.currentIndex()==1:return [line.strip() for line in self.names.toPlainText().splitlines() if line.strip()]
        return service.numbered_names(self.prefix.text(),self.start.value(),self.count.value(),self.digits.value())

    def _preview(self,*_args):
        custom=self.mode.currentIndex()==1;self.names.setVisible(custom)
        for widget in (self.prefix,self.start,self.count,self.digits):widget.setEnabled(not custom and not self.is_busy())
        self.preview.setRowCount(0);self.create_button.setEnabled(False)
        if not self._state:self.create_button.setText('先选择并读取Profile');return
        try:plan=service.build_plan(self._state,self._names(),color=self.color.currentData() or 'auto',icon=self.icon.currentData())
        except ValueError as error:self.create_button.setText('请检查名称设置');self.status.setText(str(error));return
        rows=[('已有',item['name'],item['id'],COLOR_LABELS.get(item['color'],item['color'])) for item in self._state['containers']]
        rows += [('将新增',item['name'],item['userContextId'],COLOR_LABELS.get(item['color'],item['color'])) for item in plan['added']]
        rows += [('同名略过',name,'','') for name in plan['skipped']]
        self.preview.setRowCount(len(rows))
        for row,values in enumerate(rows):
            for column,value in enumerate(values):
                item=QtWidgets.QTableWidgetItem(str(value));item.setToolTip(str(value));self.preview.setItem(row,column,item)
                if values[0]=='将新增':item.setForeground(QtGui.QColor('#176b37'))
        self.create_button.setText(f'创建 {len(plan["added"])} 个容器')
        self.create_button.setEnabled(bool(plan['added']) and not self.is_busy())

    def create(self):
        if not self._state or self.is_busy():return
        try:
            names=self._names();color=self.color.currentData() or 'auto';icon=self.icon.currentData()
            plan=service.build_plan(self._state,names,color=color,icon=icon)
        except ValueError as error:QtWidgets.QMessageBox.warning(self,'设置无效',str(error));return
        if not plan['added']:return
        selected=self._selected()
        if not selected or selected['path']!=self._state['path']:return
        answer=QtWidgets.QMessageBox.question(self,'确认目标Firefox Profile',
            f'仅修改这个Profile：\n{selected["name"]}\n{selected["path"]}\n\n新增 {len(plan["added"])} 个，同名略过 {len(plan["skipped"])} 个。\n先备份containers.json；已有容器和登录数据不变。\n目标Profile需要退出，不会强制关闭浏览器。',
            QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No,QtWidgets.QMessageBox.StandardButton.No)
        if answer!=QtWidgets.QMessageBox.StandardButton.Yes:return
        self._submit({'action':'create','path':self._state['path'],'expected_sha':self._state['sha256'],'names':names,'color':color,'icon':icon})

    def _completed(self,result):
        if result['action']=='scan':
            self._profiles=result['profiles'];self.profiles.blockSignals(True)
            self.profiles.setRowCount(len(self._profiles));self.profiles.clearSelection();self.target.clear()
            for row,profile in enumerate(self._profiles):
                values=(profile['name']+(' · 默认' if profile['default'] else ''),profile['path'],
                        profile['count'] if profile['count'] is not None else '—',profile['status'])
                for col,value in enumerate(values):
                    item=QtWidgets.QTableWidgetItem(str(value));item.setToolTip(profile['error'] or profile['path']);self.profiles.setItem(row,col,item)
            self.profiles.blockSignals(False)
            self.status.setText(f'发现 {len(self._profiles)} 个Profile。请选择要修改的那一个。'+('\n'+'\n'.join(result['warnings']) if result['warnings'] else ''))
        elif result['action']=='inspect':
            selected=self._selected()
            if not selected or selected['path']!=result['state']['path']:return
            self._state=result['state'];self.color.blockSignals(True);old=self.color.currentData();self.color.clear();self.color.addItem('自动轮换颜色','auto')
            palette=service.MODERN_COLORS if self._state['data']['version']>=6 else service.COLORS
            for label,key in zip(('蓝','青绿','绿','黄','橙','红','粉','紫'),palette):self.color.addItem(label,key)
            index=self.color.findData(old);self.color.setCurrentIndex(max(0,index));self.color.blockSignals(False)
            self.status.setText(f'所选Profile已有 {len(self._state["containers"])} 个公开容器；下方“将新增”仅为预览。')
            row=self.profiles.selectionModel().selectedRows()[0].row()
            self.profiles.item(row,2).setText(str(len(self._state['containers'])))
        elif result['action']=='create':
            self._receipt=result
            self.result.setText(f'已为所选Profile创建 {len(result["added"])} 个容器，同名略过 {len(result["skipped"])} 个。重新打开该Profile后生效。')
            self.result.setToolTip('Profile：'+result['path']+'\n备份：'+result['backup'])
            self._next_inspect=result['path'];self._state=None

    def _failed(self,message):
        if self._receipt and self._receipt.get('committed'):
            self.status.setText('容器已经写入；后到的取消操作不会撤销本次创建。')
        else:self.status.setText(message)

    def _finished(self):
        if self._next_inspect:
            path=self._next_inspect;self._next_inspect=None;self._submit({'action':'inspect','path':path});return
        self._enable(True)
