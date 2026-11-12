"""Configure visual-pool exclusion beside pool order, without scanning media."""
import copy
from qt_compat import QtCore, QtWidgets
from .pool_exclusion import choices, pool_key, peers


class PoolExclusionDialog(QtWidgets.QDialog):
    def __init__(self,state,identifier,parent=None):
        super().__init__(parent)
        names=choices(state);self.identifier=identifier
        self.setWindowTitle('素材互斥 · '+names.get(identifier,'资源池'));self.resize(420,370)
        layout=QtWidgets.QVBoxLayout(self)
        rule=state['settings'].get('pool_exclusions',{}).get(identifier,{})
        active=peers(state['settings']).get(identifier,set())
        self.enabled=QtWidgets.QCheckBox('启用互斥选择');self.enabled.setChecked(bool(active) or rule.get('enabled',False));layout.addWidget(self.enabled)
        self.list=QtWidgets.QListWidget();self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        selected=set(rule.get('pools',[]))|set(active)
        for key in list(names)+sorted(selected-set(names)):
            if key==identifier:continue
            item=QtWidgets.QListWidgetItem(names.get(key,'已删除的池：'+key),self.list)
            item.setData(QtCore.Qt.ItemDataRole.UserRole,key);item.setFlags(item.flags()|QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if key in selected else QtCore.Qt.CheckState.Unchecked)
        layout.addWidget(self.list,1)
        note=QtWidgets.QLabel('同一成品内，与所选池避开同一个原文件。');note.setWordWrap(True);layout.addWidget(note)
        self.enabled.toggled.connect(self.list.setEnabled);self.list.setEnabled(self.enabled.isChecked())
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)

    def value(self):return dict(enabled=self.enabled.isChecked(),pools=[self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.list.count()) if self.list.item(i).checkState()==QtCore.Qt.CheckState.Checked])


def edit(host,key=None):
    identifier=pool_key(key or host.visible_pool_key())
    if host.is_batch_busy() or identifier not in choices(host.state):return
    host.edit_timer.stop()
    if not host.persist():return
    dialog=PoolExclusionDialog(host.state,identifier,host)
    try:
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted:return
        before=copy.deepcopy(host.state['settings'].get('pool_exclusions',{}))
        rules=copy.deepcopy(before)
        # Editing either side owns that pool's relationships. Turning its switch
        # off removes incoming constraints too; it cannot remain secretly active.
        for other,rule in rules.items():
            if other!=identifier:
                rule['pools']=[v for v in rule.get('pools',[]) if v!=identifier]
                if not rule['pools']:rule['enabled']=False
        rules[identifier]=dialog.value();host.state['settings']['pool_exclusions']=rules
        if not host.persist():host.state['settings']['pool_exclusions']=before;return
        from .pools import bind_pool_layers
        with QtCore.QSignalBlocker(host.layer_panel):host.layer_panel.set_layers(bind_pool_layers(host.layer_panel.values(),host.state))
        host.invalidate_playback();host.update_preview();host.refresh_editor_baseline()
        host.status.setText('素材互斥设置已保存；同一成品内避开重复原文件。')
    finally:dialog.deleteLater()


def add_action(host,menu,key):
    if pool_key(key) not in choices(host.state):return
    action=menu.addAction('互斥选择…',lambda:edit(host,key));action.setEnabled(not host.is_batch_busy())


def install(host):
    host.pool_exclusion_button=QtWidgets.QToolButton();host.pool_exclusion_button.setText('互斥…')
    host.pool_exclusion_button.setAccessibleName('素材互斥选择')
    host.pool_exclusion_button.setToolTip('同一成品内，选择需要避开重复原文件的其他素材池。')
    host.pool_exclusion_button.clicked.connect(lambda:edit(host))
