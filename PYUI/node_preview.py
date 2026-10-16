"""Refreshable, main-thread preview presentation; no worker ownership."""
import base64
from qt_compat import QtCore, QtGui, QtWidgets


class PreviewImage(QtWidgets.QLabel):
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.original = QtGui.QPixmap()
        self.native = False
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#192331;border-radius:8px;")
        self.set_data(data)

    def set_data(self, data):
        original = QtGui.QPixmap()
        if not original.loadFromData(base64.b64decode(data, validate=True)):
            raise ValueError('预览图片无效，保留此前结果。')
        self.original = original
        self.redraw()

    def redraw(self):
        self.setPixmap(self.original if self.native else self.original.scaled(self.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        self.redraw()
        super().resizeEvent(event)


class NodePreviewDialog(QtWidgets.QDialog):
    refreshRequested = QtCore.pyqtSignal()

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.node_id = result.get('node_id', '')
        self.last_signature = None
        self.stale, self.busy = False, False
        self.error_message = ''
        self.result = {}
        self.image = None
        screen = self.screen().availableGeometry()
        self.resize(min(1100, screen.width() - 60), min(900 if result.get('png') else 550, screen.height() - 80))
        layout = QtWidgets.QVBoxLayout(self)
        self.heading = QtWidgets.QLabel()
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        toolbar = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton('刷新预览')
        self.refresh_button.clicked.connect(self.refreshRequested.emit)
        toolbar.addWidget(self.refresh_button)
        self.auto_refresh = QtWidgets.QCheckBox('上游变化自动刷新')
        self.auto_refresh.setToolTip('停止编辑约 0.6 秒后刷新；有耗时模型节点时建议手动刷新。')
        self.auto_refresh.toggled.connect(lambda on: self.mark_stale(self.stale) if on else self.refresh_timer.stop())
        toolbar.addWidget(self.auto_refresh)
        self.quality = QtWidgets.QComboBox()
        for size, title in ((720, '720 · 较快'), (1080, '1080 · 清晰'), (2160, '2160 · 高清')):
            self.quality.addItem(title, size)
        self.quality.setCurrentIndex(1)
        self.quality.currentIndexChanged.connect(lambda _i: self.mark_stale(True))
        self.quality_label = QtWidgets.QLabel('预览长边上限')
        toolbar.addWidget(self.quality_label)
        toolbar.addWidget(self.quality)
        self.zoom = QtWidgets.QComboBox()
        self.zoom.addItems(['适应窗口', '100% · 像素大小'])
        self.zoom.currentIndexChanged.connect(self.apply_zoom)
        toolbar.addWidget(self.zoom)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.status = QtWidgets.QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.splitter.addWidget(self.scroll)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(80)
        self.splitter.addWidget(self.text)
        layout.addWidget(self.splitter, 1)
        row = QtWidgets.QHBoxLayout()
        self.details = QtWidgets.QCheckBox('显示结果数据')
        self.details.toggled.connect(self.text.setVisible)
        row.addWidget(self.details)
        copy_button = QtWidgets.QPushButton("复制结果数据")
        copy_button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.result.get('text', '')))
        close_button = QtWidgets.QPushButton("关闭")
        close_button.clicked.connect(self.close)
        row.addWidget(copy_button)
        row.addStretch()
        maximize = QtWidgets.QPushButton('最大化／还原')
        maximize.clicked.connect(lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        row.addWidget(maximize)
        row.addWidget(close_button)
        layout.addLayout(row)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(600)
        self.refresh_timer.timeout.connect(self.refreshRequested.emit)
        self.update_result(result)
        self.details.setChecked(not bool(result.get('png')))
        self.text.setVisible(self.details.isChecked())
        self.refresh_button.setEnabled(bool(self.node_id))
        self.auto_refresh.setEnabled(bool(self.node_id))

    def update_result(self, result):
        if result.get('png'):
            if self.image is None:
                self.image = PreviewImage(result['png'])
                self.scroll.setWidget(self.image)
            else:
                self.image.set_data(result['png'])
        self.result = dict(result)
        self.error_message = ''
        self.setWindowTitle('节点预览 · ' + result['title'])
        title = result['title'] + (' · ' + result['task_label'] if result.get('task_label') else '')
        if result.get('time') is not None:
            title += f" · {result['time']:.3f} 秒的静态画面"
        if result.get('png'):
            source = result.get('source_size', [])
            if len(source) == 2:
                title += f' · 源规格 {source[0]}×{source[1]}'
            title += f' · 预览 {self.image.original.width()}×{self.image.original.height()}'
        self.heading.setText(title)
        self.scroll.setVisible(bool(result.get('png')))
        self.quality.setVisible(bool(result.get('png')))
        self.quality_label.setVisible(bool(result.get('png')))
        self.zoom.setVisible(bool(result.get('png')))
        if not result.get('png'):
            self.details.setChecked(True)
        self.text.setPlainText(result['text'])
        self.apply_zoom()
        self.mark_stale(False)

    def apply_zoom(self, _index=None):
        if self.image is not None:
            self.image.native = self.zoom.currentIndex() == 1
            self.scroll.setWidgetResizable(not self.image.native)
            if self.image.native:
                self.image.resize(self.image.original.size())
            self.image.redraw()

    def mark_stale(self, stale, *, valid=True):
        self.stale = bool(stale)
        self.refresh_button.setEnabled(valid and bool(self.node_id) and not self.busy)
        if not valid:
            self.refresh_timer.stop()
            self.status.setText('该节点已不在当前工作流中；保留旧结果，不能刷新。')
        elif not self.busy:
            self.status.setText(self.error_message or ('上游已变化，当前显示旧结果；点击“刷新预览”。' if self.stale else '预览已更新；可适应窗口、100% 查看或最大化。'))
        if valid and self.stale and not self.busy and self.auto_refresh.isChecked():
            self.refresh_timer.start()

    def set_busy(self, busy, message='正在刷新预览…'):
        self.busy = bool(busy)
        self.refresh_timer.stop()
        self.refresh_button.setEnabled(bool(self.node_id) and not self.busy)
        if busy:
            self.status.setText(message)
        else:
            self.mark_stale(self.stale)

    def show_error(self, message):
        with QtCore.QSignalBlocker(self.auto_refresh):
            self.auto_refresh.setChecked(False)
        self.stale = True
        self.error_message = '刷新失败，保留旧结果；自动刷新已暂停：' + str(message)
        self.status.setText(self.error_message)

    def closeEvent(self, event):
        self.refresh_timer.stop()
        super().closeEvent(event)
