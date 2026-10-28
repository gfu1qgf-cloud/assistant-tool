"""A compact live line and an optional modeless queue inspector."""
import time

from qt_compat import QtCore,QtWidgets
from .status_ui import status_area
from .mutex_monitor import summary,seconds


class MutexMonitor(QtWidgets.QWidget):
    def __init__(self,parent):
        super().__init__(parent)
        self.data={};self.details=None
        layout=QtWidgets.QHBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.label=status_area(QtWidgets.QLabel(self),1)
        self.label.setAccessibleName('实时UI锁持有者')
        layout.addWidget(self.label,1)
        self.button=QtWidgets.QPushButton('UI锁详情…',self)
        self.button.setToolTip('每秒显示实际持有者、租约剩余时间和等待队列；只查看状态。')
        self.button.clicked.connect(self.open_details);layout.addWidget(self.button)
        self.clock=QtCore.QTimer(self);self.clock.setInterval(1000)
        self.clock.timeout.connect(self.refresh);self.clock.start()
        self.refresh()

    def apply(self,data):
        if data and data.get('observed_at',0)>=self.data.get('observed_at',0):self.data=data
        self.refresh()

    def refresh(self):
        now=time.time();text=summary(self.data,now)
        self.label.setText(text)
        self.label.setAccessibleDescription(text)
        if not self.details or not self.details.isVisible():return
        self.details_note.setText(text)
        rows=[]
        if self.data.get('enabled'):
            owner=self.data.get('owner')
            if owner:
                remaining=seconds(now,owner['expires_at'])
                rows.append(['持有',owner['profile'],owner['operation'],f'{seconds(owner["since"],now)} 秒',
                    f'{remaining} 秒' if remaining else '到期，待确认释放'])
            for index,entry in enumerate(self.data.get('queue',[]),1):
                rows.append([f'等待 {index}',entry['profile'],entry['operation'],f'{seconds(entry["since"],now)} 秒','—'])
            for entry in self.data.get('launches',[]):
                rows.append(['待启动',entry['profile'],entry['operation'],f'{seconds(entry["since"],now)} 秒','—'])
            priority=self.data.get('priority')
            if priority:
                left=priority.get('remaining')
                rows.append(['整包优先',priority['profile'],'优先取得下一段UI锁',
                    '准备阶段未开始计时' if left is None else f'提交时间片剩余 {int(left)} 秒','—'])
        self.table.setRowCount(len(rows))
        for row,values in enumerate(rows):
            for col,text in enumerate(values):
                item=self.table.item(row,col)
                if item:item.setText(text)
                else:self.table.setItem(row,col,QtWidgets.QTableWidgetItem(text))

    def open_details(self):
        if not self.details:
            self.details=QtWidgets.QDialog(self)
            self.details.setWindowTitle('UI锁实时监控');self.details.resize(860,350)
            layout=QtWidgets.QVBoxLayout(self.details)
            self.details_note=QtWidgets.QLabel(self.details);self.details_note.setWordWrap(True)
            self.details_note.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(self.details_note)
            self.table=QtWidgets.QTableWidget(0,5,self.details)
            self.table.setHorizontalHeaderLabels(['位置','浏览器配置','操作','已等待／持有','租约剩余'])
            self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.verticalHeader().hide()
            self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            self.table.horizontalHeader().setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.Stretch)
            self.table.setToolTip('等待位置按当前整包优先策略展示。租约到期后，以主程序实际释放状态为准；整包优先不表示一直占有UI锁。')
            layout.addWidget(self.table,1)
            close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close,self.details)
            close.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setText('关闭')
            close.rejected.connect(self.details.close);layout.addWidget(close)
        self.details.show();self.refresh()
