"""A modeless, refreshable, explicit video-to-download picker."""
import base64
import uuid
from qt_compat import QtCore, QtGui, QtWidgets


class VideoCatalogDialog(QtWidgets.QDialog):
    def __init__(self, owner, job):
        super().__init__(owner)
        self.owner, self.job = owner, job['id']
        self.request_id = self.catalog = ''
        self.entries = []
        self.binding_allowed = False
        self.setWindowTitle('选择 Flow 视频恢复下载')
        self.resize(960, 670)
        layout = QtWidgets.QVBoxLayout(self)
        target = QtWidgets.QLabel('保存为：' + job['payload']['output'], self)
        target.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        target.setWordWrap(True); layout.addWidget(target)
        bar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit(self); self.search.setPlaceholderText('搜索提示词或视频编号')
        self.refresh_button = QtWidgets.QPushButton('刷新列表', self)
        self.refresh_button.setToolTip('重新读取原账号、原项目已经加载的视频；不刷新网页、不切换窗口。新视频出现后可再次刷新。')
        bar.addWidget(self.search, 1); bar.addWidget(self.refresh_button); layout.addLayout(bar)
        self.table = QtWidgets.QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(['预览', '提示词／视频编号', '时长', '页面时间'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.setIconSize(QtCore.QSize(92, 100))
        self.table.verticalHeader().hide()
        self.table.setColumnWidth(0, 110); self.table.setColumnWidth(2, 75); self.table.setColumnWidth(3, 150)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.detail = QtWidgets.QPlainTextEdit(self); self.detail.setReadOnly(True); self.detail.setMaximumHeight(110)
        self.detail.setPlaceholderText('选择视频后，在这里核对提示词和视频编号。'); layout.addWidget(self.detail)
        self.note = QtWidgets.QLabel('正在读取…', self); self.note.setWordWrap(True); layout.addWidget(self.note)
        actions = QtWidgets.QHBoxLayout(); actions.addStretch()
        self.use_button = QtWidgets.QPushButton('使用所选视频恢复下载', self); self.use_button.setEnabled(False)
        self.use_button.setToolTip('手动指定此任务下载哪条视频；保留原生成回执，只恢复下载。')
        close = QtWidgets.QPushButton('关闭', self)
        actions.addWidget(self.use_button); actions.addWidget(close); layout.addLayout(actions)
        close.clicked.connect(self.reject); self.refresh_button.clicked.connect(self.refresh)
        self.search.textChanged.connect(self.filter); self.table.itemSelectionChanged.connect(self.select)
        self.use_button.clicked.connect(self.bind)
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(22000)
        self.timer.timeout.connect(self.timeout)

    def refresh(self):
        self.request_id = uuid.uuid4().hex; self.catalog = ''
        self.entries = []; self.table.setRowCount(0); self.detail.clear()
        self.binding_allowed = False
        self.use_button.setEnabled(False); self.refresh_button.setEnabled(False)
        self.note.setText('正在读取原项目的视频…')
        self.timer.start()
        self.owner.send('list_project_videos', job=self.job, request_id=self.request_id)

    def timeout(self):
        self.request_id = ''
        self.refresh_button.setEnabled(True)
        self.note.setText('读取超时，未占用生成名额。确认原项目页面已打开后点击刷新。')

    def receive(self, result):
        if result.get('job') != self.job or result.get('request_id') != self.request_id: return
        self.timer.stop(); self.refresh_button.setEnabled(True)
        if not result.get('success'):
            self.note.setText(result.get('error') or '读取失败，请刷新重试。')
            self.select()
            return
        if result.get('bound'):
            self.owner.note.setText('已按手动选择恢复视频下载；可在任务列表查看结果。')
            self.accept(); return
        self.catalog = result['catalog']; self.entries = result.get('videos', [])
        self.binding_allowed = result.get('can_bind', True)
        self.table.setRowCount(len(self.entries))
        for index, entry in enumerate(self.entries):
            preview = QtWidgets.QTableWidgetItem('无预览')
            if entry.get('thumbnail'):
                pixmap = QtGui.QPixmap()
                pixmap.loadFromData(base64.b64decode(entry['thumbnail'].split(',', 1)[1]))
                if not pixmap.isNull(): preview.setText(''); preview.setIcon(QtGui.QIcon(pixmap))
            text = (entry.get('prompt') or '页面未提供提示词') + '\n' + entry['media_id']
            for col, item in enumerate([preview, QtWidgets.QTableWidgetItem(text),
                    QtWidgets.QTableWidgetItem(entry.get('duration') or '—'), QtWidgets.QTableWidgetItem(entry.get('created') or '—')]):
                item.setToolTip(text); self.table.setItem(index, col, item)
            self.table.setRowHeight(index, 110)
        self.table.clearSelection(); self.table.setCurrentCell(-1, -1)
        self.filter()
        self.note.setText(f'{len(self.entries)} 条已加载视频 · 按 Flow 页面顺序；新视频出现后可刷新。' if self.entries else
            '没有读到视频。请在原浏览器打开视频列表或视频详情，待页面显示后点击刷新。')
        if result.get('binding_reason'): self.note.setText(self.note.text() + '\n' + result['binding_reason'])
        self.note.setToolTip('优先读取原账号当前选中的项目列表页，保留 Flow 页面排序，不混排其他标签页。没有列表页时才读取详情页。虚拟列表中尚未加载的历史视频，请先在网页显示后刷新；不主动滚动或打断上传。' +
            ('\n' + result['warning'] if result.get('warning') else ''))

    def filter(self, *_):
        needle = self.search.text().strip().casefold()
        for i, item in enumerate(self.entries):
            self.table.setRowHidden(i, bool(needle and needle not in (item.get('prompt', '') + item['media_id']).casefold()))
        self.select()

    def select(self):
        index = self.table.currentRow()
        valid = 0 <= index < len(self.entries) and not self.table.isRowHidden(index) and self.table.selectionModel().hasSelection()
        self.use_button.setEnabled(bool(valid and self.catalog and self.binding_allowed and self.refresh_button.isEnabled()))
        entry = self.entries[index] if valid else {}
        self.detail.setPlainText((entry.get('prompt') or '') + ('\n\n视频编号：' + entry['media_id'] if entry else ''))

    def bind(self):
        index = self.table.currentRow()
        if not self.catalog or not self.binding_allowed or not 0 <= index < len(self.entries) or self.table.isRowHidden(index): return
        self.request_id = uuid.uuid4().hex
        self.use_button.setEnabled(False); self.refresh_button.setEnabled(False)
        self.note.setText('正在保存手动选择并恢复下载…'); self.timer.start()
        self.owner.send('bind_download_video', job=self.job, request_id=self.request_id,
            catalog=self.catalog, key=self.entries[index]['key'], confirmed=True)
