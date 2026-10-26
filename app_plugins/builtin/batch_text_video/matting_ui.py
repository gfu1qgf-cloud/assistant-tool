"""Material matting workspace: local inference, matte finesse and saved assets."""
import copy
from pathlib import Path
import threading
from qt_compat import QtCore, QtGui, QtWidgets
from model.QtThreadLifecycle import connect_thread_finished
from .matting_assets import Catalogue, identity
from .matting_settings import normalize, DEFAULTS
from .matting_models import MattingCancelled, check_cancel
from .matting_render import load_source, infer, refined, save_dynamic as save, qimage
from .matting_canvas import MattingCanvas
from .matting_history import MattingHistory
from .matting_video_ui import MattingVideoController

INFERENCE_KEYS = ('model', 'quality', 'device')


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
        result = dict(serial=request['serial'], path=request['path'], action=request['action'], values=request['values'])
        try:
            action, path, values = request['action'], request['path'], request['values']
            if action == 'load':
                base = load_source(path, self.cancel, request.get('ffmpeg', ''), position=request.get('position', 0))
                result['original'] = qimage(base['source'])
                result.update(video=base.get('video'), expected=base['expected'])
                metadata = self.catalogue.load()['assets'].get(identity(path))
                result['metadata'] = metadata
                try:
                    record = self.catalogue.lookup(path)
                    if record:
                        result['metadata'] = metadata or record
                        if record.get('mode') == 'dynamic':
                            base = infer(path, record['parameters'], models=request.get('models'), cancel=self.cancel,
                                         report=self.message.emit, ffmpeg=request.get('ffmpeg', ''), position=request.get('position', 0))
                            result.update(base=base, base_values=record['parameters'], image=qimage(refined(base, record['parameters'])))
                        else:
                            result['image'] = qimage(load_source(record['result']['path'], self.cancel, request.get('ffmpeg', ''), position=request.get('position', 0))['source'])
                except (ValueError, FileNotFoundError) as error: result['notice'] = str(error)
            elif action in {'infer', 'refine'}:
                base = request.get('base') if action == 'refine' else None
                if base is None:
                    base = infer(path, values, models=request.get('models'), cancel=self.cancel, report=self.message.emit,
                                 ffmpeg=request.get('ffmpeg', ''), position=request.get('position', 0))
                result.update(base=base, original=qimage(base['source']), image=qimage(refined(base, values)))
                result['notice'] = '本地AI设备：'+base['device']+'；画笔可保留／去除区域，滚轮缩放、中键平移。'
            elif action == 'save':
                base = request.get('base') or infer(path, values, models=request.get('models'), cancel=self.cancel,
                                                  report=self.message.emit, ffmpeg=request.get('ffmpeg', ''), position=request.get('position', 0))
                record = save(path, values, self.catalogue, base=base, models=request.get('models'),
                              ffmpeg=request.get('ffmpeg', ''), cancel=self.cancel,
                              report=self.message.emit, progress=self.progress.emit)
                result['record'] = record
                # Cancellation after the atomic publication cannot undo a
                # committed result. Always report it and refresh its owner.
                try:
                    if record.get('mode') == 'dynamic':
                        result.update(base=base, original=qimage(base['source']), image=qimage(refined(base, values)))
                    else: result['image'] = qimage(load_source(record['result']['path'], ffmpeg=request.get('ffmpeg', ''))['source'])
                except (ValueError, OSError): pass
            elif action == 'export':
                from .matting_export import export_dynamic, export_copy
                record = request['record']
                if record.get('mode') == 'dynamic':
                    target = export_dynamic(self.catalogue, record, request['destination'], models=request.get('models'),
                        ffmpeg=request.get('ffmpeg', ''), cancel=self.cancel, report=self.message.emit, progress=self.progress.emit)
                else: target = export_copy(self.catalogue, record, request['destination'])
                result['notice'] = '已另存：'+str(target)
            else: raise ValueError('未知抠像操作。')
            if not result.get('record'): check_cancel(self.cancel)
        except MattingCancelled: result['cancelled'] = True
        except Exception as error: result['error'] = str(error)
        self.ready.emit(result)


