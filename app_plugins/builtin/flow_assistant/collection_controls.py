"""The same global collection controls on manual and batch submission pages."""
from qt_compat import QtCore,QtWidgets


class CollectionControls(QtWidgets.QWidget):
    def __init__(self,dialog,parent=None):
        super().__init__(parent);self.dialog=dialog
        row=QtWidgets.QHBoxLayout(self);row.setContentsMargins(0,0,0,0)
        self.collect=QtWidgets.QCheckBox('只添加任务记录，手动统一提交（默认开启）',self)
        self.collect.setChecked(dialog.state.get('preferences',{}).get('collect_before_submit',True))
        self.collect.setToolTip('手动与批量共用此开关；关闭不会自动放行已收集的记录。')
        self.submit=QtWidgets.QPushButton('统一提交已添加任务（0）',self)
        self.discard=QtWidgets.QPushButton('放弃待提交任务（0）',self)
        self.discard.setToolTip('只取消当前仍为待统一提交的记录；保留图片、草稿和记录，已放行或执行中的任务不受影响。')
        self.submit.clicked.connect(dialog.start_queue);self.discard.clicked.connect(self.discard_current)
        self.collect.toggled.connect(self.changed)
        row.addWidget(self.collect,1);row.addWidget(self.submit);row.addWidget(self.discard)
        self.update_counts()

    def changed(self,checked):
        self.dialog.send('preferences',value={'collect_before_submit':checked})
        for controls in self.dialog.findChildren(CollectionControls):
            if controls is not self:
                with QtCore.QSignalBlocker(controls.collect):controls.collect.setChecked(checked)
        if hasattr(self.dialog,'manual'):self.dialog.manual.update_collect_controls()
        if hasattr(self.dialog,'batch'):self.dialog.batch.update_controls()

    def update_counts(self):
        count=sum(r['status']=='collected' for r in self.dialog.state.get('jobs',[]))
        self.submit.setText(f'统一提交已添加任务（{count}）');self.submit.setEnabled(count>0)
        self.discard.setText(f'放弃待提交任务（{count}）');self.discard.setEnabled(count>0)

    def apply(self,state):
        with QtCore.QSignalBlocker(self.collect):self.collect.setChecked(state.get('preferences',{}).get('collect_before_submit',True))
        self.update_counts()

    def discard_current(self):
        ids=[row['id'] for row in self.dialog.state.get('jobs',[]) if row['status']=='collected']
        if ids:self.dialog.send('discard_collected',ids=ids)
