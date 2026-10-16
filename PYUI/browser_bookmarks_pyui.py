from dataclasses import asdict
from functools import wraps
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from model.AppDataStorage import application_storage
from model.BrowserBookmarks import (BookmarkLibrary, discover_profiles, run_bookmark_import, run_bookmark_cleanup)


def safe_action(action):
    """Never allow a storage/submit exception to escape a Qt signal callback."""
    @wraps(action)
    def invoke(self, *args, **kwargs):
        try: return action(self, *args, **kwargs)
        except Exception as error:
            self.status.setText('书签操作未完成：' + str(error))
            QtWidgets.QMessageBox.warning(self, '书签操作未完成', str(error))
    return invoke


class BrowserBookmarksPanel(QtWidgets.QWidget):
    def __init__(self,context,parent=None):
        super().__init__(parent)
        self.context=context; self.library=BookmarkLibrary(context.storage); self.worker=None
        self.auto_clear=False; self.batch=''; self.extra=[]
        roots=context.storage.file('bookmarks/browser_roots.json')
        if roots.exists():self.extra=roots.read_json().get('roots',[])
        layout=QtWidgets.QVBoxLayout(self)
        tools=QtWidgets.QHBoxLayout(); layout.addLayout(tools)
        self.search=QtWidgets.QLineEdit(self); self.search.setPlaceholderText('搜索标题、网址、原文件夹或浏览器')
        tools.addWidget(self.search,1)
        self.add_button=QtWidgets.QPushButton('添加书签',self); tools.addWidget(self.add_button)
        menu_button=QtWidgets.QToolButton(self); menu_button.setText('导入 / 恢复'); tools.addWidget(menu_button)
        menu=QtWidgets.QMenu(menu_button); menu_button.setMenu(menu); menu_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu.addAction('从 Chrome / Edge / Firefox 导入…',self.import_dialog)
        menu.addAction('添加便携版 / 自定义浏览器目录…',self.add_directory)
        menu.addAction('查看最近导入结果 / 失败详情',self.show_import_result)
        menu.addSeparator()
        menu.addAction('清理最近已校验批次的浏览器书签…',self.cleanup_prompt)
        menu.addAction('恢复最近批次的源浏览器书签…',self.restore_prompt)
        menu.addAction('打开软件书签数据目录',self.open_data_directory)
        self.table=QtWidgets.QTableWidget(0,4,self)
        self.table.setHorizontalHeaderLabels(['标题','网址','来源浏览器 / 配置','原文件夹'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.itemDoubleClicked.connect(lambda _item:self.open_selected())
        layout.addWidget(self.table,1)
        self.status=QtWidgets.QLabel('书签只保存在本地软件；不会上传网址、浏览历史、Cookie 或密码。',self)
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.search.textChanged.connect(self.refresh)
        self.add_button.clicked.connect(self.add_bookmark)
        self.refresh()

    def refresh(self):
        try:
            items=self.library.load()['items']; key=self.search.text().strip().casefold()
            items=[item for item in items if not key or key in (item['title']+' '+item['url']+' '+str(item['origins'])).casefold()]
            self.table.setRowCount(len(items))
            for row,item in enumerate(items):
                origins=item['origins']
                values=(item['title'],item['url'],'; '.join(dict.fromkeys(x['browser']+' / '+x['profile'] for x in origins)),
                        '; '.join(dict.fromkeys(x['folder'] for x in origins)))
                for column,value in enumerate(values):
                    cell=QtWidgets.QTableWidgetItem(value); cell.setData(QtCore.Qt.ItemDataRole.UserRole,item['id'])
                    cell.setToolTip(value); self.table.setItem(row,column,cell)
            file=self.context.storage.file('bookmarks/last_import.json')
            if file.exists(): self.batch=file.read_json()['batch']
        except Exception as error: self.status.setText('书签加载失败：'+str(error))

    def selected_rows(self): return sorted({item.row() for item in self.table.selectedItems()})
    @safe_action
    def show_import_result(self):
        file=self.context.storage.file('bookmarks/last_import.json')
        if not file.exists():
            QtWidgets.QMessageBox.information(self,'尚未导入','请先从浏览器导入书签。');return
        value=file.read_json()
        lines=[f"读取 {value.get('read','—')} 项；新增 {value.get('added','—')} 项；重复合并 {value.get('duplicates','—')} 项。"]
        for item in value.get('errors',[]):
            profile=item['profile'];lines.append(profile['browser']+' / '+profile['name']+'：'+item['error'])
        lines.append('未成功配置不会自动清理；源数据有变化时请关闭浏览器后重新导入。')
        QtWidgets.QMessageBox.information(self,'最近导入结果','\n'.join(lines))
    def selected_urls(self): return [self.table.item(row,1).text() for row in self.selected_rows()]
    def open_selected(self):
        for url in self.selected_urls():
            parsed=QtCore.QUrl(url)
            if parsed.scheme().casefold() not in ('http','https'):
                QtWidgets.QMessageBox.information(self,'特殊书签','此类网址不自动运行，请右键复制后自行使用。'); continue
            from model.OfflineMode import require_online
            try: require_online('打开书签'); QtGui.QDesktopServices.openUrl(parsed)
            except Exception as error: QtWidgets.QMessageBox.warning(self,'无法打开',str(error))
    def context_menu(self,position):
        menu=QtWidgets.QMenu(self)
        menu.addAction('打开所选书签',self.open_selected)
        menu.addAction('复制网址',lambda:QtWidgets.QApplication.clipboard().setText('\n'.join(self.selected_urls())))
        menu.addSeparator(); menu.addAction('全选',self.table.selectAll)
        menu.addAction('从所有浏览器导入…',self.import_dialog)
        menu.addAction('从软件书签库删除所选',self.remove_selected)
        menu.exec(self.table.viewport().mapToGlobal(position))
    def add_bookmark(self):
        url,ok=QtWidgets.QInputDialog.getText(self,'添加书签','完整网址：')
        if not ok or not url.strip(): return
        title,ok=QtWidgets.QInputDialog.getText(self,'书签标题','标题：')
        if not ok:return
        try:self.library.add(title.strip(),url.strip());self.refresh()
        except Exception as error:QtWidgets.QMessageBox.warning(self,'无法保存',str(error))
    def remove_selected(self):
        ids=[self.table.item(row,0).data(QtCore.Qt.ItemDataRole.UserRole) for row in self.selected_rows()]
        if not ids:return
        if QtWidgets.QMessageBox.question(self,'删除软件书签',f'从软件书签库删除 {len(ids)} 项？不修改浏览器或恢复副本。')!=QtWidgets.QMessageBox.StandardButton.Yes:return
        try:self.library.remove(ids);self.refresh()
        except Exception as error:QtWidgets.QMessageBox.warning(self,'删除失败',str(error))
    @safe_action
    def add_directory(self):
        browser,ok=QtWidgets.QInputDialog.getItem(self,'自定义目录','浏览器类型：',['Chrome','Edge','Firefox'],0,False)
        if not ok:return
        path=QtWidgets.QFileDialog.getExistingDirectory(self,'选择 User Data / 配置目录（Firefox 可选 profiles.ini 所在目录）')
        if path:
            extra=[*self.extra, {'browser':browser,'path':path,'kind':'firefox' if browser=='Firefox' else 'chromium'}]
            self.context.storage.file('bookmarks/browser_roots.json').write_json({'roots':extra},indent=2)
            self.extra=extra
    @safe_action
    def import_dialog(self):
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self,'正在处理','当前书签任务尚未完成，可在主界面后台任务查看。');return
        try:
            extra=list(self.extra)
            environment_root=self.context.storage.directory('language_environments').path
            for path in environment_root.glob('*/User Data'):
                extra.append({'browser':'Chrome · 独立环境','path':str(path),'kind':'chromium'})
            profiles=discover_profiles(extra=extra)
        except Exception as error:QtWidgets.QMessageBox.warning(self,'扫描失败',str(error));return
        if not profiles:
            QtWidgets.QMessageBox.information(self,'未发现书签','未找到当前 Windows 用户的 Chrome、Edge 或 Firefox 书签。便携版可从菜单添加自定义目录。');return
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('导入浏览器书签');dialog.resize(650,420)
        layout=QtWidgets.QVBoxLayout(dialog);listing=QtWidgets.QListWidget(dialog);layout.addWidget(listing,1)
        for profile in profiles:
            item=QtWidgets.QListWidgetItem(profile.browser+' / '+profile.name);item.setToolTip(profile.path)
            item.setFlags(item.flags()|QtCore.Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(QtCore.Qt.CheckState.Checked);listing.addItem(item)
        choices=QtWidgets.QHBoxLayout();layout.addLayout(choices)
        for title,state in [('全选',QtCore.Qt.CheckState.Checked),('全不选',QtCore.Qt.CheckState.Unchecked)]:
            button=QtWidgets.QPushButton(title,dialog);choices.addWidget(button)
            button.clicked.connect(lambda _checked=False,s=state:[listing.item(i).setCheckState(s) for i in range(listing.count())])
        clear=QtWidgets.QCheckBox('导入校验成功后自动清理源书签（需 VC 模式、关闭浏览器，并再次确认）',dialog)
        clear.setChecked(False);layout.addWidget(clear)
        note=QtWidgets.QLabel('完整网址去重，保留标题和来源；只读取书签，不导入密码、Cookie 或浏览历史。\n'
                             '书签同步可能把清理传播到其他设备或恢复旧书签，程序不会替你关闭同步。',dialog)
        note.setWordWrap(True);layout.addWidget(note)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel,dialog)
        layout.addWidget(buttons);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted:return
        selected=[asdict(profiles[i]) for i in range(len(profiles)) if listing.item(i).checkState()==QtCore.Qt.CheckState.Checked]
        if not selected:return
        self.auto_clear=clear.isChecked()
        self.worker=self.context.submit_background_task(title=f'书签导入与去重 · {len(selected)} 个配置',runner=run_bookmark_import,
            payload={'profiles':selected},kind='io',execution='exclusive',resources=('bookmarks:library',))
        self.worker.completed.connect(self.import_finished);self.worker.failed.connect(self.failed)
        self.status.setText('已提交书签导入；关闭本窗口不取消后台任务。')
    @QtCore.pyqtSlot(object)
    @safe_action
    def import_finished(self,result):
        self.worker=None;self.batch=result['batch'];self.refresh()
        self.status.setText(f"读取 {result['read']} 项，新增 {result['added']} 项，重复合并 {result['duplicates']} 项；失败配置 {len(result['errors'])} 个。源书签尚未删除。")
        if result['errors']:
            QtWidgets.QMessageBox.warning(self,'部分配置未导入','\n'.join(item['profile']['browser']+' / '+item['profile']['name']+'：'+item['error'] for item in result['errors']))
        if self.auto_clear:self.auto_clear=False;self.cleanup_prompt()
    @QtCore.pyqtSlot(str)
    def failed(self,message):self.worker=None;self.status.setText(message);QtWidgets.QMessageBox.warning(self,'书签处理未完成',message)
    @safe_action
    def cleanup_prompt(self):
        if not self.batch:QtWidgets.QMessageBox.information(self,'先导入书签','请先导入并校验书签。');return
        if not application_storage().protected:
            QtWidgets.QMessageBox.information(self,'先启用 VC','源书签清理需要把恢复副本保存在加密卷内。请关闭软件，用根目录“启动VC加密模式.cmd”完成迁移后再清理。');return
        if QtWidgets.QMessageBox.question(self,'确认清理浏览器源书签',
            '请关闭所有 Chrome、Edge、Firefox，包括后台进程。\n'
            '只清理最近批次中已完整保存、且未发生变化的配置；恢复副本保留在 VC 卷内。\n'
            '若开启书签同步，删除可能传播到其他设备，也可能被云端重新恢复。\n'
            'Firefox 恢复副本包含 Places 数据库，仅用于本地恢复，不上传。\n\n确认清理？',
            QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)!=QtWidgets.QMessageBox.StandardButton.Yes:return
        self.worker=self.context.submit_background_task(title='清理已导入的浏览器书签',runner=run_bookmark_cleanup,
            payload={'batch':self.batch},kind='io',execution='exclusive',resources=('bookmarks:library','bookmarks:browsers'))
        self.worker.completed.connect(self.cleanup_finished);self.worker.failed.connect(self.failed)
    @QtCore.pyqtSlot(object)
    @safe_action
    def cleanup_finished(self,result):
        self.worker=None;self.status.setText(f"已清理 {result['removed']} 项；未成功配置 {result['failed']} 个。恢复副本保留在加密数据目录。")
        if result['failed']:QtWidgets.QMessageBox.warning(self,'部分配置未清理','\n'.join(item.get('error','') for item in result['profiles'] if item['state']=='failed'))
    @safe_action
    def restore_prompt(self):
        if not self.batch:return
        if QtWidgets.QMessageBox.question(self,'恢复源书签','关闭所有浏览器后，恢复最近清理批次。若清理后源数据发生变化，将拒绝覆盖。软件书签库不删除。')!=QtWidgets.QMessageBox.StandardButton.Yes:return
        try:count=self.library.restore(self.batch);self.status.setText(f'已恢复 {count} 个浏览器配置的书签。')
        except Exception as error:QtWidgets.QMessageBox.warning(self,'未恢复',str(error))
    @safe_action
    def open_data_directory(self):
        folder=self.context.storage.directory('bookmarks');folder.ensure()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder.path)))
