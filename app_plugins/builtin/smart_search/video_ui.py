"""GUI-owned local video search; heavyweight indexing uses host jobs."""
import logging
from pathlib import Path
import time
from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import directory_resource
from model.InventoryManager import InventoryStore
from model.MediaPlayback import FfplayReviewSurface
from model.QtThreadLifecycle import connect_thread_finished
from model.image_retrieval.encoder import ChineseImageEncoder, MODEL_SPECS, combine_search_vectors
from model.video_retrieval.frames import resolve_tools
from model.video_retrieval.index import VideoSearchIndex
from .settings import VideoSettingsPage, normalize_video_settings
from .video_jobs import run_video_index_job

log = logging.getLogger(__name__)


def time_label(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


def fitted_icon(path):
    source = QtGui.QPixmap(str(path))
    canvas = QtGui.QPixmap(240, 160); canvas.fill(QtCore.Qt.GlobalColor.transparent)
    if not source.isNull():
        source = source.scaled(240, 160, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                               QtCore.Qt.TransformationMode.SmoothTransformation)
        painter = QtGui.QPainter(canvas)
        painter.drawPixmap((240 - source.width()) // 2, (160 - source.height()) // 2, source)
        painter.end()
    return QtGui.QIcon(canvas)


class Results(QtWidgets.QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.setIconSize(QtCore.QSize(240, 160)); self.setGridSize(QtCore.QSize(266, 219))
        self.setSpacing(8); self.setWordWrap(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True); self.setAcceptDrops(False)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)

    def mimeData(self, items):
        mime = QtCore.QMimeData()
        paths = [item.data(QtCore.Qt.ItemDataRole.UserRole)['path'] for item in items]
        mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in dict.fromkeys(paths) if Path(path).is_file()])
        return mime

    def startDrag(self, _actions):
        mime = self.mimeData(self.selectedItems())
        if mime.hasUrls():
            drag = QtGui.QDrag(self); drag.setMimeData(mime)
            drag.exec(QtCore.Qt.DropAction.CopyAction, QtCore.Qt.DropAction.CopyAction)


class ReferenceEdit(QtWidgets.QLineEdit):
    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                self.setText(url.toLocalFile()); event.acceptProposedAction(); return
        super().dropEvent(event)


class QueryWorker(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, task, parent):
        super().__init__(parent); self.task = task

    def run(self):
        try:
            result = self.task()
            if not self.isInterruptionRequested():
                self.completed.emit(result)
        except Exception as error:
            log.exception('智能搜视频查询失败')
            self.failed.emit(f'{type(error).__name__}: {error}')


