from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from .analyzer import (
    VIDEO_SUFFIXES,
    analysis_signature,
    analyze_video,
    discover_videos,
    error_analysis_result,
    normalize_material_organizer_settings,
)


ASSET_MIME_TYPE = "application/x-lzx-material-assets"
ASSET_ID_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
CATEGORY_ID_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
CATEGORY_BUILTIN_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 2
CATEGORY_MODE_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 3


def _duration_text(seconds):
    seconds = max(0, int(round(float(seconds or 0.0))))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class MaterialAssetModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.assets = []
        self._icons = {}

    def set_assets(self, assets):
        self.beginResetModel()
        self.assets = list(assets or [])
        # Re-analysis overwrites a thumbnail at the same cache path, so old
        # QIcon instances must not survive a model reset.
        self._icons.clear()
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.assets)

    def asset_at(self, index):
        if isinstance(index, QtCore.QModelIndex) and index.isValid():
            row = index.row()
            if 0 <= row < len(self.assets):
                return self.assets[row]
        return None

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        asset = self.asset_at(index)
        if asset is None:
            return None
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            confidence = int(round(float(asset.get("confidence") or 0.0) * 100))
            return (
                f"{asset.get('file_name', '')}\n"
                f"{_duration_text(asset.get('duration'))}  ·  置信度 {confidence}%"
            )
        if role == QtCore.Qt.ItemDataRole.DecorationRole:
            path = str(asset.get("thumbnail_path") or "")
            icon = self._icons.get(path)
            if icon is None:
                icon = QtGui.QIcon(path) if path and Path(path).is_file() else QtGui.QIcon()
                self._icons[path] = icon
            return icon
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            analysis = asset.get("analysis") or {}
            state = "文件存在" if asset.get("exists_now") else "源文件已丢失"
            return (
                f"{asset.get('path', '')}\n"
                f"{asset.get('width', 0)}×{asset.get('height', 0)}  "
                f"{float(asset.get('fps') or 0):.2f} fps  "
                f"{_duration_text(asset.get('duration'))}\n"
                f"{analysis.get('reason', '尚未分析')} · {state}"
            )
        if role == QtCore.Qt.ItemDataRole.ForegroundRole and not asset.get("exists_now"):
            return QtGui.QBrush(QtGui.QColor("#d93025"))
        if role == ASSET_ID_ROLE:
            return int(asset["id"])
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid():
            flags |= QtCore.Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def mimeTypes(self):
        return [ASSET_MIME_TYPE, "text/uri-list"]

    def mimeData(self, indexes):
        unique_rows = sorted({index.row() for index in indexes if index.isValid()})
        assets = [self.assets[row] for row in unique_rows]
        mime = QtCore.QMimeData()
        mime.setData(
            ASSET_MIME_TYPE,
            json.dumps([int(asset["id"]) for asset in assets]).encode("utf-8"),
        )
        urls = [
            QtCore.QUrl.fromLocalFile(str(Path(asset["path"]).resolve()))
            for asset in assets
            if asset.get("exists_now") and Path(asset["path"]).is_file()
        ]
        mime.setUrls(urls)
        return mime

    def supportedDragActions(self):
        return QtCore.Qt.DropAction.CopyAction


class CopyOnlyMaterialView(QtWidgets.QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.setMovement(QtWidgets.QListView.Movement.Static)
        self.setLayoutMode(QtWidgets.QListView.LayoutMode.Batched)
        self.setBatchSize(80)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setIconSize(QtCore.QSize(240, 135))
        self.setGridSize(QtCore.QSize(270, 190))
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)

    def startDrag(self, _supported_actions):
        indexes = self.selectedIndexes()
        if not indexes or self.model() is None:
            return
        mime = self.model().mimeData(indexes)
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        decoration = self.model().data(
            indexes[0], QtCore.Qt.ItemDataRole.DecorationRole
        )
        if isinstance(decoration, QtGui.QIcon):
            drag.setPixmap(decoration.pixmap(128, 72))
        # MoveAction is intentionally never offered.  Explorer and other
        # targets can only copy the referenced source videos.
        drag.exec(
            QtCore.Qt.DropAction.CopyAction,
            QtCore.Qt.DropAction.CopyAction,
        )


