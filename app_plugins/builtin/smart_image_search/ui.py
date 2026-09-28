"""Non-modal Chinese image search for existing inventory material files."""

import shutil
import time
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from model.InventoryManager import InventoryStore

from .encoder import ChineseImageEncoder
from .index import ImageSearchIndex, discover_external_groups
from .settings import normalize_settings


def _fitted_icon(path, size=175):
    """Letterbox the cached thumbnail instead of stretching portrait images."""
    source = QtGui.QPixmap(str(path))
    if source.isNull():
        return QtGui.QIcon()
    fitted = source.scaled(
        size, size,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QtGui.QPixmap(size, size)
    canvas.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(canvas)
    painter.drawPixmap((size - fitted.width()) // 2,
                       (size - fitted.height()) // 2, fitted)
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
        shutil.copy2(source, candidate)
        copied.append(str(candidate))
        progress(number, len(paths), f"已复制 {number}/{len(paths)} 张")
    return {"kind": "copy", "paths": copied, "cancelled": cancelled()}


def _move_external(paths, target_dir):
    """Move loose-library images as a batch; restore prior moves on failure."""
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
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
            shutil.move(str(source), str(target))
            moved.append({"source": str(source), "target": str(target)})
    except Exception:
        _restore_external(moved)
        raise
    return moved


def _restore_external(moved):
    failures = []
    for item in reversed(moved):
        try:
            shutil.move(item["target"], item["source"])
        except Exception as error:
            failures.append(f"{item['source']}：{error}")
    if failures:
        raise RuntimeError("移动失败且部分原图无法还原：" + "；".join(failures))


class SmartImageSearchDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None, store=None, index=None, encoder=None):
        super().__init__(parent)
        self.setWindowTitle("智能搜图")
        self.resize(1060, 760)
        self.settings = normalize_settings(settings)
        self.store = store or InventoryStore()
        self.index = index or ImageSearchIndex()
        self.encoder = encoder or ChineseImageEncoder(self.settings["model"])
        self.worker = None
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
        image_layout = QtWidgets.QHBoxLayout(image_page)
        self.query_image = _ImageDropEdit()
        image_layout.addWidget(self.query_image, 1)
        browse = QtWidgets.QPushButton("选择图片…")
        browse.clicked.connect(self._browse_image)
        image_layout.addWidget(browse)
        self.search_tabs.addTab(image_page, "以图找图")
        layout.addWidget(self.search_tabs)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel("范围"))
        self.scope = QtWidgets.QComboBox()
        self.scope.addItem("全部素材", "")
        self.scope.addItem("普通素材", "material")
        self.scope.addItem("人物素材", "person")
        self.scope.addItem("额外图片库", "folder")
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
        self.results.setIconSize(QtCore.QSize(175, 175))
        self.results.setGridSize(QtCore.QSize(205, 245))
        self.results.setWordWrap(True)
        self.results.itemDoubleClicked.connect(self._open_item)
        self.results.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._show_context_menu)
        self.results.moveDropFinished.connect(self._schedule_drag_cleanup)
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
        layout.addLayout(action_row)
        self._refresh_index_status()

    def _refresh_index_status(self):
        count = self.index.count(self.encoder.model_id)
        self.index_status.setText(f"中文 CLIP Base · 已索引 {count} 张图片")

    def update_settings(self, settings):
        self.settings = normalize_settings(settings)
        if not self.is_busy() and self.encoder.model_key != self.settings["model"]:
            self.encoder = ChineseImageEncoder(self.settings["model"])
            self._model_notice_acknowledged = False
            self.results.clear()
            self._search_rows = []
            self._shown = 0
            self.load_more_button.setVisible(False)
            self._refresh_index_status()

    def _confirm_model(self):
        if self._model_notice_acknowledged:
            return True
        if self.encoder.model_is_cached():
            self._model_notice_acknowledged = True
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "中文模型", "首次使用可能需要联网下载约 753 MB 的中文模型。"
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
        return self.worker is not None and self.worker.isRunning()

    def cancel_work(self):
        if self.is_busy():
            self.worker.requestInterruption()
            self.status.setText("正在停止；当前图片处理完后会保存已完成的索引。")

    def _start(self, task, message):
        if self.is_busy():
            return False
        self.worker = _SearchWorker(task, self)
        self.worker.progressChanged.connect(self._progress)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        self.index_button.setEnabled(False)
        self.search_button.setEnabled(False)
        self.move_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.load_more_button.setEnabled(False)
        self.results.setDragEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText(message)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.worker.start()
        return True

    def update_index(self):
        if self.is_busy() or not self._confirm_model():
            return
        def task(progress, cancelled):
            progress(0, 0, "正在扫描库存中的图片…")
            groups = self.store.list_image_groups()
            progress(0, 0, "正在扫描额外图片库…")
            groups.extend(discover_external_groups(self.settings["library_roots"]))
            result = self.index.sync(groups, self.encoder, progress, cancelled)
            return {"kind": "sync", **result}
        self._start(task, "正在扫描素材库；新增图片才会运行中文模型…")

    def search(self):
        if self.is_busy():
            return
        if not self.index.count(self.encoder.model_id):
            QtWidgets.QMessageBox.information(
                self, "尚无索引", "先点击“增量更新索引”，完成第一次建库。"
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
        scope = str(self.scope.currentData() or "")
        def task(progress, _cancelled):
            load_start = time.perf_counter()
            prepare = getattr(self.encoder, "prepare", None)
            if callable(prepare) and not getattr(self.encoder, "is_loaded", False):
                progress(0, 0, "正在加载中文模型（首次搜索需要数秒）…")
                prepare()
            load_seconds = time.perf_counter() - load_start
            progress(0, 0, "正在分析搜索条件…")
            encode_start = time.perf_counter()
            vector = self.encoder.text(query) if mode == 0 else self.encoder.image(query)
            encode_seconds = time.perf_counter() - encode_start
            progress(0, 0, "正在检索已建立的图片索引…")
            search_start = time.perf_counter()
            return {"kind": "search", "rows": self.index.search(
                self.encoder.model_id, vector, None, scope
            ), "load_seconds": load_seconds,
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
        if kind == "sync":
            self._refresh_index_status()
            self.status.setText(
                f"增量更新完成：处理 {result['new_or_changed']} / "
                f"{result['needed']} 张，移除失效记录 {result['removed']} 条，"
                f"无法读取 {result['failed']} 张。"
                + (" 已暂停，下次可续建。" if result["cancelled"] else "")
            )
        elif kind == "search":
            self._search_details = (
                f"条件 {result['encode_seconds']:.2f} 秒 · "
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

    def _failed(self, message):
        self.status.setText("操作失败：" + message)
        QtWidgets.QMessageBox.warning(self, "智能搜图", message)

    def _finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        self.index_button.setEnabled(True)
        self.search_button.setEnabled(True)
        self.move_button.setEnabled(True)
        self.copy_button.setEnabled(True)
        self.load_more_button.setEnabled(True)
        self.results.setDragEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.update_settings(self.settings)

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
        QtCore.QTimer.singleShot(
            500, lambda: self._finalize_dragged_move(paths, action)
        )

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
                    source.unlink()
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
        menu.exec(self.results.viewport().mapToGlobal(position))
