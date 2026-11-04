"""Modeless, cancellable inventory; balances are fresh observations only."""
import time
from qt_compat import QtCore,QtGui,QtWidgets


class CreditScanDialog(QtWidgets.QDialog):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.report={};self.stamp=None
        self.setWindowTitle('全面刷新千分账号积分');self.resize(960,540)
        layout=QtWidgets.QVBoxLayout(self);bar=QtWidgets.QHBoxLayout()
        self.start=QtWidgets.QPushButton('开始全面刷新',self)
        self.start.clicked.connect(lambda:owner.send('start_credit_scan'));bar.addWidget(self.start)
        self.stop=QtWidgets.QPushButton('停止刷新',self)
        self.stop.clicked.connect(lambda:owner.send('stop_credit_scan'));bar.addWidget(self.stop)
        self.funded=QtWidgets.QCheckBox('只看有积分（≥10分）',self);self.funded.toggled.connect(self.render);bar.addWidget(self.funded)
        bar.addStretch();layout.addLayout(bar)
        self.note=QtWidgets.QLabel(self);self.note.setWordWrap(True);layout.addWidget(self.note)
        self.table=QtWidgets.QTableWidget(0,5,self)
        self.table.setHorizontalHeaderLabels(['浏览器配置','账号','最新积分','状态','说明'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        for col,width in ((0,120),(1,240),(2,90),(3,100)):self.table.setColumnWidth(col,width)
        self.table.verticalHeader().setMinimumWidth(32)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self.open_selected)
        layout.addWidget(self.table,1)
        bottom=QtWidgets.QHBoxLayout();button=QtWidgets.QPushButton('打开所选浏览器',self)
        button.clicked.connect(self.open_selected);bottom.addWidget(button);bottom.addStretch()
        close=QtWidgets.QPushButton('关闭',self);close.clicked.connect(self.close);bottom.addWidget(close);layout.addLayout(bottom)
        self.setToolTip('包含历史千分账号；离线账号逐个打开检测，临时窗口仅在确认空闲且没有个人标签页时收回。'
            '忙碌、手动禁用或达到窗口上限时说明原因并继续。关闭此窗口后后台继续；点“停止刷新”结束检测。刷新不改变账号使用权限。')
        self.apply(owner.state.get('credit_scan',{}))

    def apply(self,report):
        self.report=report;self.render()

    def render(self,*_):
        report=self.report;entries=report.get('entries',[]);running=report.get('running',False)
        self.start.setEnabled(not running);self.stop.setEnabled(running and not report.get('cancelled'))
        self.start.setText('重新全面刷新' if entries else '开始全面刷新')
        done=sum(e['phase']=='finished' for e in entries)
        funded=sum(type(e.get('credits')) is int and e['credits']>=10 for e in entries)
        self.note.setText((report.get('message') or '点击开始，逐个检查已识别的千分账号')+f' · 已处理 {done}/{len(entries)} · 有积分 {funded} 个')
        visible=[e for e in entries if not self.funded.isChecked() or type(e.get('credits')) is int and e['credits']>=10]
        stamp=repr(visible)
        if stamp==self.stamp:return
        self.stamp=stamp;selected=self.table.currentRow()
        profile=self.table.item(selected,0).data(QtCore.Qt.ItemDataRole.UserRole) if selected>=0 and self.table.item(selected,0) else None
        self.table.setRowCount(len(visible))
        for row,entry in enumerate(visible):
            points=entry.get('credits');state=entry['result']+('／正在收回窗口' if entry['phase']=='closing' else '')
            color='#e3efff' if type(points) is int and points>=100 else '#e0f4e8' if type(points) is int and points>=10 else '#fff4cc' if type(points) is int else '#fff0e8' if entry['result'] in {'失败','跳过'} else '#ffffff'
            for col,text in enumerate((entry['profile'],entry.get('account') or '—',str(points) if points is not None else '—',state,entry.get('note',''))):
                item=QtWidgets.QTableWidgetItem(text)
                item.setToolTip(text+('\n检测时间：'+time.strftime('%m-%d %H:%M:%S',time.localtime(entry['at'])) if col==2 and entry.get('at') else ''))
                item.setBackground(QtGui.QColor(color))
                item.setData(QtCore.Qt.ItemDataRole.UserRole,entry['profile']);self.table.setItem(row,col,item)
            if entry['profile']==profile:self.table.selectRow(row)
        self.table.resizeRowsToContents()

    def open_selected(self,*_):
        row=self.table.currentRow();item=self.table.item(row,0) if row>=0 else None
        if item:self.owner.send('open_browser',profile=item.data(QtCore.Qt.ItemDataRole.UserRole))
