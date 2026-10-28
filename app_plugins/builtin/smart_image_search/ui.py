"""Non-modal Chinese image search for existing inventory material files."""
from model.AppDataStorage import data_directory, data_file, data_io

from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
import logging
import shutil
import time
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from model.InventoryManager import InventoryStore

from model.image_retrieval.encoder import ChineseImageEncoder, MODEL_SPECS, combine_search_vectors
from model.image_retrieval.index import ImageSearchIndex, discover_external_groups
from .settings import normalize_settings


PREVIEW_WIDTH = 185
PREVIEW_HEIGHT = 280


def _fitted_icon(path, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT):
    """Fit without distortion on a portrait-friendly transparent canvas."""
    source = QtGui.QPixmap(str(path))
    if source.isNull():
        return QtGui.QIcon()
    fitted = source.scaled(
        width, height,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QtGui.QPixmap(width, height)
    canvas.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(canvas)
    painter.drawPixmap((width - fitted.width()) // 2,
                       (height - fitted.height()) // 2, fitted)
    painter.end()
    return QtGui.QIcon(canvas)


class _ImageDropEdit(QtWidgets.QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("拖入一张参考图片，或点击右侧选择…")

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                self.setText(url.toLocalFile())
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class _DraggableResults(QtWidgets.QListWidget):
    moveDropFinished = QtCore.pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)

    def mimeData(self, items):
        mime = QtCore.QMimeData()
        mime.setUrls([
            QtCore.QUrl.fromLocalFile(path)
            for item in items
            if (path := str(item.data(QtCore.Qt.ItemDataRole.UserRole) or ""))
            and Path(path).is_file()
        ])
        return mime

    def startDrag(self, _supported_actions):
        items = self.selectedItems()
        if not items:
            return
        mime = self.mimeData(items)
        if not mime.hasUrls():
            return
        paths = [url.toLocalFile() for url in mime.urls()]
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        icon = items[0].icon()
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(96, 96))
        # Default move; Ctrl can request a copy from Explorer.
        action = drag.exec(
            QtCore.Qt.DropAction.MoveAction | QtCore.Qt.DropAction.CopyAction,
            QtCore.Qt.DropAction.MoveAction,
        )
        if action in (QtCore.Qt.DropAction.MoveAction,
                      QtCore.Qt.DropAction.TargetMoveAction):
            self.moveDropFinished.emit(paths, action)


class _SearchWorker(QtCore.QThread):
    progressChanged = QtCore.pyqtSignal(int, int, str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task

    def run(self):
        try:
            self.completed.emit(self.task(
                self.progressChanged.emit, self.isInterruptionRequested
            ))
        except Exception as error:
            logging.getLogger(__name__).exception("智能搜图后台任务失败")
            self.failed.emit(f"{type(error).__name__}: {error}")


def _copy_selected(paths, target_dir, progress, cancelled):
    target_dir = Path(target_dir)
    copied = []
    for number, source in enumerate(paths, 1):
        if cancelled():
            break
        source = Path(source)
        if not source.is_file():
            continue
        candidate = target_dir / source.name
        suffix = 2
        while candidate.exists():
            candidate = target_dir / f"{source.stem} ({suffix}){source.suffix}"
            suffix += 1
        data_io.copy2(source, candidate)
        copied.append(str(candidate))
        progress(number, len(paths), f"已复制 {number}/{len(paths)} 张")
    return {"kind": "copy", "paths": copied, "cancelled": cancelled()}


def _move_external(paths, target_dir):
    """Move loose-library images as a batch; restore prior moves on failure."""
    target_dir = Path(target_dir).resolve()
    data_directory(target_dir).ensure(parents=True, exist_ok=True)
    moved = []
    try:
        for raw_source in paths:
            source = Path(raw_source).resolve()
            if not source.is_file():
                raise FileNotFoundError(f"原图已不存在：{source}")
            target = target_dir / source.name
            suffix = 2
            while target.exists():
                target = target_dir / f"{source.stem} ({suffix}){source.suffix}"
                suffix += 1
            data_io.move(str(source), str(target))
            moved.append({"source": str(source), "target": str(target)})
    except Exception:
        _restore_external(moved)
        raise
    return moved


def _restore_external(moved):
    failures = []
    for item in reversed(moved):
        try:
            data_io.move(item["target"], item["source"])
        except Exception as error:
            failures.append(f"{item['source']}：{error}")
    if failures:
        raise RuntimeError("移动失败且部分原图无法还原：" + "；".join(failures))


class SmartImageSearchDialog(QtWidgets.QDialog):
    settingsChanged = QtCore.pyqtSignal(object)

    def __init__(self, settings, parent=None, store=None, index=None, encoder=None):
        super().__init__(parent)
        self.setWindowTitle("智能搜图")
        self.resize(1060, 760)
        self.settings = normalize_settings(settings)
        self.store = store or InventoryStore()
        self.encoder = encoder or ChineseImageEncoder(self.settings["model"])
        self.index = index or ImageSearchIndex.for_encoder(self.encoder)
        self.worker = None
        self._count_worker = None
        self._count_generation = 0
        self._count_pending = False
        self._shutting_down = False
        self._drag_cleanup_timers = set()
        self._finish_timer = QtCore.QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self._finished)
        self._pending_settings = None
        self._search_rows = []
        self._shown = 0
        self._search_details = ""
        self._model_notice_acknowledged = False
        layout = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "搜索素材库、人物素材及设置中的额外图片库。首次建索引较慢；以后只处理新增或变化的图片。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        index_row = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        for key, spec in MODEL_SPECS.items():
            self.model_combo.addItem(spec["label"], key)
        self.model_combo.setCurrentIndex(self.model_combo.findData(self.settings["model"]))
        self.model_combo.setToolTip("新模型使用独立索引，Base 原索引不会丢失。")
        index_row.addWidget(self.model_combo)
        self.index_status = QtWidgets.QLabel()
        index_row.addWidget(self.index_status, 1)
        self.index_button = QtWidgets.QPushButton("增量更新索引")
        self.index_button.clicked.connect(self.update_index)
        index_row.addWidget(self.index_button)
        self.cancel_button = QtWidgets.QPushButton("停止")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        index_row.addWidget(self.cancel_button)
        layout.addLayout(index_row)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.search_tabs = QtWidgets.QTabWidget()
        text_page = QtWidgets.QWidget()
        text_layout = QtWidgets.QHBoxLayout(text_page)
        self.query_text = QtWidgets.QLineEdit()
        self.query_text.setPlaceholderText("例如：灾难、洪水中的城市、阴暗的废墟…")
        self.query_text.returnPressed.connect(self.search)
        text_layout.addWidget(QtWidgets.QLabel("中文描述"))
        text_layout.addWidget(self.query_text, 1)
        self.search_tabs.addTab(text_page, "文字搜图")

        image_page = QtWidgets.QWidget()
        image_layout = QtWidgets.QVBoxLayout(image_page)
        image_row = QtWidgets.QHBoxLayout()
        self.query_image = _ImageDropEdit()
        image_row.addWidget(self.query_image, 1)
        browse = QtWidgets.QPushButton("选择图片…")
        browse.clicked.connect(self._browse_image)
        image_row.addWidget(browse)
        image_layout.addLayout(image_row)
        description_row = QtWidgets.QHBoxLayout()
        description_row.addWidget(QtWidgets.QLabel("补充描述"))
        self.image_description = QtWidgets.QLineEdit()
        self.image_description.setPlaceholderText("可留空，例如：人物流泪、雨天、全身画面…")
        self.image_description.returnPressed.connect(self.search)
        description_row.addWidget(self.image_description, 1)
        description_row.addWidget(QtWidgets.QLabel("文字侧重"))
        self.text_weight = QtWidgets.QSpinBox()
        self.text_weight.setRange(0, 100)
        self.text_weight.setSingleStep(5)
        self.text_weight.setSuffix(" %")
        self.text_weight.setValue(self.settings["text_weight"])
        self.text_weight.setToolTip(
            "留空只按参考图搜索。有文字时：0% 只看图片，100% 只看文字；"
            "中间值综合排序，不是严格筛选，也不会修改参考图片。"
        )
        description_row.addWidget(self.text_weight)
        image_layout.addLayout(description_row)
        self.search_tabs.addTab(image_page, "以图找图")
        layout.addWidget(self.search_tabs)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel("范围"))
        self.scope = QtWidgets.QComboBox()
        self.scope.setToolTip("仅搜索设置中添加的图片库，不自动包含程序库存和人物素材。")
        self._refresh_scopes()
        filter_row.addWidget(self.scope)
        filter_row.addStretch(1)
        self.search_button = QtWidgets.QPushButton("搜索")
        self.search_button.clicked.connect(self.search)
        filter_row.addWidget(self.search_button)
        layout.addLayout(filter_row)

        self.results = _DraggableResults()
        self.results.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.results.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.results.setMovement(QtWidgets.QListView.Movement.Static)
        self.results.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.results.setToolTip("拖到资源管理器：默认移动；按住 Ctrl 可复制。")
        self.results.setIconSize(QtCore.QSize(PREVIEW_WIDTH, PREVIEW_HEIGHT))
        self.results.setGridSize(QtCore.QSize(220, 350))
        self.results.setWordWrap(True)
        self.results.itemDoubleClicked.connect(self._open_item)
        self.results.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._show_context_menu)
        self.results.moveDropFinished.connect(self._schedule_drag_cleanup)
        self.delete_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Delete"), self.results)
        self.delete_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        self.delete_shortcut.activated.connect(self._delete_selected)
        layout.addWidget(self.results, 1)

        self.load_more_button = QtWidgets.QPushButton("加载更多")
        self.load_more_button.clicked.connect(self._append_page)
        self.load_more_button.setVisible(False)
        layout.addWidget(self.load_more_button)

        action_row = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel(
            "可直接拖到资源管理器：默认移动，Ctrl 复制；索引仅存储在本机。"
        )
        self.status.setWordWrap(True)
        action_row.addWidget(self.status, 1)
        self.copy_button = QtWidgets.QPushButton("复制选中图片到…")
        self.copy_button.clicked.connect(lambda: self._transfer("copy"))
        action_row.addWidget(self.copy_button)
        self.move_button = QtWidgets.QPushButton("移出素材库到…")
        self.move_button.setToolTip("移动原图；成功后立即从搜图索引注销，不重算其他图片。")
        self.move_button.clicked.connect(lambda: self._transfer("move"))
        action_row.addWidget(self.move_button)
        self.delete_button = QtWidgets.QPushButton("删除选中图片…")
        self.delete_button.setToolTip("将选中的原图移入系统回收站，并从搜索结果中移除。")
        self.delete_button.clicked.connect(self._delete_selected)
        action_row.addWidget(self.delete_button)
        layout.addLayout(action_row)
        self.model_combo.currentIndexChanged.connect(self._search_options_changed)
        self.text_weight.valueChanged.connect(self._search_options_changed)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self._refresh_index_status()

    def _refresh_scopes(self):
        previous = self.scope.currentData()
        blocker = QtCore.QSignalBlocker(self.scope)
        self.scope.clear()
        self.scope.addItem("全部已配置图片库", "")
        for root in self.settings["library_roots"]:
            self.scope.addItem(str(root), str(root))
        selected = self.scope.findData(previous)
        self.scope.setCurrentIndex(max(0, selected))
        del blocker

    def _selected_roots(self):
        root = self.scope.currentData()
        roots = self.settings["library_roots"]
        return [root] if root and root in roots else list(roots)

    def _clear_results(self):
        self.results.clear()
        self._search_rows = []
        self._shown = 0
        self.load_more_button.setVisible(False)
        self._search_details = ""

    def _scope_changed(self, _value=None):
        self._clear_results()
        self._refresh_index_status()
        self.status.setText("搜索范围已切换，请重新搜索；不会混入程序库存。")

    def _refresh_index_status(self):
        self._count_generation += 1
        if not self._selected_roots():
            self._count_pending = False
            if self._count_worker is not None:
                self._count_worker.requestInterruption()
            self.index_status.setText("当前图片库已索引 0 张图片")
            return
        self._count_pending = True
        self.index_status.setText("正在后台统计当前图片库…")
        if self._count_worker is not None:
            self._count_worker.requestInterruption()
        else:
            self._start_count()

    def _start_count(self):
        if self._shutting_down or not self._count_pending:
            return
        self._count_pending = False
        generation = self._count_generation
        index, model_id, roots = self.index, self.encoder.model_id, self._selected_roots()
        def task(_progress, cancelled):
            return generation, index.count(model_id, allowed_roots=roots, cancelled=cancelled)
        worker = _SearchWorker(task, QtWidgets.QApplication.instance())
        worker.count_generation = generation
        self._count_worker = worker
        worker.completed.connect(self._count_ready)
        worker.failed.connect(self._count_failed)
        connect_thread_finished(worker, self, '_count_worker', self._count_finished)
        worker.start()

    def _count_ready(self, result):
        generation, count = result
        if not self._shutting_down and generation == self._count_generation and count is not None:
            self.index_status.setText(f"当前图片库已索引 {count:,} 张图片")

    def _count_failed(self, message):
        if (not self._shutting_down and not self._count_pending and self._count_worker is not None
                and self._count_worker.count_generation == self._count_generation):
            self.index_status.setText("图片库统计失败：" + message)

    def _count_finished(self):
        self._count_worker = None
        self._start_count()

    def is_counting(self):
        return self._count_worker is not None

    def cancel_statistics(self):
        self._count_generation += 1
        self._count_pending = False
        if self._count_worker is not None:
            self._count_worker.requestInterruption()

    def closeEvent(self, event):
        self.cancel_statistics()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_index_status()

    def update_settings(self, settings):
        updated = normalize_settings(settings)
        if self.is_busy():
            self._pending_settings = updated
            return
        roots_changed = self.settings["library_roots"] != updated["library_roots"]
        model_changed = self.encoder.model_key != updated["model"]
        if model_changed:
            self.encoder = ChineseImageEncoder(updated["model"])
            self.index = ImageSearchIndex.for_encoder(self.encoder, self.index.root)
            self._model_notice_acknowledged = False
            self._clear_results()
            self.status.setText("已切换模型，旧索引保留。新模型尚无索引时，请先增量更新索引。")
        self.settings = updated
        self._refresh_scopes()
        if roots_changed:
            self._clear_results()
            self.status.setText("图片库范围已更新，请重新搜索；原模型和索引保留。")
        if model_changed or roots_changed:
            self._refresh_index_status()
        blockers = [QtCore.QSignalBlocker(self.model_combo), QtCore.QSignalBlocker(self.text_weight)]
        self.model_combo.setCurrentIndex(self.model_combo.findData(updated["model"]))
        self.text_weight.setValue(updated["text_weight"])
        del blockers

    def _search_options_changed(self, _value=None):
        if self.is_busy():
            return
        updated = {**self.settings, "model": self.model_combo.currentData(),
                   "text_weight": self.text_weight.value()}
        self.update_settings(updated)
        self.settingsChanged.emit(dict(self.settings))

    def _confirm_model(self):
        if self._model_notice_acknowledged:
            return True
        if self.encoder.model_is_cached():
            self._model_notice_acknowledged = True
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "中文模型", f"首次使用 {MODEL_SPECS[self.encoder.model_key]['label']} "
            f"需要联网下载{MODEL_SPECS[self.encoder.model_key]['download']}的模型。"
            "下载完成后会保存在本机，之后不会重复下载。继续吗？",
        )
        self._model_notice_acknowledged = answer == QtWidgets.QMessageBox.StandardButton.Yes
        return self._model_notice_acknowledged

    def _browse_image(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择参考图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.jfif *.tif *.tiff)"
        )
        if path:
            self.query_image.setText(path)

    def is_busy(self):
        # Until the queued finished handler runs, the previous task still owns
        # the encoder/index. Do not switch models or replace its worker early.
        return self.worker is not None

    def cancel_work(self):
        self.cancel_statistics()
        if self.is_busy():
            self.worker.requestInterruption()
            self.status.setText("正在停止；当前图片处理完后会保留已完成的操作。")

    def _start(self, task, message):
        if self._shutting_down or self.is_busy():
            return False
        self.worker = _SearchWorker(task, self)
        self.worker.progressChanged.connect(self._progress)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(self._failed)
        connect_thread_finished(self.worker, self, 'worker', self._finished)
        self.index_button.setEnabled(False)
        self.search_button.setEnabled(False)
        self.move_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.load_more_button.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.text_weight.setEnabled(False)
        self.search_tabs.setEnabled(False)
        self.scope.setEnabled(False)
        self.results.setDragEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText(message)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.worker.start()
        return True

    def update_index(self):
        if self.is_busy():
            return
        roots = list(self.settings["library_roots"])
        if not roots:
            self.status.setText("未配置图片库，请到程序设置 → 智能搜索 → 图片中添加文件夹。")
            return
        if not self._confirm_model():
            return
        encoder, index = self.encoder, self.index
        def task(progress, cancelled):
            progress(0, 0, "正在扫描已配置的图片库…")
            groups = discover_external_groups(roots)
            # Inventory vectors share this database with image assignment. Keep
            # them intact while pruning obsolete external-library records only.
            result = index.sync(groups, encoder, progress, cancelled,
                                prune_source_kinds=("folder",))
            return {"kind": "sync", **result}
        self._start(task, "正在扫描图片库；新增图片才会运行中文模型，不扫描程序库存…")

    def search(self):
        if self.is_busy():
            return
        roots = self._selected_roots()
        if not roots:
            QtWidgets.QMessageBox.information(
                self, "未配置图片库", "请到程序设置 → 智能搜索 → 图片中添加文件夹。不会自动搜索程序库存。"
            )
            return
        mode = self.search_tabs.currentIndex()
        if mode == 0:
            query = self.query_text.text().strip()
            if not query:
                self.query_text.setFocus()
                return
        else:
            query = self.query_image.text().strip()
            if not Path(query).is_file():
                QtWidgets.QMessageBox.warning(self, "参考图片", "请选择有效的本地图片。")
                return
        if not self._confirm_model():
            return
        description = self.image_description.text().strip() if mode else ""
        weight = self.text_weight.value() / 100
        encoder, index = self.encoder, self.index
        def task(progress, _cancelled):
            progress(0, 0, "正在核对当前图片库索引…")
            count = index.count(encoder.model_id, allowed_roots=roots, cancelled=_cancelled)
            if count is None or _cancelled():
                return {"kind": "canceled"}
            if not count:
                return {"kind": "empty"}
            load_start = time.perf_counter()
            prepare = getattr(encoder, "prepare", None)
            if callable(prepare) and not getattr(encoder, "is_loaded", False):
                progress(0, 0, "正在加载中文模型（首次搜索需要数秒）…")
                prepare()
            load_seconds = time.perf_counter() - load_start
            progress(0, 0, "正在分析搜索条件…")
            encode_start = time.perf_counter()
            if mode == 0:
                vector = encoder.text(query)
                search_mode = "文字搜索"
            elif description and weight == 1:
                vector = encoder.text(description)
                search_mode = "只按补充文字"
            else:
                image_vector = encoder.image(query)
                text_vector = encoder.text(description) if description and weight > 0 else None
                vector = combine_search_vectors(image_vector, text_vector, weight)
                search_mode = f"图片＋文字（文字 {weight:.0%}）" if text_vector is not None else "参考图片"
            encode_seconds = time.perf_counter() - encode_start
            progress(0, 0, "正在检索已建立的图片索引…")
            search_start = time.perf_counter()
            return {"kind": "search", "rows": index.search(
                encoder.model_id, vector, None, allowed_roots=roots
            ), "load_seconds": load_seconds,
                "search_mode": search_mode,
                "encode_seconds": encode_seconds,
                "search_seconds": time.perf_counter() - search_start}
        self._start(task, "正在搜索…")

    def _progress(self, done, total, message):
        self.status.setText(message)
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        else:
            self.progress_bar.setRange(0, 0)

    def _completed(self, result):
        kind = result.get("kind")
        if kind == "empty":
            self.status.setText("当前图片库尚无索引，请先点击“增量更新索引”。")
        elif kind == "canceled":
            self.status.setText("已取消搜索。")
        elif kind == "sync":
            self._refresh_index_status()
            self.status.setText(
                f"增量更新完成：处理 {result['new_or_changed']} / "
                f"{result['needed']} 张，移除失效记录 {result['removed']} 条，"
                f"无法读取 {result['failed']} 张；"
                f"高清缩略图更新 {result['thumbnail_updated']} 张"
                f"（失败 {result['thumbnail_failed']} 张）。"
                + (" 已暂停，下次可续建。" if result["cancelled"] else "")
            )
        elif kind == "search":
            self._search_details = (
                f"{result['search_mode']} · 条件 {result['encode_seconds']:.2f} 秒 · "
                f"检索 {result['search_seconds']:.2f} 秒"
            )
            if result["load_seconds"] >= 0.1:
                self._search_details += f" · 模型加载 {result['load_seconds']:.1f} 秒"
            self._show_results(result["rows"])
        elif kind == "move":
            moved = result["moved"]
            self.index.remove_paths(item["source"] for item in moved)
            self._remove_result_paths({item["source"] for item in moved})
            self._refresh_index_status()
            self.status.setText(f"已移出 {len(moved)} 张；其余图片的模型特征没有重算。")
        elif kind == "copy":
            self.status.setText(f"已复制 {len(result['paths'])} 张图片。")
        elif kind == "delete":
            self._remove_result_paths(result["deleted"])
            self._refresh_index_status()
            message = f"已将 {len(result['deleted'])} 张图片移入回收站。"
            if result["cancelled"]:
                message += " 已停止，其余图片未删除。"
            failures = result["failures"]
            if failures:
                message += f" {len(failures)} 张删除失败，已保留在结果中。"
            if result.get("index_error"):
                message += " 索引更新失败，请点击“增量更新索引”修复。"
            self.status.setText(message)
            if failures or result.get("index_error"):
                details = "\n".join(failures)
                if result.get("index_error"):
                    details += "\n索引更新失败：" + result["index_error"]
                notice = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Warning,
                    "删除图片", message, parent=self)
                notice.setDetailedText(details.strip())
                notice.exec()

    def _failed(self, message):
        self.status.setText("操作失败：" + message)
        QtWidgets.QMessageBox.warning(self, "智能搜图", message)

    def _finished(self):
        worker = self.worker
        # finished() precedes native thread-local cleanup. Keep ownership and
        # keep exit blocked until wait(0) confirms the OS thread has joined.
        if isinstance(worker, QtCore.QThread) and not worker.wait(0):
            self._finish_timer.start(20)
            return
        self.worker = None
        if worker is not None:
            if isinstance(worker, _SearchWorker):
                worker.task = None
            worker.deleteLater()
        self.index_button.setEnabled(True)
        self.search_button.setEnabled(True)
        self.move_button.setEnabled(True)
        self.copy_button.setEnabled(True)
        self.delete_button.setEnabled(True)
        self.load_more_button.setEnabled(True)
        self.model_combo.setEnabled(True)
        self.text_weight.setEnabled(True)
        self.search_tabs.setEnabled(True)
        self.scope.setEnabled(True)
        self.results.setDragEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        if self._pending_settings is not None:
            updated, self._pending_settings = self._pending_settings, None
            self.update_settings(updated)

    def _show_results(self, rows):
        self.results.clear()
        self._search_rows = list(rows)
        self._shown = 0
        self._append_page()

    def _append_page(self):
        end = min(len(self._search_rows), self._shown + self.settings["result_limit"])
        for row in self._search_rows[self._shown:end]:
            path = str(row["path"])
            caption = f"{Path(path).name}\n{row['source_name']} · {row['score']:.3f}"
            item = QtWidgets.QListWidgetItem(caption)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, row["source_kind"])
            item.setToolTip(f"{path}\n来源：{row['source_name']}\n相似度排序分数：{row['score']:.4f}")
            thumbnail = str(row.get("thumbnail") or "")
            if Path(thumbnail).is_file():
                item.setIcon(_fitted_icon(thumbnail))
            self.results.addItem(item)
        self._shown = end
        self.load_more_button.setVisible(end < len(self._search_rows))
        self.load_more_button.setText(
            f"加载更多（下一批 {min(self.settings['result_limit'], len(self._search_rows) - end)} 张）"
        )
        self._update_result_status()

    def _update_result_status(self):
        self.status.setText(
            f"已显示 {self._shown:,} / 共 {len(self._search_rows):,} 张 · "
            f"{self._search_details}。分数仅用于排序，不是准确率。"
        )

    def _selected_paths(self):
        return [str(item.data(QtCore.Qt.ItemDataRole.UserRole))
                for item in self.results.selectedItems()]

    def _selected_records(self):
        return [
            (str(item.data(QtCore.Qt.ItemDataRole.UserRole)),
             str(item.data(QtCore.Qt.ItemDataRole.UserRole + 1)))
            for item in self.results.selectedItems()
        ]

    def _remove_result_paths(self, paths):
        normalized = {str(Path(path).resolve()).casefold() for path in paths}
        self._search_rows = [
            row for row in self._search_rows
            if str(Path(row["path"]).resolve()).casefold() not in normalized
        ]
        for row in range(self.results.count() - 1, -1, -1):
            item = self.results.item(row)
            if str(Path(item.data(QtCore.Qt.ItemDataRole.UserRole)).resolve()).casefold() in normalized:
                self.results.takeItem(row)
        self._shown = self.results.count()
        self.load_more_button.setVisible(self._shown < len(self._search_rows))

    def _schedule_drag_cleanup(self, paths, action):
        # Shell file moves may finish just after the drag returns.
        if self._shutting_down:
            return
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        self._drag_cleanup_timers.add(timer)

        def finish():
            self._drag_cleanup_timers.discard(timer)
            timer.deleteLater()
            if not self._shutting_down:
                self._finalize_dragged_move(paths, action)

        timer.timeout.connect(finish)
        timer.start(500)

    def shutdown(self):
        if self.is_busy():
            self.cancel_work()
            return False
        self._shutting_down = True
        self.cancel_statistics()
        if self._count_worker is not None:
            self._count_worker.requestInterruption()
            return False  # The lifecycle gate joins asynchronously, never wait here.
        self._pending_settings = None
        self._finish_timer.stop()
        for timer in self._drag_cleanup_timers:
            timer.stop()
            timer.deleteLater()
        self._drag_cleanup_timers.clear()
        release = getattr(self.encoder, "release", None)
        if callable(release):
            release()
        self.hide()
        return True

    def _finalize_dragged_move(self, paths, action):
        moved = []
        failures = []
        for path in paths:
            source = Path(path)
            if not source.is_file():
                moved.append(path)
            elif action == QtCore.Qt.DropAction.MoveAction:
                # Qt's MoveAction contract: the target accepted the data,
                # and the source must remove the original. On Windows,
                # TargetMoveAction means the target already owns the move.
                try:
                    data_file(source).remove()
                    moved.append(path)
                except OSError as error:
                    failures.append(f"{source.name}：{error}")
            else:
                failures.append(f"{source.name}：目标未移走原件")
        if moved:
            try:
                self.index.remove_paths(moved)
                self._remove_result_paths(moved)
                self._refresh_index_status()
            except Exception as error:
                self.status.setText(
                    f"已移出 {len(moved)} 张，但索引更新失败：{error}。"
                    "下次点击“增量更新索引”可修复。"
                )
                return
        if failures:
            self.status.setText(
                f"已移出 {len(moved)} 张；另有 {len(failures)} 张仍在库中。"
                "请检查目标文件夹：" + "；".join(failures[:3])
            )
        elif moved:
            self.status.setText(f"已通过拖拽移走 {len(moved)} 张，索引已同步注销。")

    def _transfer(self, operation):
        selected = self._selected_records()
        paths = [path for path, _kind in selected]
        if not paths or self.is_busy():
            self.status.setText("请先选择一张或多张结果图片。")
            return
        target = QtWidgets.QFileDialog.getExistingDirectory(self, "选择目标文件夹")
        if not target:
            return
        if operation == "move":
            destination = Path(target).resolve()
            for raw_root in self.settings["library_roots"]:
                root = Path(raw_root).expanduser().resolve()
                if destination == root or root in destination.parents:
                    QtWidgets.QMessageBox.warning(
                        self, "目标仍在图片库内", "请选图库之外的文件夹；库内移动不会移出索引。"
                    )
                    return
            answer = QtWidgets.QMessageBox.question(
                self, "确认移出素材库",
                f"将 {len(paths)} 张原图移到目标文件夹？移动后它们会从素材库和搜图结果中消失。",
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            def task(progress, _cancelled):
                progress(0, 0, "正在移动原图并更新素材库…")
                external = [path for path, kind in selected if kind == "folder"]
                managed = [path for path, kind in selected if kind != "folder"]
                moved = _move_external(external, target) if external else []
                try:
                    if managed:
                        result = self.store.move_material_images(
                            [{"path": path, "target_dir": target} for path in managed]
                        )
                        moved.extend(result["moved"])
                except Exception:
                    _restore_external(moved)
                    raise
                return {"kind": "move", "moved": moved}
        else:
            def task(progress, cancelled):
                return _copy_selected(paths, target, progress, cancelled)
        self._start(task, "正在处理选中的图片…")

    def _open_item(self, item):
        path = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")
        if Path(path).is_file():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _delete_selected(self):
        if self.is_busy() or self._shutting_down:
            return
        selected = self._selected_records()
        if not selected:
            self.status.setText("请先选择一张或多张结果图片。")
            return
        # The search page owns only explicitly configured loose libraries.
        # Managed inventory records require their own store transaction.
        if any(kind != "folder" for _path, kind in selected):
            QtWidgets.QMessageBox.warning(self, "删除图片", "请在库存管理中删除程序库存或人物素材。")
            return
        paths = list(dict.fromkeys(path for path, _kind in selected))
        roots = self._selected_roots()
        confirm = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Warning,
            "确认删除原图", f"将选中的 {len(paths)} 张原图移入系统回收站？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            self)
        confirm.setInformativeText("删除后会从图片库和搜索结果中消失。无法移入回收站的图片将保留。")
        confirm.setDetailedText("\n".join(paths))
        confirm.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if confirm.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        index = self.index
        def task(progress, cancelled):
            from .image_delete import trash_selected
            result = trash_selected(paths, roots, progress, cancelled)
            if result["deleted"]:
                try:
                    index.remove_paths(result["deleted"])
                except Exception as error:
                    result["index_error"] = str(error)
            return result
        self._start(task, "正在将选中图片移入回收站…")

    def _show_context_menu(self, position):
        item = self.results.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            self.results.clearSelection()
            item.setSelected(True)
        menu = QtWidgets.QMenu(self)
        menu.addAction("打开原图", lambda: self._open_item(item))
        menu.addAction("打开所在文件夹", lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(Path(
                item.data(QtCore.Qt.ItemDataRole.UserRole)
            ).parent))
        ))
        menu.addSeparator()
        menu.addAction("移出素材库到…", lambda: self._transfer("move"))
        menu.addAction("复制到…", lambda: self._transfer("copy"))
        menu.addSeparator()
        delete_action = menu.addAction("删除选中图片…", self._delete_selected)
        delete_action.setEnabled(not self.is_busy())
        menu.exec(self.results.viewport().mapToGlobal(position))
