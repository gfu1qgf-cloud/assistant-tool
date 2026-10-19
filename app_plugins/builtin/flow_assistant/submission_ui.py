"""Single-item submission UI; picture selection is always tied to a task."""
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from .store import IMAGE_SUFFIXES
from .routing import DURATIONS
from .manual_panel import ManualWorkflowMixin
from .task_ui import TaskSelector
from .pricing import RULE_NOTE


class GenerationOptions(QtWidgets.QWidget):
    def __init__(self, parent=None, *, manual=False):
        super().__init__(parent)
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow('视频模型', QtWidgets.QLabel('Omni 1.1 Flash'))
        self.duration = QtWidgets.QComboBox(self)
        self.duration.addItem('模型默认（通用浏览器）', None)
        for seconds in DURATIONS:
            self.duration.addItem(f'{seconds} 秒', seconds)
        if manual:
            self.duration.setCurrentIndex(self.duration.findData(8))
        self.aspect = QtWidgets.QComboBox(self); self.aspect.addItems(['9:16', '16:9'])
        self.resolution = QtWidgets.QComboBox(self); self.resolution.addItems(['720p', '360p'])
        self.count = QtWidgets.QSpinBox(self); self.count.setRange(1, 20)
        self.budget = QtWidgets.QSpinBox(self); self.budget.setRange(0, 10000)
        self.budget.setSpecialValueText('自动计算积分')
        self.budget.setToolTip(RULE_NOTE + '。非零为单条预算；低于固定费用时不提交。4 秒／模型默认没有固定价格，保留页面读取和手动预算。')
        form.addRow(QtWidgets.QLabel(RULE_NOTE, self))
        for label, widget in [('视频时长', self.duration), ('画面比例', self.aspect),
                              ('分辨率', self.resolution), ('生成数量', self.count), ('单条积分预算', self.budget)]:
            form.addRow(label, widget)

    def values(self):
        return {'duration': self.duration.currentData(), 'aspect': self.aspect.currentText(),
                'resolution': self.resolution.currentText(), 'count': self.count.value(), 'budget': self.budget.value()}

    def set_values(self, values):
        index = self.duration.findData(values.get('duration', 8))
        if index >= 0:
            self.duration.setCurrentIndex(index)
        self.aspect.setCurrentText(values.get('aspect', '9:16'))
        self.resolution.setCurrentText(values.get('resolution', '720p'))
        self.count.setValue(int(values.get('count', 1)))
        self.budget.setValue(int(values.get('budget', 0)))


class FullImagePreview(QtWidgets.QWidget):
    """Fit, never crop. Keep a bounded decoded image rather than a full 8K copy."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QtGui.QImage()
        self.message = '选择任务图片后，在此确认完整画面'
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#202731'))
        if self.image.isNull():
            painter.setPen(QtGui.QColor('#d4dbe5'))
            painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                             QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap, self.message)
            return
        size = self.image.size().scaled(self.size() - QtCore.QSize(12, 12), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        rect = QtCore.QRect(QtCore.QPoint((self.width() - size.width()) // 2, (self.height() - size.height()) // 2), size)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, self.image)


class TaskImagePicker(QtWidgets.QWidget):
    target_changed = QtCore.pyqtSignal(object)
    image_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.task_selector = TaskSelector(self)
        self.target = self.task_selector.combo
        self.task_selector.refresh_requested.connect(self._refresh_tasks)
        layout.addWidget(self.task_selector)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('该任务的图片（先选任务编号，再选图片）')); row.addStretch()
        refresh = QtWidgets.QPushButton('刷新图片', self)
        refresh.clicked.connect(self.load_images); row.addWidget(refresh)
        layout.addLayout(row)
        splitter = QtWidgets.QSplitter(self)
        self.images = QtWidgets.QListWidget(self)
        self.images.setIconSize(QtCore.QSize(74, 68))
        self.images.setMinimumWidth(180)
        self.images.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        splitter.addWidget(self.images)
        self.preview = FullImagePreview(self); splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 650])
        layout.addWidget(splitter, 1)
        self.info = QtWidgets.QLabel('尚未选择图片', self); self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info)
        self.image_path, self.image_stat = '', None
        self.task_selector.changed.connect(self.load_images)
        self.images.currentItemChanged.connect(self.show_image)

    def _refresh_tasks(self):
        panel = self.parentWidget()
        if panel and hasattr(panel, 'dialog'): panel.dialog.refresh_task_catalog()

    def set_targets(self, targets, *, preferred_directory=None):
        self.task_selector.set_targets(targets, preferred_directory=preferred_directory)

    def load_images(self):
        old_image = self.image_path
        self.images.clear()
        target = self.target.currentData()
        self.target_changed.emit(target or {})
        if not target:
            self.show_image(None); return
        try:
            for path in sorted(Path(target['target_dir']).iterdir()):
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    reader = QtGui.QImageReader(str(path)); reader.setAutoTransform(True)
                    size = reader.size()
                    if size.isValid():
                        reader.setScaledSize(size.scaled(74, 68, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
                    item = QtWidgets.QListWidgetItem(QtGui.QIcon(QtGui.QPixmap.fromImage(reader.read())), path.name)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path)); item.setToolTip(str(path))
                    self.images.addItem(item)
            selected = next((self.images.item(i) for i in range(self.images.count())
                             if self.images.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == old_image), None)
            if self.images.count():
                self.images.setCurrentItem(selected or self.images.item(0))
            else:
                self.show_image(None)
        except OSError as error:
            self.show_image(None); self.info.setText(str(error))

    def show_image(self, item, _previous=None):
        self.image_path = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else ''
        self.image_stat = None
        self.preview.image = QtGui.QImage()
        self.preview.message = '此任务没有可用图片，请先放入任务目录，再刷新。'
        if item:
            try:
                stat = Path(self.image_path).stat(); self.image_stat = (stat.st_size, stat.st_mtime_ns)
                reader = QtGui.QImageReader(self.image_path); reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid() and (size.width() > 1600 or size.height() > 1200):
                    reader.setScaledSize(size.scaled(1600, 1200, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
                self.preview.image = reader.read()
                if self.preview.image.isNull():
                    raise ValueError('图片无法解码，未提交。')
                self.info.setText(f'图片：{Path(self.image_path).name}  ·  {size.width()} × {size.height()} px\n{self.image_path}')
            except (OSError, ValueError) as error:
                self.preview.message = str(error); self.info.setText(str(error))
        else:
            self.info.setText('尚未选择图片')
        self.preview.update()
        self.image_changed.emit(self.image_path)

    def selection(self):
        if not self.target.currentData():
            raise ValueError('请先在任务编号下拉框中选择一个已加载的任务。')
        if not self.image_path or self.preview.image.isNull():
            raise ValueError('请先选择并确认一张可预览的任务图片。')
        stat = Path(self.image_path).stat()
        if self.image_stat != (stat.st_size, stat.st_mtime_ns):
            self.show_image(self.images.currentItem())
            raise ValueError('图片刚刚发生变化，已刷新预览；请确认后再次提交。')
        return self.target.currentData(), self.image_path


class ManualSubmitPanel(ManualWorkflowMixin, QtWidgets.QWidget):
    """The reusable picker/options stay here; manual workflow lives separately."""
    pass
