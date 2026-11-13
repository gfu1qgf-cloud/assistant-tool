"""Virtual music rows; imports register paths in a worker without decoding audio."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import random
import threading
from qt_compat import QtCore,QtWidgets
from . import shared_music as shared


class MusicModel(QtCore.QAbstractTableModel):
    def __init__(self,parent=None):super().__init__(parent);self.pool=shared.fresh()
    def rowCount(self,parent=QtCore.QModelIndex()):return 0 if parent.isValid() else len(self.pool['items'])
    def columnCount(self,parent=QtCore.QModelIndex()):return 0 if parent.isValid() else 3
    def headerData(self,section,orientation,role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role==QtCore.Qt.ItemDataRole.DisplayRole:
            return ('文件','续用位置','上次使用')[section] if orientation==QtCore.Qt.Orientation.Horizontal else section+1
    def data(self,index,role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return
        item=self.pool['items'][index.row()]
        if role==QtCore.Qt.ItemDataRole.ToolTipRole:return item['path']
        if role==QtCore.Qt.ItemDataRole.DisplayRole:
            if index.column()==1:return f'{item.get("cursor",0):.1f} 秒'
            if index.column()==2:return item.get('last_used') or '未使用'
            prefix='→ ' if item['id']==self.pool['next_id'] or not self.pool['next_id'] and index.row()==0 else ''
            return prefix+Path(item['path']).name
    def set_pool(self,pool):self.beginResetModel();self.pool=copy.deepcopy(pool);self.endResetModel()


def prepare_paths(pool,paths,cancel):
    from .custom_pools import accepted_suffixes
    suffixes=accepted_suffixes('music');files=[]
    for value in paths:
        if cancel.is_set():return None
        if Path(value).is_dir():
            for directory,_folders,names in os.walk(value):
                if cancel.is_set():return None
                for name in names:
                    if Path(name).suffix.casefold() in suffixes:files.append(str(Path(directory)/name))
                    if len(files)>shared.MAX_ITEMS:raise ValueError(f'一次最多导入{shared.MAX_ITEMS}首，请分批选择。')
        else:files.append(value)
    if cancel.is_set():return None
    count=shared.add_paths(pool,files)
    return pool,count


class SharedMusicDialog(QtWidgets.QDialog):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.store=owner.store
        self.setWindowTitle('共享背景音乐池 · 所有项目共用');self.resize(820,560)
        pools=shared.normalize(self.store.load());selected=owner.state['settings'].get('music_pool_id')
        self.pool=copy.deepcopy(next((p for p in pools if p['id']==selected),pools[0] if pools else None));self.cancel=threading.Event()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='SharedMusicImport');self.future=None
        body=QtWidgets.QVBoxLayout(self);library_row=QtWidgets.QHBoxLayout();body.addLayout(library_row)
        self.libraries=QtWidgets.QComboBox();library_row.addWidget(self.libraries,1);self.library_buttons=[]
        for title,callback in (('新建库…',self.create_library),('重命名…',self.rename_library),('删除库…',self.delete_library)):
            button=QtWidgets.QPushButton(title);button.setAutoDefault(False);button.clicked.connect(callback);library_row.addWidget(button);self.library_buttons.append(button)
        self.libraries.currentIndexChanged.connect(self.select_library)
        row=QtWidgets.QHBoxLayout();body.addLayout(row);self.mutations=[]
        for title,callback in (('添加文件…',self.choose_files),('添加文件夹…',self.choose_folder),
                               ('从当前音乐池加入',self.import_current),('移除所选',self.remove),('打乱顺序',self.shuffle),('重置进度',self.reset)):
            button=QtWidgets.QPushButton(title);button.setAutoDefault(False);button.clicked.connect(callback);row.addWidget(button);self.mutations.append(button)
            if title=='打乱顺序':self.shuffle_button=button
        self.table=QtWidgets.QTableView();self.model=MusicModel(self);self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1,100);self.table.setColumnWidth(2,180);body.addWidget(self.table,1)
        self.note=QtWidgets.QLabel();body.addWidget(self.note)
        footer=QtWidgets.QHBoxLayout();body.addLayout(footer)
        refresh=QtWidgets.QPushButton('刷新');refresh.clicked.connect(self.reload);footer.addWidget(refresh)
        footer.addStretch();use=QtWidgets.QPushButton('当前项目使用共享池');use.clicked.connect(self.use);footer.addWidget(use);self.mutations.append(use)
        close=QtWidgets.QPushButton('关闭');close.clicked.connect(self.accept);footer.addWidget(close)
        self.poll=QtCore.QTimer(self);self.poll.setInterval(80);self.poll.timeout.connect(self.finish_import)
        self.reload()

    def display(self,message=''):
        self.model.set_pool(self.pool or shared.fresh())
        self.note.setText(message or (f'{len(self.pool["items"])} 首 · 项目共用顺序和续播位置；只保存路径，不复制音乐文件。' if self.pool else '请先新建一个共享音乐库。'))
        for button in self.mutations:button.setEnabled(bool(self.pool) and not self.future)
        for button in self.library_buttons[1:]:button.setEnabled(bool(self.pool) and not self.future)
        self.shuffle_button.setText('恢复原顺序' if self.pool and self.pool.get('original_order') else '随机顺序')

    def reload(self):
        if self.future:return
        selected=self.pool['id'] if self.pool else ''
        pools=shared.normalize(self.store.load());blocker=QtCore.QSignalBlocker(self.libraries);self.libraries.clear()
        for pool in pools:self.libraries.addItem(pool['name'],pool['id'])
        self.libraries.setCurrentIndex(max(0,self.libraries.findData(selected)));del blocker
        self.select_library()

    def select_library(self,*_args):
        if self.future:return
        identifier=self.libraries.currentData()
        self.pool=copy.deepcopy(shared.get(self.store.load(),identifier)) if identifier else None
        self.display()

    def error(self,title,error):
        self.note.setText(str(error))
        from .export_results_ui import show_action_error
        show_action_error(self,title,error)

    def create_library(self):
        name,ok=QtWidgets.QInputDialog.getText(self,'新建共享音乐库','名称')
        if not ok:return
        try:self.pool=self.store.create_shared_music(name);self.reload();sync_owner(self.owner)
        except Exception as error:self.error('未创建音乐库',error)

    def rename_library(self):
        if not self.pool:return
        name,ok=QtWidgets.QInputDialog.getText(self,'重命名共享音乐库','名称',text=self.pool['name'])
        if not ok:return
        value=copy.deepcopy(self.pool);value['name']=name.strip()
        if self.save(value):self.reload()

    def delete_library(self):
        if not self.pool:return
        if QtWidgets.QMessageBox.question(self,'删除共享音乐库','删除“'+self.pool['name']+'”的库记录？原音乐文件不会删除。')!=QtWidgets.QMessageBox.StandardButton.Yes:return
        try:
            self.store.delete_shared_music(self.pool['id'],self.pool['revision']);self.pool=None;self.reload();sync_owner(self.owner)
        except Exception as error:self.error('未删除音乐库',error)

    def save(self,value,message='',*,additions=None):
        try:
            self.pool=self.store.save_shared_music(value,self.pool['revision'],additions=additions);self.display(message)
            sync_owner(self.owner)
            return True
        except Exception as error:self.error('未保存音乐库',error);return False

    def import_paths(self,paths):
        if self.future or not paths or not self.pool:return
        self.future=self.executor.submit(prepare_paths,copy.deepcopy(self.pool),list(paths),self.cancel)
        for button in self.mutations:button.setEnabled(False)
        for button in self.library_buttons:button.setEnabled(False)
        self.libraries.setEnabled(False)
        self.note.setText('正在登记音乐文件…');self.poll.start()

    def finish_import(self):
        if not self.future or not self.future.done():return
        future=self.future;self.future=None;self.poll.stop()
        for button in self.mutations:button.setEnabled(True)
        for button in self.library_buttons:button.setEnabled(True)
        self.libraries.setEnabled(True)
        try:
            result=future.result()
            if result:
                value,count=result
                if count:
                    before={v['id'] for v in self.pool['items']}
                    self.save(value,f'已登记 {count} 首新音乐。',additions=[v for v in value['items'] if v['id'] not in before])
                else:self.display('没有新音乐；重复路径和不支持的文件已忽略。')
        except Exception as error:self.error('音乐导入未完成',error)

    def choose_files(self):
        paths,_=QtWidgets.QFileDialog.getOpenFileNames(self,'添加共享背景音乐','','音频 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma)')
        self.import_paths(paths)
    def choose_folder(self):
        path=QtWidgets.QFileDialog.getExistingDirectory(self,'添加音乐文件夹（含子文件夹）')
        if path:self.import_paths([path])
    def import_current(self):
        from .custom_pools import music_source
        items,_next,pool=music_source(self.owner.state)
        if pool and self.pool and pool['id']==self.pool['id']:self.note.setText('当前项目正在使用这个共享库。');return
        self.import_paths([item['path'] for item in items])
    def remove(self):
        selected={i.row() for i in self.table.selectionModel().selectedRows()}
        if not selected:return
        value=copy.deepcopy(self.pool);value['items']=[v for i,v in enumerate(value['items']) if i not in selected]
        if value['next_id'] not in {v['id'] for v in value['items']}:value['next_id']=''
        self.save(value)
    def shuffle(self):
        if len(self.pool['items'])<2:return
        value=copy.deepcopy(self.pool)
        if value.get('original_order'):
            order={identifier:i for i,identifier in enumerate(value.pop('original_order'))}
            value['items'].sort(key=lambda item:order.get(item['id'],len(order)))
        else:
            value['original_order']=[v['id'] for v in value['items']];random.SystemRandom().shuffle(value['items'])
        value['next_id']='';self.save(value)
    def reset(self):
        if QtWidgets.QMessageBox.question(self,'重置共享音乐进度','重置所有项目共用的音乐顺序和续播位置？')!=QtWidgets.QMessageBox.StandardButton.Yes:return
        value=copy.deepcopy(self.pool);value['next_id']=''
        for item in value['items']:item.update(cursor=0.,last_used='')
        self.save(value)
    def use(self):
        if not self.pool:return
        if self.owner.is_batch_busy():self.note.setText('当前项目正在处理，请结束后切换来源。');return
        self.owner.controls['music_pool_id'].setCurrentIndex(self.owner.controls['music_pool_id'].findData(self.pool['id']))
        self.owner.controls['music_enabled'].setChecked(True)
        if self.owner.persist():self.note.setText('当前项目已使用共享背景音乐池。')
    def done(self,result):
        self.cancel.set();self.poll.stop();self.executor.shutdown(wait=False,cancel_futures=True);super().done(result)


def sync_owner(owner):
    current=owner.store.load()
    owner.state[shared.KEY]=copy.deepcopy(current[shared.KEY])
    # A global-only edit never touches the active project's revision.
    identifier=owner.state.get('active_profile')
    old=next((p for p in owner.state.get('profiles',[]) if p['id']==identifier),{})
    live=next((p for p in current.get('profiles',[]) if p['id']==identifier),{})
    if current.get('active_profile')==identifier and old.get('revision',0)==live.get('revision',0):
        owner.state['revision']=current['revision']
    for card in getattr(owner,'audio_cards',{}).values():card.sync_pools(owner.state)


def open_manager(widget):
    owner=widget
    while owner is not None and not all(hasattr(owner,k) for k in ('store','state','persist','controls')):owner=owner.parentWidget()
    if owner is None:return
    if not all(callable(getattr(owner.store,k,None)) for k in ('save_shared_music','create_shared_music','delete_shared_music')):
        from .export_results_ui import show_action_error
        show_action_error(owner,'请正常重启后使用','共享音乐库源码已更新，当前会话仍载有旧的数据接口。请正常重启软件后使用。')
        return
    if not owner.is_batch_busy() and not owner.persist():return
    dialog=SharedMusicDialog(owner)
    try:dialog.exec()
    finally:dialog.cancel.set();dialog.poll.stop();dialog.executor.shutdown(wait=False,cancel_futures=True);dialog.deleteLater()


def reset_from_owner(owner):
    sync_owner(owner);value=copy.deepcopy(shared.get(owner.state,owner.controls['music_pool_id'].currentData()));value['next_id']=''
    for item in value['items']:item.update(cursor=0.,last_used='')
    owner.store.save_shared_music(value,value['revision']);sync_owner(owner)