class VideoSearchPanel(QtWidgets.QWidget):
    settingsChanged = QtCore.pyqtSignal(object)

    def __init__(self, settings, context, parent=None, index=None, encoder=None, store=None):
        super().__init__(parent)
        self.context = context; self.settings = normalize_video_settings(settings)
        self.encoder = encoder or ChineseImageEncoder(self.settings['model'])
        self.index = index or VideoSearchIndex(model_key=self.settings['model'])
        self.store = store or InventoryStore()
        self.worker = None; self._shutting_down = False; self._pending_settings = None
        self._rows = []; self._shown = 0; self._errors = []; self._preview_duration = 0
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel('中文描述或参考图片搜索视频画面，命中帧可直接预览。首次建立索引后，搜索不重新扫描视频。原文件只读。')
        note.setWordWrap(True); layout.addWidget(note)
        row = QtWidgets.QHBoxLayout(); self.model = QtWidgets.QComboBox()
        for key, spec in MODEL_SPECS.items(): self.model.addItem(spec['label'], key)
        self.model.setCurrentIndex(self.model.findData(self.settings['model']))
        self.model.currentIndexChanged.connect(self._model_changed); row.addWidget(self.model)
        self.count_label = QtWidgets.QLabel(); row.addWidget(self.count_label, 1)
        self.settings_button = QtWidgets.QPushButton('视频库 / 索引设置…')
        self.settings_button.clicked.connect(self.edit_settings); row.addWidget(self.settings_button)
        self.index_button = QtWidgets.QPushButton('建立 / 更新索引')
        self.index_button.clicked.connect(self.update_index); row.addWidget(self.index_button)
        self.cancel_button = QtWidgets.QPushButton('停止'); self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_work); row.addWidget(self.cancel_button); layout.addLayout(row)
        row = QtWidgets.QHBoxLayout(); self.query = QtWidgets.QLineEdit()
        self.query.setPlaceholderText('例如：洪水、祈祷的人、海上的小船…')
        self.query.returnPressed.connect(self.search); row.addWidget(self.query, 1)
        self.search_button = QtWidgets.QPushButton('搜索视频'); self.search_button.clicked.connect(self.search)
        row.addWidget(self.search_button); layout.addLayout(row)
        row = QtWidgets.QHBoxLayout(); row.addWidget(QtWidgets.QLabel('参考图片'))
        self.reference = ReferenceEdit(); self.reference.setAcceptDrops(True)
        self.reference.setPlaceholderText('可选：拖入图片；有文字时综合排序，清空则只按文字搜索')
        row.addWidget(self.reference, 1)
        choose = QtWidgets.QPushButton('选择…'); choose.clicked.connect(self.choose_reference)
        clear = QtWidgets.QPushButton('清空'); clear.clicked.connect(self.reference.clear)
        row.addWidget(choose); row.addWidget(clear); layout.addLayout(row)
        self.progress = QtWidgets.QProgressBar(); self.progress.hide(); layout.addWidget(self.progress)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.results = Results(); self.results.itemDoubleClicked.connect(lambda _item: self.preview_selected())
        self.results.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self.context_menu); self.splitter.addWidget(self.results)
        preview = QtWidgets.QWidget(); preview_layout = QtWidgets.QVBoxLayout(preview)
        self.player = FfplayReviewSurface(parent=preview); preview_layout.addWidget(self.player, 1)
        if hasattr(self.player.backend, 'placeholder'):
            self.player.backend.placeholder.setText('双击结果，从命中位置预览视频')
        self.preview_info = QtWidgets.QLabel('双击结果：从命中位置播放'); self.preview_info.setWordWrap(True)
        preview_layout.addWidget(self.preview_info)
        self.seek = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); self.seek.setRange(0, 1000)
        self.seek.setEnabled(False); self.seek.sliderReleased.connect(self.seek_preview); preview_layout.addWidget(self.seek)
        self.player.positionChanged.connect(self._position_changed)
        self.status = QtWidgets.QLabel('请先设置视频库，再建立索引。可多选拖入达芬奇，不移动原视频。')
        self.player.errorOccurred.connect(lambda message: self.status.setText('预览：' + message))
        row = QtWidgets.QHBoxLayout()
        for title, callback in (('播放', self.player.play), ('暂停', lambda: self.player.pause()), ('停止', self.stop_preview)):
            button = QtWidgets.QPushButton(title); button.clicked.connect(callback); row.addWidget(button)
        preview_layout.addLayout(row); self.splitter.addWidget(preview)
        self.splitter.setSizes([740, 340]); layout.addWidget(self.splitter, 1)
        row = QtWidgets.QHBoxLayout()
        self.more = QtWidgets.QPushButton('加载更多'); self.more.clicked.connect(self.append_page); self.more.hide(); row.addWidget(self.more)
        self.details_button = QtWidgets.QPushButton('查看索引失败详情'); self.details_button.clicked.connect(self.show_errors)
        self.details_button.hide(); row.addWidget(self.details_button); row.addStretch(); layout.addLayout(row)
        self.status.setWordWrap(True); layout.addWidget(self.status); self.refresh_count()

    def refresh_count(self):
        videos, frames = self.index.count(); self.count_label.setText(f'{videos:,} 个视频 · {frames:,} 个代表帧')

    def update_settings(self, settings):
        settings = normalize_video_settings(settings)
        if self.is_busy(): self._pending_settings = settings; return
        if settings['model'] != self.settings['model']:
            self.encoder.release(); self.index.release()
            self.encoder = ChineseImageEncoder(settings['model']); self.index = VideoSearchIndex(model_key=settings['model'])
            self.show_results([])
        self.settings = settings; blocker = QtCore.QSignalBlocker(self.model)
        self.model.setCurrentIndex(self.model.findData(settings['model'])); del blocker
        self.refresh_count()

    def _model_changed(self):
        self.update_settings({**self.settings, 'model': self.model.currentData()}); self.settingsChanged.emit(self.settings)

    def edit_settings(self):
        if self.is_busy(): return
        dialog = QtWidgets.QDialog(self); dialog.setWindowTitle('视频库与索引设置'); dialog.resize(610, 680)
        layout = QtWidgets.QVBoxLayout(dialog); page = VideoSettingsPage(dialog)
        page.load_config({'smart_video_search': self.settings}); layout.addWidget(page.widget)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.update_settings(page.values()); self.settingsChanged.emit(self.settings)

    def choose_reference(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择参考图片', '', '图片 (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.jfif)')
        if path: self.reference.setText(path)

    def _confirm_model(self):
        if self.encoder.model_is_cached(): return True
        spec = MODEL_SPECS[self.settings['model']]
        return QtWidgets.QMessageBox.question(self, '首次下载视觉模型',
            f"需要下载 {spec['label']}（{spec['download']}）。与搜图共用缓存，不会上传素材。继续吗？") == QtWidgets.QMessageBox.StandardButton.Yes

    def _roots(self):
        roots = list(self.settings['library_roots'])
        if self.settings['include_inventory']:
            state = self.store.load()
            roots += [str(item['path']) for item in state.get('materials', []) if item.get('path')]
            for item in state.get('people', []):
                path = item.get('material_path') or (str(Path(item['path']) / '素材') if item.get('path') else '')
                if path: roots.append(str(path))
        return list(dict.fromkeys(roots))

    def update_index(self):
        if self.is_busy() or self._shutting_down: return
        try:
            roots = self._roots()
            if not roots: raise ValueError('没有视频库。请点击“视频库 / 索引设置…”添加目录，或在库存中添加素材。')
            if not self._confirm_model(): return
            payload = {'roots': roots, 'settings': dict(self.settings), 'index_root': str(self.index.root)}
            self.worker = self.context.submit_background_task(title='智能搜索 · 更新视频索引', runner=run_video_index_job,
                payload=payload, kind='model', execution='parallel',
                resources=('model:chinese-clip-video-index', directory_resource(self.index.root)), dedup_key='video-index:' + str(self.index.path))
            self.worker.progressChanged.connect(self._progress); self.worker.completed.connect(self._indexed)
            self.worker.failed.connect(self.failed); self.worker.finished.connect(self._finished)
            self.worker.set_result_handler(lambda: self._show_index_result())
            self.set_busy(True); self.status.setText('已提交到主程序后台任务；关闭窗口仍继续。')
        except Exception as error:
            log.exception('视频索引提交失败'); self.failed(str(error))

    def _progress(self, message, percent):
        self.status.setText(message); self.progress.setRange(0, 0 if percent is None else 100)
        if percent is not None: self.progress.setValue(percent)

    def _indexed(self, result):
        self.index.release(); self.refresh_count(); self._errors = result.get('errors', [])
        self.details_button.setVisible(bool(self._errors))
        self.status.setText(f"更新 {result['updated']}，未变 {result['unchanged']}，移除失效 {result['removed']}，失败 {len(self._errors)}。")

    def _show_index_result(self):
        window = self.window()
        window.showNormal(); window.raise_(); window.activateWindow()
        if hasattr(window, 'ensure_tab'):
            window.ensure_tab(2)
        return True

    def search(self):
        if self.is_busy() or self._shutting_down: return
        query, reference = self.query.text().strip(), self.reference.text().strip()
        try:
            if not query and not reference: raise ValueError('请输入描述，或拖入参考图片。')
            if not self.index.count()[0]: raise ValueError('当前模型尚无视频索引，请先建立 / 更新索引。')
            if not self._confirm_model(): return
            encoder, index = self.encoder, self.index
            def task():
                started = time.monotonic(); text_vector = encoder.text(query) if query else None
                vector = combine_search_vectors(encoder.image(reference), text_vector, .35) if reference else text_vector
                return index.search(vector, encoder.model_id), time.monotonic() - started
            self.worker = QueryWorker(task, self)
            self.worker.completed.connect(self._searched); self.worker.failed.connect(self.failed)
            connect_thread_finished(self.worker, self, 'worker', self._finished, pass_thread=True)
            self.set_busy(True); self.status.setText('正在分析查询并搜索画面…'); self.worker.start()
        except Exception as error:
            log.exception('视频查询启动失败'); self.failed(str(error))

    def _searched(self, result):
        rows, elapsed = result; self.show_results(rows)
        self.status.setText(f'找到 {len(rows):,} 个视频，查询耗时 {elapsed:.2f} 秒（含条件编码）。分数不是准确率。')

    def show_results(self, rows):
        self._rows = list(rows); self._shown = 0; self.results.clear(); self.append_page()

    def append_page(self):
        end = min(len(self._rows), self._shown + self.settings['result_limit'])
        for row in self._rows[self._shown:end]:
            item = QtWidgets.QListWidgetItem(f"{Path(row['path']).name}\n命中 {time_label(row['seconds'])} · {row['score']:.3f}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row); item.setIcon(fitted_icon(row['thumbnail']))
            item.setToolTip(row['path'] + '\n命中位置：' + ', '.join(time_label(hit['seconds']) for hit in row['hits']))
            self.results.addItem(item)
        self._shown = end; self.more.setVisible(end < len(self._rows)); self.more.setText(f'加载更多 · 已显示 {end}/{len(self._rows)}')

    def selected(self):
        item = self.results.currentItem()
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None

    def preview_selected(self, seconds=None):
        # QAction.triggered supplies a bool, not a timestamp.
        if isinstance(seconds, bool): seconds = None
        row = self.selected()
        if not row: return
        try:
            if not Path(row['path']).is_file(): raise FileNotFoundError('原视频已移走，请更新索引。')
            self._preview_duration = row['duration']; self._preview_path = row['path']
            ffmpeg, _ = resolve_tools(self.settings['ffmpeg_path']); self.player.configure_ffplay(ffmpeg)
            self.player.prepare(row['path'], 0, row['duration'], row['seconds'] if seconds is None else seconds)
            self.player.play(); self.seek.setEnabled(True)
        except Exception as error:
            log.exception('视频结果预览失败'); self.failed(str(error))

    def _position_changed(self, seconds):
        if self._preview_duration:
            if not self.seek.isSliderDown(): self.seek.setValue(int(seconds / self._preview_duration * 1000))
            self.preview_info.setText(f'{time_label(seconds)} / {time_label(self._preview_duration)}')

    def seek_preview(self):
        if self._preview_duration:
            self.player.prepare(self._preview_path, 0, self._preview_duration, self.seek.value() / 1000 * self._preview_duration)
            self.player.play()

    def stop_preview(self):
        self.player.stop()

    def context_menu(self, position):
        item = self.results.itemAt(position)
        if item is None: return
        self.results.setCurrentItem(item, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        if not item.isSelected(): item.setSelected(True)
        row = item.data(QtCore.Qt.ItemDataRole.UserRole); menu = QtWidgets.QMenu(self)
        menu.addAction('从命中处预览', self.preview_selected); hits = menu.addMenu('其他命中位置')
        for hit in row['hits']:
            hits.addAction(f"{time_label(hit['seconds'])} · {hit['score']:.3f}", lambda _checked=False, t=hit['seconds']: self.preview_selected(t))
        menu.addAction('打开原视频', lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(row['path'])))
        menu.addAction('打开所在目录', lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(row['path']).parent))))
        menu.addAction('复制文件名', lambda: QtWidgets.QApplication.clipboard().setText(Path(row['path']).name))
        menu.addAction('复制文件路径', lambda: QtWidgets.QApplication.clipboard().setText(row['path']))
        menu.addSeparator(); menu.addAction('全选', self.results.selectAll); menu.addAction('取消选择', self.results.clearSelection)
        menu.exec(self.results.viewport().mapToGlobal(position))

    def show_errors(self):
        dialog = QtWidgets.QDialog(self); dialog.setWindowTitle('视频索引失败详情'); dialog.resize(740, 420)
        layout = QtWidgets.QVBoxLayout(dialog); text = QtWidgets.QPlainTextEdit(); text.setReadOnly(True)
        text.setPlainText('\n\n'.join(self._errors)); layout.addWidget(text); dialog.exec()

    def failed(self, message):
        self.status.setText('操作失败：' + message); self._errors.append(message); self.details_button.show()

    def is_busy(self): return self.worker is not None

    def cancel_work(self):
        if self.worker is not None:
            self.worker.requestInterruption(); self.status.setText('正在取消；已完成的视频索引保留。')

    def set_busy(self, busy):
        for widget in (self.model, self.settings_button, self.index_button, self.search_button, self.more): widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy); self.progress.setVisible(busy)
        if busy: self.progress.setRange(0, 0)

    def _finished(self, worker=None):
        worker = worker or self.worker; self.worker = None
        if isinstance(worker, QueryWorker): worker.task = None
        self.set_busy(False)
        if self._pending_settings is not None:
            value, self._pending_settings = self._pending_settings, None; self.update_settings(value)

    def shutdown(self):
        if self.is_busy(): self.cancel_work(); return False
        self._shutting_down = True; self.player.release(); self.encoder.release(); self.index.release()
        self._pending_settings = None
        return True
