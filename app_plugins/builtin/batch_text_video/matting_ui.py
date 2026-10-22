"""Material matting workspace: local inference, matte finesse and saved assets."""
import copy
from pathlib import Path
import threading
from qt_compat import QtCore, QtGui, QtWidgets
from model.QtThreadLifecycle import connect_thread_finished
from .matting_assets import Catalogue, identity
from .matting_settings import normalize, DEFAULTS
from .matting_models import MattingCancelled, check_cancel
from .matting_render import load_source, infer, refined, save, qimage
from .matting_canvas import MattingCanvas


class MattingWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    message = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int)

    def __init__(self, request, catalogue, parent=None):
        super().__init__(parent)
        self.request, self.catalogue = request, catalogue
        self.cancel = threading.Event()

    def run(self):
        request = self.request
        result = dict(serial=request['serial'], path=request['path'], action=request['action'])
        try:
            action, path, values = request['action'], request['path'], request['values']
            if action == 'load':
                base = load_source(path, self.cancel, request.get('ffmpeg', ''))
                result['original'] = qimage(base['source'])
                metadata = self.catalogue.load()['assets'].get(identity(path))
                result['metadata'] = metadata
                try:
                    record = self.catalogue.lookup(path)
                    if record:
                        result['metadata'] = metadata or record
                        result['image'] = qimage(load_source(record['result']['path'], self.cancel, request.get('ffmpeg', ''))['source'])
                except (ValueError, FileNotFoundError) as error: result['notice'] = str(error)
            elif action in {'infer', 'refine'}:
                base = request.get('base') if action == 'refine' else None
                if base is None:
                    base = infer(path, values, models=request.get('models'), cancel=self.cancel, report=self.message.emit,
                                 ffmpeg=request.get('ffmpeg', ''))
                result.update(base=base, original=qimage(base['source']), image=qimage(refined(base, values)))
                result['notice'] = '本地AI设备：'+base['device']+'；画笔可保留／去除区域，滚轮缩放、中键平移。'
            elif action == 'save':
                record = save(path, values, self.catalogue, base=request.get('base'), models=request.get('models'),
                              ffmpeg=request.get('ffmpeg', ''), cancel=self.cancel,
                              report=self.message.emit, progress=self.progress.emit)
                result['record'] = record
                # Cancellation after the atomic publication cannot undo a
                # committed result. Always report it and refresh its owner.
                try: result['image'] = qimage(load_source(record['result']['path'], ffmpeg=request.get('ffmpeg', ''))['source'])
                except (ValueError, OSError): pass
            else: raise ValueError('未知抠像操作。')
            if not result.get('record'): check_cancel(self.cancel)
        except MattingCancelled: result['cancelled'] = True
        except Exception as error: result['error'] = str(error)
        self.ready.emit(result)