class MattingDialog(QtWidgets.QDialog, MattingHistory, MattingVideoController):
    saved = QtCore.pyqtSignal(str)

    def __init__(self, sources=(), parent=None, *, catalogue=None, ffmpeg='', models=None):
        super().__init__(parent)
        from .numeric_wheel import protect_numeric_wheels
        protect_numeric_wheels(self)
        self.catalogue, self.ffmpeg, self.models = catalogue or Catalogue(), ffmpeg, models
        self.path, self.base, self.worker = '', None, None
        self.values, self._drafts, self._pending = normalize(), {}, None
        self._serial, self._loading, self._closing, self._parameters_valid = 0, False, False, True
        self._user_cancelled = False
        self.setWindowTitle('素材抠像工作台 · 保存参数，内存动态抠像')
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
        top.addWidget(QtWidgets.QLabel('按素材保存参数；预览和渲染时动态抠像，无透明视频缓存'))
        root.addLayout(top)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); root.addWidget(split, 1)
        self.files = QtWidgets.QListWidget(); self.files.setMinimumWidth(150); split.addWidget(self.files)
        self.files.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.files.currentItemChanged.connect(self.select_source)
        center = QtWidgets.QWidget(); center_layout = QtWidgets.QVBoxLayout(center); center_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = MattingCanvas(); center_layout.addWidget(self.canvas, 1); self.canvas.strokeAdded.connect(self.add_stroke)
        self.init_video_preview(center_layout)
        row = QtWidgets.QHBoxLayout()
        self.view = QtWidgets.QComboBox()
        for title, value in (('合成／透明棋盘', 'composite'), ('黑白遮罩', 'mask'), ('原始素材', 'original'), ('叠加遮罩', 'overlay')):
            self.view.addItem(title, value)
        self.view.currentIndexChanged.connect(self.change_view); row.addWidget(self.view)
        fit = QtWidgets.QPushButton('适合窗口'); fit.setAutoDefault(False); fit.clicked.connect(self.canvas.fit); row.addWidget(fit)
        center_layout.addLayout(row)
        row = QtWidgets.QHBoxLayout(); row.addWidget(QtWidgets.QLabel('遮罩画笔'))
        self.brush = QtWidgets.QComboBox(self); self.brush.hide()
        self.brush_buttons, self.brush_group = {}, QtWidgets.QButtonGroup(self)
        for title, value in (('浏览', 'view'), ('保留区域＋', 'keep'), ('去除区域－', 'remove')):
            self.brush.addItem(title, value)
            button = QtWidgets.QToolButton(); button.setText(title); button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=value: self.brush.setCurrentIndex(self.brush.findData(value)))
            self.brush_group.addButton(button); self.brush_buttons[value] = button; row.addWidget(button)
        self.brush.currentIndexChanged.connect(self.change_brush)
        self.brush_size = QtWidgets.QSpinBox(); self.brush_size.setRange(1, 500); self.brush_size.setValue(24)
        self.brush_size.setSuffix(' px'); self.brush_size.valueChanged.connect(self.change_brush); row.addWidget(self.brush_size)
        center_layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        undo = QtWidgets.QPushButton('撤销 Ctrl+Z'); redo = QtWidgets.QPushButton('重做 Ctrl+Y')
        clear = QtWidgets.QPushButton('清空涂抹')
        self.undo_button, self.redo_button, self.clear_button = undo, redo, clear
        for button in (undo, redo, clear): button.setAutoDefault(False)
        undo.clicked.connect(self.undo_edit); redo.clicked.connect(self.redo_edit)
        clear.clicked.connect(lambda: self.undo_stroke(all_strokes=True))
        row.addWidget(undo); row.addWidget(redo); row.addWidget(clear); row.addStretch(1); center_layout.addLayout(row)
        self.view_note = QtWidgets.QLabel(); self.view_note.setWordWrap(True); center_layout.addWidget(self.view_note)
        hint = QtWidgets.QLabel('生成AI遮罩后，选择“保留区域＋”或“去除区域－”，按住鼠标左键涂抹；松开算一笔。\n'
            '视频可播放、暂停和拖动定位；逐帧AI预览可能低于原速。暂停后可涂抹，画笔固定在素材坐标，不自动跟踪运动。')
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
            ('denoise', '遮罩降噪', 0, 5, ''), ('grow', '抠像遮罩收缩－／扩展＋', -20, 20, ' px'),
            ('smooth', '边缘引导平滑', 0, 100, ' %'), ('blur', '羽化半径', 0, 20, ' px'),
            ('in_out', '羽化内外比例', -100, 100, ' %'), ('opacity', '遮罩不透明度', 0, 100, ' %'),
            ('cleanup_size', '碎块尺度', 1, 12, ' px'), ('despill_amount', '去色强度', 0, 100, ' %'),
            ('hole_threshold', '补洞前景阈值', 1, 99, ' %'),
            ('despill_width', '去溢色作用范围', 1, 12, ' px'), ('despill_luma', '去色亮度补偿', 0, 100, ' %'),
            ('despill_softness', '去色过渡柔和度', 0, 100, ' %'),
        ):
            control = QtWidgets.QSpinBox() if key in {'denoise', 'cleanup_size', 'despill_width'} else QtWidgets.QDoubleSpinBox()
            if isinstance(control, QtWidgets.QDoubleSpinBox): control.setDecimals(1)
            if key == 'grow': control.setDecimals(2); control.setSingleStep(.25)
            control.setRange(low, high); control.setSuffix(suffix); control.valueChanged.connect(self.parameters_changed)
            self.controls[key] = control; form.addRow(label, control)
        for key, label in (('invert', '反转遮罩'), ('fill_holes', '填补主体内部封闭孔洞'),
                           ('screen_cleanup', '清除边缘／指缝残留幕色'), ('cleanup', '去碎块')):
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
        self.generate = QtWidgets.QPushButton('生成AI遮罩'); self.save_button = QtWidgets.QPushButton('保存参数并用于此素材')
        self.export_button = QtWidgets.QPushButton('另存透明结果…'); self.cancel_button = QtWidgets.QPushButton('取消处理')
        for button in (self.generate, self.save_button, self.export_button, self.cancel_button): button.setAutoDefault(False); row.addWidget(button)
        self.generate.clicked.connect(lambda: self.submit('infer')); self.save_button.clicked.connect(lambda: self.submit('save'))
        self.export_button.clicked.connect(self.export_result); self.cancel_button.clicked.connect(self.cancel_work)
        close = QtWidgets.QPushButton('关闭'); close.setAutoDefault(False); close.clicked.connect(self.close); row.addWidget(close)
        root.addLayout(row)
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(140)
        self.timer.timeout.connect(lambda: self.submit('refine'))
        self.init_matting_history()
        self.change_view(); self.change_brush()
        self.set_values(self.values); self.add_sources(sources); self.refresh_enabled()

    def set_values(self, values):
        normalized = normalize(values)
        self._loading = True; self.values = normalized; self._parameters_valid = True
        for key, control in self.controls.items():
            value = self.values[key]
            if isinstance(control, QtWidgets.QComboBox): control.setCurrentIndex(control.findData(value))
            elif isinstance(control, QtWidgets.QCheckBox): control.setChecked(value)
            else: control.setValue(value)
        self.canvas.strokes = copy.deepcopy(self.values['strokes']); self.canvas.update(); self._loading = False
        self.controls['quality'].setEnabled(self.values['model'] == 'rvm')
        self.controls['cleanup_size'].setEnabled(self.values['cleanup'])
        self.controls['screen_cleanup'].setEnabled(self.values['despill'] != 'none' and self.values['despill_amount'] > 0)

    def parameters_changed(self, *_):
        if self._loading: return
        before = self.values
        values = {**self.values}
        for key, control in self.controls.items():
            values[key] = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                           control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else control.value())
        try: values = normalize(values)
        except ValueError as error:
            self._parameters_valid = False; self.timer.stop(); self._serial += 1; self._pending = None
            self.status.setText(str(error)); self.refresh_enabled(); return
        self._parameters_valid = True
        self.values = values
        self.controls['quality'].setEnabled(values['model'] == 'rvm')
        self.controls['cleanup_size'].setEnabled(values['cleanup'])
        self.controls['screen_cleanup'].setEnabled(values['despill'] != 'none' and values['despill_amount'] > 0)
        control = self.sender()
        key = next((key for key, widget in self.controls.items() if widget is control), None)
        self.record_values(before, values, key if isinstance(control, QtWidgets.QAbstractSpinBox) else None)
        self.queue_preview(before)

    def queue_preview(self, before):
        self.pause_video()
        self._serial += 1; self._pending = None; self._user_cancelled = False
        if any(before[key] != self.values[key] for key in INFERENCE_KEYS):
            self.base = None
            if self.worker: self.worker.cancel.set()
        if self.path:
            self.timer.start()
            self.status.setText('等待基础AI遮罩，完成后应用最新参数…' if self.base is None else '准备更新精修预览…')
        self.refresh_enabled()

    def reset_values(self):
        before = self.values
        self.set_values({**DEFAULTS, **{key: self.values[key] for key in ('model', 'quality', 'device')}})
        self.record_values(before, self.values)
        self.queue_preview(before)

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
        self.reset_video_source(self.path)
        self.select_history()
        self.canvas.trace = self.canvas.pan = None
        self.brush.setCurrentIndex(0)
        from .visual_media import is_animated
        self.set_values(self._drafts.get(identity(self.path), {'model': 'rvm' if is_animated(self.path) else 'isnet'}))
        self.canvas.set_images(QtGui.QImage()); self.submit('load')

    def submit(self, action, **options):
        if not self.path or (action != 'load' and not self._parameters_valid): return
        self.pause_video()
        self.timer.stop(); self._serial += 1; self._user_cancelled = False
        request = dict(action=action, path=self.path, values=copy.deepcopy(self.values), base=self.base,
                       serial=self._serial, models=self.models, ffmpeg=self.ffmpeg, position=self.video_position, **options)
        if self.worker:
            previous = self.worker.request
            reusable = (action == 'refine' and previous['action'] in {'infer', 'refine'}
                        and previous['path'] == request['path']
                        and previous.get('position', 0) == request['position']
                        and all(previous['values'][key] == request['values'][key] for key in INFERENCE_KEYS))
            if not reusable: self.worker.cancel.set()
            self._pending = request
            self.refresh_enabled()
        else: self.start_worker(request)

    def start_worker(self, request):
        self.worker = MattingWorker(request, self.catalogue, self)
        self.worker.ready.connect(self.worker_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.message.connect(self.worker_message, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.progress.connect(self.worker_progress, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.worker, self, 'worker', self.worker_finished)
        self.status.setText({'load': '读取素材与抠像预览…', 'infer': '生成本地AI遮罩…', 'refine': '更新精修预览…',
            'save': '保存素材抠像参数…', 'export': '另存透明文件…'}[request['action']])
        self.progress.setValue(0); self.refresh_enabled(); self.worker.start()

    def worker_message(self, message):
        worker = self.sender()
        if worker and worker.request['serial'] == self._serial: self.status.setText(message)

    def worker_progress(self, value):
        worker = self.sender()
        if worker and worker.request['serial'] == self._serial: self.progress.setValue(value)

    def worker_ready(self, result):
        if result.get('record'): self.saved.emit(result['path'])
        base_values = result.get('base_values', result['values'])
        # An older refinement may supply a reusable raw AI mask, but its
        # rendered image must never replace the user's newer parameter state.
        if (result.get('base') is not None and not result.get('error') and not result.get('cancelled')
                and not self._user_cancelled and result['path'] == self.path
                and abs(result['base'].get('position', 0)-self.video_position) < .000001
                and all(base_values[key] == self.values[key] for key in INFERENCE_KEYS)):
            self.base = result['base']
            if self._pending and self._pending['action'] == 'refine': self._pending['base'] = self.base
        if result['serial'] != self._serial: return
        if result.get('error'): self.status.setText(result['error']); return
        if result.get('cancelled'): self.status.setText('已取消，原素材和已保存结果保留。'); return
        if result['action'] == 'export':
            self.status.setText(result.get('notice', '已另存透明结果。')); return
        if result['action'] == 'load':
            if result.get('video'): self.configure_video(result['video'], result.get('expected'))
            metadata = result.get('metadata')
            if metadata and identity(self.path) not in self._drafts:
                try: self.set_values(metadata['parameters'])
                except (ValueError, KeyError): self.status.setText('已存参数无效，请重新设置。')
        if result.get('base') is not None and all(base_values[key] == self.values[key] for key in INFERENCE_KEYS):
            self.base = result['base']
        original = result.get('original', self.canvas.original)
        self.canvas.set_images(original, result.get('image'))
        if result.get('record'):
            self._drafts[identity(self.path)] = copy.deepcopy(result['record']['parameters'])
            self.status.setText('已保存此素材参数；设计预览、播放预览和正式导出自动使用，逐帧内存抠像，不生成透明MOV缓存。')
        else: self.status.setText(result.get('notice') or ('已读取保存结果；调整参数会自动重建基础遮罩并更新预览。' if result.get('image') else '点击“生成AI遮罩”或调整参数开始预览。'))
        if result['action'] == 'load' and identity(self.path) in self._drafts: self.timer.start()

    def worker_finished(self):
        self.worker.deleteLater(); self.worker = None
        if self._closing: self.close(); return
        pending, self._pending = self._pending, None
        if pending: self.start_worker(pending)
        else:
            if self._user_cancelled: self.status.setText('已取消，原素材和已保存结果保留。')
            self.refresh_enabled()

    def parameters_locked(self):
        if self.video_worker is not None and not self._video_playing: return True
        return self.worker is not None and (self.worker.request['action'] != 'refine'
            or self.worker.request['path'] != self.path or bool(self._pending and self._pending['action'] != 'refine'))

    def refresh_enabled(self):
        busy = self.worker is not None
        video_busy = self.video_worker is not None
        locking = self.parameters_locked()
        self.parameter_page.setEnabled(not locking)
        self.generate.setEnabled(bool(self.path) and not busy and not video_busy and self._parameters_valid)
        self.save_button.setEnabled(bool(self.path) and not busy and not video_busy and self._parameters_valid)
        self.export_button.setEnabled(bool(self.path) and not busy and not video_busy); self.disable_result.setEnabled(bool(self.path) and not busy and not video_busy)
        self.brush.setEnabled(self.base is not None and not locking)
        for mode, button in self.brush_buttons.items():
            button.setEnabled(not locking and (mode == 'view' or (self.base is not None and self._parameters_valid and not self._video_playing)))
        self.brush_size.setEnabled(self.base is not None and not locking)
        self.canvas.editable = self.base is not None and not locking and self._parameters_valid and not self._video_playing
        self.undo_button.setEnabled(bool(self.path) and not locking and (bool(self.history.past) or not self._parameters_valid))
        self.redo_button.setEnabled(bool(self.path) and not locking and self._parameters_valid and bool(self.history.future))
        self.clear_button.setEnabled(self.canvas.editable and bool(self.values['strokes']))
        self.cancel_button.setEnabled(busy or video_busy); self.progress.setVisible(busy)
        self.refresh_video_controls()

    def cancel_work(self):
        self.pause_video()
        self.timer.stop(); self._pending = None; self._serial += 1; self._user_cancelled = True
        if self.worker: self.worker.cancel.set(); self.status.setText('正在取消处理…')
        elif self.video_worker: self.status.setText('正在停止视频预览…')

    def change_view(self):
        self.canvas.view = self.view.currentData(); self.canvas.update()
        self.video_view_changed()
        if hasattr(self, 'view_note'):
            self.view_note.setText('原始素材对比视图；参数效果请切换到合成／透明棋盘或黑白遮罩。' if self.canvas.view == 'original' else
                                   '参数自动更新当前预览；保存后才会应用到资源池和成品。')
    def change_brush(self, *_):
        if self.brush.currentData() != 'view': self.pause_video()
        self.canvas.mode, self.canvas.brush_size = self.brush.currentData(), self.brush_size.value()
        self.brush_buttons[self.canvas.mode].setChecked(True); self.canvas.update()

    def add_stroke(self, stroke):
        try: values = normalize({**self.values, 'strokes': self.values['strokes']+[copy.deepcopy(stroke)]})
        except ValueError as error:
            self.canvas.strokes = copy.deepcopy(self.values['strokes']); self.canvas.update(); self.status.setText(str(error)); return
        before, self.values = self.values, values
        self.record_values(before, values)
        self.queue_preview(before)

    def undo_stroke(self, _checked=False, *, all_strokes=False):
        if not self.values['strokes']: return
        before = self.values
        values = copy.deepcopy(before)
        if all_strokes: values['strokes'].clear()
        else: values['strokes'].pop()
        self.set_values(values); self.record_values(before, values); self.queue_preview(before)

    def disable_saved(self):
        try:
            if self.path and self.catalogue.set_enabled(self.path, False):
                self.saved.emit(self.path); self.status.setText('已停用此素材的已存结果，资源池恢复使用原素材。')
        except (ValueError, OSError) as error: self.status.setText(str(error))

    def export_result(self):
        try: record = self.catalogue.lookup(self.path)
        except (ValueError, FileNotFoundError) as error: self.status.setText(str(error)); return
        if not record: self.status.setText('请先保存此素材的抠像结果，再另存文件。'); return
        from .visual_media import is_animated
        suffix = ('.mov' if is_animated(self.path) else '.png') if record.get('mode') == 'dynamic' else Path(record['result']['path']).suffix
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存透明抠像结果', Path(self.path).stem+'-抠像'+suffix,
            '透明PNG (*.png)' if suffix == '.png' else '透明MOV (*.mov)')
        if not destination: return
        self.submit('export', record=record, destination=destination)

    def enable_saved(self):
        if self.worker or not self.path: return
        try:
            if self.catalogue.set_enabled(self.path, True):
                self.saved.emit(self.path); self.submit('load')
            else: self.status.setText('此素材还没有已保存的抠像结果。')
        except (ValueError, OSError) as error: self.status.setText(str(error))

    def closeEvent(self, event):
        if self.worker or self.video_worker:
            self._closing = True; self.cancel_work(); event.ignore(); return
        self.timer.stop(); self.video_seek_timer.stop(); event.accept()

    def showEvent(self, event):
        self._closing = False
        super().showEvent(event)

    def hideEvent(self, event):
        self.pause_video()
        super().hideEvent(event)

    def reject(self):
        self.close()
