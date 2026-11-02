from qt_compat import QtWidgets
from model.QtThreadLifecycle import thread_is_active


class VideoSorterDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle('分拣视频 · 按任务图片匹配')
        self.resize(760, 480)
        config = plugin.context.load_config()
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel('按图片特征匹配视频，并将匹配的视频移动到对应任务图片所在目录。关闭本窗口不停止分拣。')
        note.setWordWrap(True); layout.addWidget(note)
        form = QtWidgets.QFormLayout(); layout.addLayout(form)
        self.source = QtWidgets.QLineEdit(str(config.get('video_sorting_station_path') or ''))
        self.target = QtWidgets.QLineEdit(str(plugin.context.current_project_directory(require_loaded=False) or ''))
        self.inputs = []
        for title, edit in [('视频来源目录', self.source), ('任务图片目录', self.target)]:
            row = QtWidgets.QWidget(self); horizontal = QtWidgets.QHBoxLayout(row); horizontal.setContentsMargins(0, 0, 0, 0)
            button = QtWidgets.QPushButton('选择…'); horizontal.addWidget(edit, 1); horizontal.addWidget(button)
            button.clicked.connect(lambda _checked=False, field=edit: self.choose(field))
            self.inputs.extend((edit, button)); form.addRow(title, row)
        self.threshold = QtWidgets.QDoubleSpinBox(); self.threshold.setRange(0, 1); self.threshold.setDecimals(3)
        self.threshold.setSingleStep(.005)
        try: value = float(config.get('video_match_ratio_threshold', .03))
        except (TypeError, ValueError): value = .03
        self.threshold.setValue(min(1, max(0, value))); self.inputs.append(self.threshold)
        form.addRow('匹配阈值', self.threshold)
        actions = QtWidgets.QHBoxLayout(); layout.addLayout(actions)
        self.run_button = QtWidgets.QPushButton('分拣视频'); self.run_button.clicked.connect(self.run)
        self.save_button = QtWidgets.QPushButton('保存来源目录和阈值为默认值'); self.save_button.clicked.connect(self.save_defaults)
        actions.addWidget(self.run_button); actions.addWidget(self.save_button)
        self.inputs.append(self.save_button)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.document().setMaximumBlockCount(200)
        layout.addWidget(self.log, 1)

    def choose(self, field):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, '选择目录', field.text())
        if path: field.setText(path)

    def run(self):
        if not thread_is_active(self.plugin.assign_video_thread):
            if not self.source.text().strip() or not self.target.text().strip():
                QtWidgets.QMessageBox.warning(self, '请选择目录', '请先选择视频来源目录和任务图片目录。'); return
        try:
            self.plugin.assignVideo()
        except (Exception, SystemExit) as error:
            if not thread_is_active(self.plugin.assign_video_thread):
                self.busy(False); self.run_button.setEnabled(True); self.run_button.setText('分拣视频')
            QtWidgets.QMessageBox.warning(self, '分拣未启动', str(error))

    def busy(self, value):
        for widget in self.inputs: widget.setEnabled(not value)

    def save_defaults(self):
        updates = {'video_sorting_station_path': self.source.text().strip(), 'video_match_ratio_threshold': self.threshold.value()}
        try:
            config = self.plugin.context.load_config()
            if self.plugin.context.patch_config(updates, expected=config) is False:
                self.log.appendPlainText('默认设置未保存，请检查程序日志。'); return
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, '默认设置未保存', str(error)); return
        self.log.appendPlainText('来源目录和阈值已保存；任务图片目录不作为全局默认值保存。')