class MattingDialog(QtWidgets.QDialog):
    saved = QtCore.pyqtSignal(str)

    def __init__(self, sources=(), parent=None, *, catalogue=None, ffmpeg='', models=None):
        super().__init__(parent)
        self.catalogue, self.ffmpeg, self.models = catalogue or Catalogue(), ffmpeg, models
        self.path, self.base, self.worker = '', None, None
        self.values, self._drafts, self._pending = normalize(), {}, None
        self._serial, self._loading, self._closing, self._parameters_valid = 0, False, False, True
        self.setWindowTitle('素材抠像工作台 · 图片／视频独立保存')
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowMinMaxButtonsHint)
        self.resize(1320, 880)
        root = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton('添加图片／视频…'); add.setAutoDefault(False); add.clicked.connect(self.choose_sources)
        self.disable_result = QtWidgets.QPushButton('停用此素材的已存结果')
        self.disable_result.setAutoDefault(False); self.disable_result.clicked.connect(self.disable_saved)
        enable = QtWidgets.QPushButton('启用已存结果'); enable.setAutoDefault(False)
        enable.clicked.connect(self.enable_saved)
        top.addWidget(add); top.addWidget(self.disable_result); top.addWidget(enable); top.addStretch(1)
        top.addWidget(QtWidgets.QLabel('结果按素材保存，资源池轮换时自动读取'))
        root.addLayout(top)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); root.addWidget(split, 1)
        self.files = QtWidgets.QListWidget(); self.files.setMinimumWidth(150); split.addWidget(self.files)
        self.files.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.files.currentItemChanged.connect(self.select_source)
        center = QtWidgets.QWidget(); center_layout = QtWidgets.QVBoxLayout(center); center_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = MattingCanvas(); center_layout.addWidget(self.canvas, 1); self.canvas.strokeAdded.connect(self.add_stroke)
        row = QtWidgets.QHBoxLayout()
        self.view = QtWidgets.QComboBox()
        for title, value in (('合成／透明棋盘', 'composite'), ('黑白遮罩', 'mask'), ('原始素材', 'original'), ('叠加遮罩', 'overlay')):
            self.view.addItem(title, value)
        self.view.currentIndexChanged.connect(self.change_view); row.addWidget(self.view)
        self.brush = QtWidgets.QComboBox()
        for title, value in (('浏览', 'view'), ('保留画笔＋', 'keep'), ('去除画笔－', 'remove')): self.brush.addItem(title, value)
        self.brush.currentIndexChanged.connect(self.change_brush); row.addWidget(self.brush)
        self.brush_size = QtWidgets.QSpinBox(); self.brush_size.setRange(1, 500); self.brush_size.setValue(24)
        self.brush_size.setSuffix(' px'); self.brush_size.valueChanged.connect(self.change_brush); row.addWidget(self.brush_size)
        fit = QtWidgets.QPushButton('适合窗口'); fit.setAutoDefault(False); fit.clicked.connect(self.canvas.fit); row.addWidget(fit)
        center_layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        undo = QtWidgets.QPushButton('撤销笔画'); clear = QtWidgets.QPushButton('清空笔画')
        self.undo_button, self.clear_button = undo, clear
        for button in (undo, clear): button.setAutoDefault(False)
        undo.clicked.connect(self.undo_stroke); clear.clicked.connect(lambda: self.undo_stroke(all_strokes=True))
        row.addWidget(undo); row.addWidget(clear); row.addStretch(1); center_layout.addLayout(row)
        hint = QtWidgets.QLabel('画笔修正为手工保留／去除。视频预览显示首帧，画笔固定在素材坐标，不自动跟踪运动。')
        hint.setWordWrap(True); center_layout.addWidget(hint); split.addWidget(center)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setMinimumWidth(280)
        self.parameter_page = QtWidgets.QWidget(); form = QtWidgets.QFormLayout(self.parameter_page)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.controls = {}
        for key, label, choices in (
            ('model', 'AI模型', [('通用主体 · 图片优先', 'isnet'), ('人物 · 视频时间记忆', 'rvm')]),
            ('quality', '人物推理质量', [('快速', 'fast'), ('精细', 'better')]),
            ('device', '运行设备', [('自动选择', 'auto'), ('CPU', 'cpu'), ('CUDA显卡', 'cuda')]),
            ('despill', '边缘去色', [('关闭', 'none'), ('去绿色', 'green'), ('去蓝色', 'blue')]),
        ):
            control = QtWidgets.QComboBox()
            for title, value in choices: control.addItem(title, value)
            control.currentIndexChanged.connect(self.parameters_changed)
            self.controls[key] = control; form.addRow(label, control)
        for key, label, low, high, suffix in (
            ('black_clip', '黑色截断', 0, 99.9, ' %'), ('white_clip', '白色截断', .1, 100, ' %'),
            ('clean_black', '清理黑色区域', 0, 100, ' %'), ('clean_white', '清理白色区域', 0, 100, ' %'),
            ('denoise', '遮罩降噪', 0, 5, ''), ('grow', '收缩－／扩张＋', -20, 20, ' px'),
            ('smooth', '边缘引导平滑', 0, 100, ' %'), ('blur', '羽化半径', 0, 20, ' px'),
            ('in_out', '羽化内外比例', -100, 100, ' %'), ('opacity', '遮罩不透明度', 0, 100, ' %'),
            ('cleanup_size', '碎块尺度', 1, 12, ' px'), ('despill_amount', '去色强度', 0, 100, ' %'),
            ('despill_width', '去色边缘范围', 1, 12, ' px'), ('despill_luma', '去色亮度补偿', 0, 100, ' %'),
        ):
            control = QtWidgets.QSpinBox() if key in {'denoise', 'grow', 'cleanup_size', 'despill_width'} else QtWidgets.QDoubleSpinBox()
            if isinstance(control, QtWidgets.QDoubleSpinBox): control.setDecimals(1)
            control.setRange(low, high); control.setSuffix(suffix); control.valueChanged.connect(self.parameters_changed)
            self.controls[key] = control; form.addRow(label, control)
        for key, label in (('invert', '反转遮罩'), ('cleanup', '去碎块')):
            control = QtWidgets.QCheckBox(label); control.toggled.connect(self.parameters_changed)
            self.controls[key] = control; form.addRow(control)
        reset = QtWidgets.QPushButton('重置精修参数'); reset.setAutoDefault(False); reset.clicked.connect(self.reset_values); form.addRow(reset)
        help_text = QtWidgets.QLabel('参数借鉴专业遮罩精修的操作方式。这里采用独立的本地模型与算法。\n'
            '通用图片模型固定1024分析尺寸；人物精细模式提高内部推理分辨率。\n'
            '负的内外比例偏向向内羽化，正值偏向向外。源素材原本透明的像素始终保留透明。')
        help_text.setWordWrap(True); help_text.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(help_text); scroll.setWidget(self.parameter_page); split.addWidget(scroll); split.setSizes([190, 800, 300])
        self.status = QtWidgets.QLabel('首次生成时下载经校验的本地模型，不上传素材。'); self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.progress = QtWidgets.QProgressBar(); self.progress.setRange(0, 100); self.progress.hide(); root.addWidget(self.progress)
        row = QtWidgets.QHBoxLayout()
        self.generate = QtWidgets.QPushButton('生成AI遮罩'); self.save_button = QtWidgets.QPushButton('保存结果并用于此素材')
        self.export_button = QtWidgets.QPushButton('另存透明结果…'); self.cancel_button = QtWidgets.QPushButton('取消处理')
        for button in (self.generate, self.save_button, self.export_button, self.cancel_button): button.setAutoDefault(False); row.addWidget(button)
        self.generate.clicked.connect(lambda: self.submit('infer')); self.save_button.clicked.connect(lambda: self.submit('save'))
        self.export_button.clicked.connect(self.export_result); self.cancel_button.clicked.connect(self.cancel_work)
        close = QtWidgets.QPushButton('关闭'); close.setAutoDefault(False); close.clicked.connect(self.close); row.addWidget(close)
        root.addLayout(row)
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(140)
        self.timer.timeout.connect(lambda: self.submit('refine') if self.base is not None else None)
        self.set_values(self.values); self.add_sources(sources); self.refresh_enabled()

    def set_values(self, values):
        normalized = normalize(values)
        self._loading = True; self.values = normalized; self._parameters_valid = True
        for key, control in self.controls.items():
            value = self.values[key]
            if isinstance(control, QtWidgets.QComboBox): control.setCurrentIndex(control.findData(value))
            elif isinstance(control, QtWidgets.QCheckBox): control.setChecked(value)
            else: control.setValue(value)
        self.canvas.strokes = copy.deepcopy(self.values['strokes']); self._loading = False
        self.controls['quality'].setEnabled(self.values['model'] == 'rvm')
        self.controls['cleanup_size'].setEnabled(self.values['cleanup'])

    def parameters_changed(self, *_):
        if self._loading: return
        before = self.values
        values = {**self.values}
        for key, control in self.controls.items():
            values[key] = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                           control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else control.value())
        try: values = normalize(values)
        except ValueError as error:
            self._parameters_valid = False; self.timer.stop(); self.status.setText(str(error)); self.refresh_enabled(); return
        self._parameters_valid = True
        self.values = values
        self.controls['quality'].setEnabled(values['model'] == 'rvm')
        self.controls['cleanup_size'].setEnabled(values['cleanup'])
        if any(before[k] != values[k] for k in ('model', 'quality', 'device')):
            self._serial += 1; self._pending = None
            if self.worker: self.worker.cancel.set()
            self.base = None; self.status.setText('模型设置已变化，请重新生成AI遮罩。')
        elif self.base is not None: self.timer.start()
        else: self.status.setText('点击“生成AI遮罩”后，可预览这些精修参数。')
        self.refresh_enabled()

    def reset_values(self):
        self.set_values({**DEFAULTS, **{key: self.values[key] for key in ('model', 'quality', 'device')}})
        if self.base is not None: self.timer.start()
        self.refresh_enabled()

    def choose_sources(self):
        from .visual_media import VISUAL_FILTER
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '选择需要单独抠像的素材', '', VISUAL_FILTER)
        self.add_sources(paths)

    def add_sources(self, paths):
        known = {identity(self.files.item(i).data(QtCore.Qt.ItemDataRole.UserRole)) for i in range(self.files.count())}
        for path in paths:
            if not path or identity(path) in known: continue
            known.add(identity(path)); item = QtWidgets.QListWidgetItem(Path(path).name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path)); item.setToolTip(str(path)); self.files.addItem(item)
        if self.files.currentRow() < 0 and self.files.count(): self.files.setCurrentRow(0)

    def select_source(self, item, _previous=None):
        if item is None: return
        if self.path: self._drafts[identity(self.path)] = copy.deepcopy(self.values)
        self.path = item.data(QtCore.Qt.ItemDataRole.UserRole); self.base = None; self.timer.stop(); self.canvas.fit()
        self.canvas.trace = self.canvas.pan = None
        self.brush.setCurrentIndex(0)
        from .visual_media import is_animated
        self.set_values(self._drafts.get(identity(self.path), {'model': 'rvm' if is_animated(self.path) else 'isnet'}))
        self.canvas.set_images(QtGui.QImage()); self.submit('load')

    def submit(self, action):
        if not self.path or (action != 'load' and not self._parameters_valid): return
        self.timer.stop(); self._serial += 1
        request = dict(action=action, path=self.path, values=copy.deepcopy(self.values), base=self.base,
                       serial=self._serial, models=self.models, ffmpeg=self.ffmpeg)
        if self.worker:
            self.worker.cancel.set(); self._pending = request
        else: self.start_worker(request)

    def start_worker(self, request):
        self.worker = MattingWorker(request, self.catalogue, self)
        self.worker.ready.connect(self.worker_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.message.connect(self.status.setText, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.progress.connect(self.progress.setValue, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.worker, self, 'worker', self.worker_finished)
        self.status.setText({'load': '读取素材…', 'infer': '生成本地AI遮罩…', 'refine': '更新精修预览…', 'save': '保存素材抠像结果…'}[request['action']])
        self.progress.setValue(0); self.refresh_enabled(); self.worker.start()

    def worker_ready(self, result):
        if result.get('record'): self.saved.emit(result['path'])
        if result['serial'] != self._serial: return
        if result.get('error'): self.status.setText(result['error']); return
        if result.get('cancelled'): self.status.setText('已取消，原素材和已保存结果保留。'); return
        if result['action'] == 'load':
            metadata = result.get('metadata')
            if metadata and identity(self.path) not in self._drafts:
                try: self.set_values(metadata['parameters'])
                except (ValueError, KeyError): self.status.setText('已存参数无效，请重新设置。')
        if result.get('base') is not None: self.base = result['base']
        original = result.get('original', self.canvas.original)
        self.canvas.set_images(original, result.get('image'))
        if result.get('record'):
            self._drafts[identity(self.path)] = copy.deepcopy(result['record']['parameters'])
            self.status.setText('已保存并关联此素材；池轮换和正式导出自动读取该结果。')
        else: self.status.setText(result.get('notice') or ('已读取保存结果，可重新生成后调整。' if result.get('image') else '点击“生成AI遮罩”开始抠像。'))

    def worker_finished(self):
        self.worker.deleteLater(); self.worker = None
        if self._closing: self.close(); return
        pending, self._pending = self._pending, None
        if pending: self.start_worker(pending)
        else: self.refresh_enabled()

    def refresh_enabled(self):
        busy = self.worker is not None
        locking = busy and self.worker.request['action'] != 'refine'
        self.parameter_page.setEnabled(not locking)
        self.generate.setEnabled(bool(self.path) and not busy and self._parameters_valid)
        self.save_button.setEnabled(bool(self.path) and not busy and self._parameters_valid)
        self.export_button.setEnabled(bool(self.path) and not busy); self.disable_result.setEnabled(bool(self.path) and not busy)
        self.brush.setEnabled(self.base is not None and not locking)
        self.canvas.editable = self.base is not None and not locking and self._parameters_valid
        self.undo_button.setEnabled(self.canvas.editable); self.clear_button.setEnabled(self.canvas.editable)
        self.cancel_button.setEnabled(busy); self.progress.setVisible(busy)

    def cancel_work(self):
        self.timer.stop(); self._pending = None
        if self.worker: self.worker.cancel.set(); self.status.setText('正在取消处理…')

    def change_view(self): self.canvas.view = self.view.currentData(); self.canvas.update()
    def change_brush(self, *_):
        self.canvas.mode, self.canvas.brush_size = self.brush.currentData(), self.brush_size.value(); self.canvas.update()

    def add_stroke(self, stroke):
        try: values = normalize({**self.values, 'strokes': self.values['strokes']+[copy.deepcopy(stroke)]})
        except ValueError as error:
            self.canvas.strokes = copy.deepcopy(self.values['strokes']); self.canvas.update(); self.status.setText(str(error)); return
        self.values = values; self.timer.start()

    def undo_stroke(self, _checked=False, *, all_strokes=False):
        if not self.values['strokes']: return
        if all_strokes: self.values['strokes'].clear()
        else: self.values['strokes'].pop()
        self.canvas.strokes = copy.deepcopy(self.values['strokes']); self.canvas.update()
        if self.base is not None: self.timer.start()

    def disable_saved(self):
        try:
            if self.path and self.catalogue.set_enabled(self.path, False):
                self.saved.emit(self.path); self.status.setText('已停用此素材的已存结果，资源池恢复使用原素材。')
        except (ValueError, OSError) as error: self.status.setText(str(error))

    def export_result(self):
        try: record = self.catalogue.lookup(self.path)
        except (ValueError, FileNotFoundError) as error: self.status.setText(str(error)); return
        if not record: self.status.setText('请先保存此素材的抠像结果，再另存文件。'); return
        suffix = Path(record['result']['path']).suffix
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存透明抠像结果', Path(self.path).stem+'-抠像'+suffix,
            '透明PNG (*.png)' if suffix == '.png' else '透明MOV (*.mov)')
        if not destination: return
        from .matting_export import export_copy
        try: destination = export_copy(self.catalogue, record, destination)
        except (ValueError, OSError) as error: self.status.setText(str(error)); return
        self.status.setText('已另存：'+str(destination))

    def enable_saved(self):
        if self.worker or not self.path: return
        try:
            if self.catalogue.set_enabled(self.path, True):
                self.saved.emit(self.path); self.submit('load')
            else: self.status.setText('此素材还没有已保存的抠像结果。')
        except (ValueError, OSError) as error: self.status.setText(str(error))

    def closeEvent(self, event):
        if self.worker:
            self._closing = True; self.cancel_work(); event.ignore(); return
        self.timer.stop(); event.accept()

    def showEvent(self, event):
        self._closing = False
        super().showEvent(event)

    def reject(self):
        self.close()