class CategoryTree(QtWidgets.QTreeWidget):
    assetsDropped = QtCore.pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setMinimumWidth(210)
        self.setAcceptDrops(True)
        self.setDragEnabled(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(ASSET_MIME_TYPE):
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(0, CATEGORY_ID_ROLE):
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        category_id = item.data(0, CATEGORY_ID_ROLE) if item is not None else None
        if not category_id or not event.mimeData().hasFormat(ASSET_MIME_TYPE):
            event.ignore()
            return
        try:
            asset_ids = json.loads(bytes(
                event.mimeData().data(ASSET_MIME_TYPE)
            ).decode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            event.ignore()
            return
        self.assetsDropped.emit(asset_ids, int(category_id))
        event.setDropAction(QtCore.Qt.DropAction.CopyAction)
        event.accept()


class MaterialAnalysisThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int, str)
    assetUpdated = QtCore.pyqtSignal(int)
    itemFailed = QtCore.pyqtSignal(str, str)
    completed = QtCore.pyqtSignal(object)

    def __init__(self, store, paths, settings, force=False, parent=None):
        super().__init__(parent)
        self.store = store
        self.paths = tuple(Path(path) for path in paths)
        self.settings = normalize_material_organizer_settings(settings)
        self.force = bool(force)

    def run(self):
        completed = 0
        skipped = 0
        failed = 0
        total = len(self.paths)
        for index, path in enumerate(self.paths, 1):
            if self.isInterruptionRequested():
                break
            self.progress.emit(index, total, f"正在分析：{path.name}")
            asset_id = None
            try:
                asset_id = self.store.upsert_pending_asset(path)
                current = self.store.asset(asset_id)
                signature = analysis_signature(path, self.settings)
                thumbnail = Path(str(current.get("thumbnail_path") or ""))
                if (
                    not self.force
                    and current.get("analysis_signature") == signature
                    and thumbnail.is_file()
                    and current.get("status") in {"ready", "error"}
                ):
                    skipped += 1
                    self.assetUpdated.emit(asset_id)
                    continue
                result = analyze_video(
                    path,
                    self.store.thumbnail_root,
                    self.settings,
                    should_stop=self.isInterruptionRequested,
                )
                self.store.update_analysis(asset_id, result)
                completed += 1
                self.assetUpdated.emit(asset_id)
            except InterruptedError:
                break
            except Exception as error:
                failed += 1
                if asset_id is not None:
                    try:
                        self.store.update_analysis(
                            asset_id,
                            error_analysis_result(
                                path,
                                self.store.thumbnail_root,
                                self.settings,
                                f"{type(error).__name__}: {error}",
                            ),
                        )
                        self.assetUpdated.emit(asset_id)
                    except Exception:
                        pass
                self.itemFailed.emit(str(path), f"{type(error).__name__}: {error}")
        self.completed.emit({
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "cancelled": self.isInterruptionRequested(),
        })


class MaterialOrganizerDialog(QtWidgets.QDialog):
    def __init__(self, store, settings, logger=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.settings = normalize_material_organizer_settings(settings)
        self.logger = logger or (lambda _message, _level=logging.INFO: None)
        self.worker = None
        self.current_category_id = None
        self.current_mode = "all"
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(180)
        self._refresh_timer.timeout.connect(self._refresh_after_worker_update)
        self.setWindowTitle("素材整理")
        self.setWindowFlags(
            self.windowFlags()
            | QtCore.Qt.WindowType.WindowMinimizeButtonHint
            | QtCore.Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self._build_ui()
        self.refresh_categories()
        self.refresh_assets()
        # Recover records left pending or without a thumbnail by an older
        # analyzer.  Valid error thumbnails are retained and are not retried
        # on every open.
        QtCore.QTimer.singleShot(0, self.repair_incomplete_assets)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        self.add_files_button = QtWidgets.QPushButton("添加视频…", self)
        self.add_folder_button = QtWidgets.QPushButton("添加文件夹…", self)
        self.reanalyze_button = QtWidgets.QPushButton("重新分析选中项", self)
        self.refresh_button = QtWidgets.QPushButton("查库", self)
        self.new_category_button = QtWidgets.QPushButton("新建分类", self)
        toolbar.addWidget(self.add_files_button)
        toolbar.addWidget(self.add_folder_button)
        toolbar.addWidget(self.reanalyze_button)
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.new_category_button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.category_tree = CategoryTree(splitter)
        right = QtWidgets.QWidget(splitter)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        search_row = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit(right)
        self.search_edit.setPlaceholderText("搜索素材名称或路径…")
        self.location_label = QtWidgets.QLabel("全部素材", right)
        self.location_label.setStyleSheet("font-weight:600;")
        search_row.addWidget(self.location_label)
        search_row.addWidget(self.search_edit, 1)
        right_layout.addLayout(search_row)
        self.asset_model = MaterialAssetModel(self)
        self.asset_view = CopyOnlyMaterialView(right)
        self.asset_view.setModel(self.asset_model)
        self.asset_view.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        right_layout.addWidget(self.asset_view, 1)
        splitter.addWidget(self.category_tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 900])
        root.addWidget(splitter, 1)

        status_row = QtWidgets.QHBoxLayout()
        self.progress_bar = QtWidgets.QProgressBar(self)
        self.progress_bar.setVisible(False)
        self.status_label = QtWidgets.QLabel("", self)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.progress_bar)
        root.addLayout(status_row)

        self.add_files_button.clicked.connect(self.choose_files)
        self.add_folder_button.clicked.connect(self.choose_folder)
        self.reanalyze_button.clicked.connect(self.reanalyze_selected)
        self.refresh_button.clicked.connect(self.check_library)
        self.new_category_button.clicked.connect(self.create_category)
        self.search_edit.textChanged.connect(self.refresh_assets)
        self.category_tree.currentItemChanged.connect(self._category_changed)
        self.category_tree.assetsDropped.connect(self._add_virtual_memberships)
        self.category_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.category_tree.customContextMenuRequested.connect(
            self._category_context_menu
        )
        self.asset_view.doubleClicked.connect(self.open_asset)
        self.asset_view.customContextMenuRequested.connect(self._asset_context_menu)

    def update_settings(self, settings):
        self.settings = normalize_material_organizer_settings(settings)

    def refresh_categories(self):
        selected_id = self.current_category_id
        selected_mode = self.current_mode
        self.category_tree.blockSignals(True)
        self.category_tree.clear()
        counts = self.store.counts()
        all_item = QtWidgets.QTreeWidgetItem([f"全部素材 ({counts['total']})"])
        all_item.setData(0, CATEGORY_MODE_ROLE, "all")
        self.category_tree.addTopLevelItem(all_item)
        missing_item = QtWidgets.QTreeWidgetItem([f"源文件已丢失 ({counts['missing']})"])
        missing_item.setData(0, CATEGORY_MODE_ROLE, "missing")
        self.category_tree.addTopLevelItem(missing_item)
        categories = self.store.list_categories()
        items = {}
        for category in categories:
            item = QtWidgets.QTreeWidgetItem([
                f"{category['name']} ({category['asset_count']})"
            ])
            item.setData(0, CATEGORY_ID_ROLE, int(category["id"]))
            item.setData(0, CATEGORY_BUILTIN_ROLE, bool(category["builtin"]))
            item.setData(0, CATEGORY_MODE_ROLE, "category")
            items[int(category["id"])] = item
        for category in categories:
            item = items[int(category["id"])]
            parent = items.get(category.get("parent_id"))
            (parent.addChild if parent is not None else self.category_tree.addTopLevelItem)(item)
        target = all_item
        for item in [all_item, missing_item, *items.values()]:
            if selected_mode == "category" and item.data(0, CATEGORY_ID_ROLE) == selected_id:
                target = item
                break
            if selected_mode == item.data(0, CATEGORY_MODE_ROLE) and selected_mode != "category":
                target = item
                break
        self.category_tree.setCurrentItem(target)
        self.category_tree.expandAll()
        self.category_tree.blockSignals(False)

    def refresh_assets(self):
        assets = self.store.list_assets(
            self.current_category_id if self.current_mode == "category" else None,
            self.search_edit.text(),
        )
        if self.current_mode == "missing":
            assets = [asset for asset in assets if not asset.get("exists_now")]
        self.asset_model.set_assets(assets)
        self.status_label.setText(f"显示 {len(assets)} 个素材；拖到资源管理器时只会复制。")

    def _category_changed(self, current, _previous):
        if current is None:
            return
        self.current_mode = str(current.data(0, CATEGORY_MODE_ROLE) or "all")
        self.current_category_id = current.data(0, CATEGORY_ID_ROLE)
        self.location_label.setText(current.text(0).rsplit(" (", 1)[0])
        self.refresh_assets()

    def choose_files(self):
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择视频素材",
            "",
            "视频文件 (*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.mts *.m2ts)",
        )
        self.start_analysis(paths)

    def choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择素材文件夹")
        if path:
            self.start_analysis([path])

    def start_analysis(self, paths, force=False):
        if self.worker is not None and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self, "正在分析", "请等待当前素材分析完成。")
            return
        videos = discover_videos(paths)
        if not videos:
            QtWidgets.QMessageBox.information(self, "没有视频", "没有找到支持的视频文件。")
            return
        self.worker = MaterialAnalysisThread(
            self.store, videos, self.settings, force=force, parent=self
        )
        self.worker.progress.connect(self._analysis_progress)
        self.worker.assetUpdated.connect(self._asset_updated)
        self.worker.itemFailed.connect(self._analysis_failed)
        self.worker.completed.connect(self._analysis_completed)
        self.progress_bar.setRange(0, len(videos))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._set_controls_enabled(False)
        self.worker.start()

    def _set_controls_enabled(self, enabled):
        for widget in (
            self.add_files_button,
            self.add_folder_button,
            self.reanalyze_button,
        ):
            widget.setEnabled(bool(enabled))

    def _analysis_progress(self, current, total, message):
        self.progress_bar.setMaximum(max(1, int(total)))
        self.progress_bar.setValue(max(0, int(current) - 1))
        self.status_label.setText(message)

    def _asset_updated(self, _asset_id):
        self.progress_bar.setValue(min(
            self.progress_bar.maximum(), self.progress_bar.value() + 1
        ))
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _refresh_after_worker_update(self):
        self.refresh_categories()
        self.refresh_assets()

    def _analysis_failed(self, path, message):
        self.logger(f"[素材整理] {Path(path).name} 分析失败：{message}", logging.ERROR)

    def _analysis_completed(self, summary):
        self._refresh_timer.stop()
        self.progress_bar.setVisible(False)
        self._set_controls_enabled(True)
        self.refresh_categories()
        self.refresh_assets()
        self.status_label.setText(
            f"分析完成：更新 {summary['completed']}，复用 {summary['skipped']}，"
            f"失败 {summary['failed']}。"
        )
        self.logger(
            f"[素材整理] 分析完成：更新 {summary['completed']}，"
            f"复用 {summary['skipped']}，失败 {summary['failed']}。"
        )
        self.worker.deleteLater()
        self.worker = None

    def selected_assets(self):
        result = []
        seen = set()
        for index in self.asset_view.selectedIndexes():
            asset = self.asset_model.asset_at(index)
            if asset and asset["id"] not in seen:
                seen.add(asset["id"])
                result.append(asset)
        return result

    def reanalyze_selected(self):
        paths = [asset["path"] for asset in self.selected_assets() if Path(asset["path"]).is_file()]
        if not paths:
            QtWidgets.QMessageBox.information(self, "未选择素材", "请先选择需要重新分析的视频。")
            return
        self.start_analysis(paths, force=True)

    def repair_incomplete_assets(self):
        if self.worker is not None and self.worker.isRunning():
            return
        paths = []
        for asset in self.store.list_assets():
            source = Path(str(asset.get("path") or ""))
            thumbnail = Path(str(asset.get("thumbnail_path") or ""))
            if not source.is_file():
                continue
            if asset.get("status") == "pending" or not thumbnail.is_file():
                paths.append(source)
        if paths:
            self.status_label.setText(
                f"正在自动修复 {len(paths)} 个缺少缩略图的素材…"
            )
            self.start_analysis(paths, force=True)

    def check_library(self):
        changed = self.store.refresh_file_states()
        self.refresh_categories()
        self.refresh_assets()
        self.status_label.setText(f"查库完成：{changed} 个素材的文件状态发生变化。")

    def open_asset(self, index):
        asset = self.asset_model.asset_at(index)
        if not asset:
            return
        path = Path(asset["path"])
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "源文件不存在", str(path))
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def create_category(self):
        parent_id = self.current_category_id if self.current_mode == "category" else None
        name, accepted = QtWidgets.QInputDialog.getText(self, "新建虚拟分类", "分类名称：")
        if not accepted:
            return
        try:
            category_id = self.store.create_category(name, parent_id)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "无法创建分类", str(error))
            return
        self.current_mode = "category"
        self.current_category_id = category_id
        self.refresh_categories()
        self.refresh_assets()

    def _add_virtual_memberships(self, asset_ids, category_id):
        self.store.add_memberships(asset_ids, category_id)
        self.refresh_categories()
        self.refresh_assets()
        self.status_label.setText("已复制到虚拟分类；源文件和原分类均未改变。")

    def _category_context_menu(self, point):
        item = self.category_tree.itemAt(point)
        if item is None or item.data(0, CATEGORY_MODE_ROLE) != "category":
            return
        category_id = int(item.data(0, CATEGORY_ID_ROLE))
        builtin = bool(item.data(0, CATEGORY_BUILTIN_ROLE))
        menu = QtWidgets.QMenu(self)
        child_action = menu.addAction("新建子分类…")
        rename_action = menu.addAction("重命名…")
        delete_action = menu.addAction("删除虚拟分类")
        rename_action.setEnabled(not builtin)
        delete_action.setEnabled(not builtin)
        chosen = menu.exec(self.category_tree.viewport().mapToGlobal(point))
        if chosen == child_action:
            self.current_mode = "category"
            self.current_category_id = category_id
            self.create_category()
        elif chosen == rename_action:
            current_name = item.text(0).rsplit(" (", 1)[0]
            name, accepted = QtWidgets.QInputDialog.getText(
                self, "重命名虚拟分类", "新名称：", text=current_name
            )
            if accepted:
                try:
                    self.store.rename_category(category_id, name)
                    self.refresh_categories()
                except Exception as error:
                    QtWidgets.QMessageBox.warning(self, "无法重命名", str(error))
        elif chosen == delete_action:
            if QtWidgets.QMessageBox.question(
                self,
                "删除虚拟分类",
                "只删除分类和分类关系，不会删除任何源视频。继续吗？",
            ) == QtWidgets.QMessageBox.StandardButton.Yes:
                try:
                    self.store.delete_category(category_id)
                    self.current_mode = "all"
                    self.current_category_id = None
                    self.refresh_categories()
                    self.refresh_assets()
                except Exception as error:
                    QtWidgets.QMessageBox.warning(self, "无法删除", str(error))

    def _asset_context_menu(self, point):
        index = self.asset_view.indexAt(point)
        if index.isValid() and not self.asset_view.selectionModel().isSelected(index):
            self.asset_view.setCurrentIndex(index)
        assets = self.selected_assets()
        if not assets:
            return
        menu = QtWidgets.QMenu(self)
        open_action = menu.addAction("打开原视频")
        folder_action = menu.addAction("打开所在文件夹")
        analyze_action = menu.addAction("重新分析")
        menu.addSeparator()
        remove_category_action = menu.addAction("从当前虚拟分类移除")
        remove_category_action.setEnabled(self.current_mode == "category")
        remove_library_action = menu.addAction("从素材整理库移除")
        chosen = menu.exec(self.asset_view.viewport().mapToGlobal(point))
        if chosen == open_action:
            self.open_asset(index)
        elif chosen == folder_action:
            path = Path(assets[0]["path"])
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.parent)))
        elif chosen == analyze_action:
            self.start_analysis(
                [asset["path"] for asset in assets if Path(asset["path"]).is_file()],
                force=True,
            )
        elif chosen == remove_category_action:
            self.store.remove_memberships(
                [asset["id"] for asset in assets], self.current_category_id
            )
            self.refresh_categories()
            self.refresh_assets()
        elif chosen == remove_library_action:
            if QtWidgets.QMessageBox.question(
                self,
                "移出素材整理库",
                "只移除插件记录和缩略图缓存，绝不会删除源视频。继续吗？",
            ) != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            thumbnails = self.store.remove_assets([asset["id"] for asset in assets])
            for thumbnail in thumbnails:
                try:
                    if thumbnail:
                        Path(thumbnail).unlink(missing_ok=True)
                except OSError:
                    pass
            self.refresh_categories()
            self.refresh_assets()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self.start_analysis(paths)
        event.acceptProposedAction()

    def can_close(self):
        if self.worker is None or not self.worker.isRunning():
            return True
        self.worker.requestInterruption()
        return False

    def closeEvent(self, event):
        # Keep the database browser and its selection alive for fast reopening.
        self.hide()
        event.ignore()
