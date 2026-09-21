"""Standalone breath-cut selection, timeline review and result dialogs."""

from __future__ import annotations

import copy
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from .engine import VIDEO_SUFFIXES
from .timeline_review import SmartVideoTimelineReview


class _VideoDropList(QtWidgets.QListWidget):
    filesDropped = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    @staticmethod
    def _local_files(event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._local_files(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._local_files(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        files = self._local_files(event)
        if files:
            self.filesDropped.emit(files)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class BreathCutSourceDialog(QtWidgets.QDialog):
    """Choose arbitrary videos without requiring a task script."""

    def __init__(self, sources=(), parent=None):
        super().__init__(parent)
        self.selected_sources = []
        self.setWindowTitle("剪辑气口：选择视频")
        self.resize(760, 480)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "这里只分析气口，不依赖任务文案或智能排序；可按分贝、人声或"
            "组合策略判断。可拖入多个视频，"
            "分析后会进入带波形的时间线；原视频不会被覆盖。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list_widget = _VideoDropList(self)
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.filesDropped.connect(self.add_sources)
        self.list_widget.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.list_widget, 1)

        actions = QtWidgets.QHBoxLayout()
        add_button = QtWidgets.QPushButton("添加视频…", self)
        remove_button = QtWidgets.QPushButton("移除选中", self)
        add_button.clicked.connect(self._browse)
        remove_button.clicked.connect(self._remove_selected)
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            self,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("分析并打开时间线")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.add_sources(sources)

    def _browse(self):
        files, _selected_filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择需要剪辑气口的视频",
            "",
            "视频文件 (*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.mts);;所有文件 (*.*)",
        )
        self.add_sources(files)

    def add_sources(self, values):
        existing = {
            str(self.list_widget.item(index).data(QtCore.Qt.UserRole))
            for index in range(self.list_widget.count())
        }
        for value in values or []:
            path = Path(value)
            try:
                candidates = (
                    sorted(
                        (
                            child for child in path.iterdir()
                            if child.is_file()
                            and child.suffix.lower() in VIDEO_SUFFIXES
                        ),
                        key=lambda child: child.name.casefold(),
                    )
                    if path.is_dir() else [path]
                )
            except OSError:
                continue
            for candidate in candidates:
                try:
                    resolved = str(candidate.resolve())
                except OSError:
                    resolved = str(candidate)
                if (
                    resolved in existing
                    or not candidate.is_file()
                    or candidate.suffix.lower() not in VIDEO_SUFFIXES
                ):
                    continue
                item = QtWidgets.QListWidgetItem(candidate.name)
                item.setData(QtCore.Qt.UserRole, resolved)
                item.setToolTip(resolved)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Checked)
                self.list_widget.addItem(item)
                existing.add(resolved)

    def _remove_selected(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def _open_item(self, item, _column=0):
        path = Path(str(item.data(QtCore.Qt.UserRole) or ""))
        if path.is_file():
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(path.resolve()))
            )

    def accept(self):
        selected = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.checkState() == QtCore.Qt.Checked:
                selected.append(str(item.data(QtCore.Qt.UserRole) or ""))
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "没有视频", "请至少勾选一个视频。"
            )
            return
        self.selected_sources = selected
        super().accept()

    @classmethod
    def get_sources(cls, sources=(), parent=None):
        dialog = cls(sources, parent)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            return dialog.selected_sources
        return None


class BreathCutReviewDialog(QtWidgets.QDialog):
    def __init__(self, bundle, parent=None):
        super().__init__(parent)
        self.bundle = copy.deepcopy(bundle)
        self.reviewed_bundle = None
        self.setWindowTitle("剪辑气口：时间线预览")
        self.resize(1280, 820)
        layout = QtWidgets.QVBoxLayout(self)
        self.summary_label = QtWidgets.QLabel(self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.timeline_review = SmartVideoTimelineReview(self.bundle, self)
        self.timeline_review.orderChanged.connect(self._update_summary)
        layout.addWidget(self.timeline_review, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            self,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("导出气口剪辑视频")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_summary()

    def _update_summary(self, *_args):
        clips = [
            clip
            for task in self.bundle.get("tasks", []) or []
            for clip in task.get("clips", []) or []
        ]
        removed = sum(float(clip.get("removed_seconds") or 0.0) for clip in clips)
        errors = sum(bool(clip.get("silence_detection_error")) for clip in clips)
        text = (
            f"共 {len(clips)} 个视频，当前计划删除约 {removed:.2f} 秒。"
            "灰色区域不会导出；播放会跳过灰色区域，点击灰色区域则可单独试听。"
        )
        if errors:
            text += f"　有 {errors} 个视频检测失败，已保守保留原内容。"
        self.summary_label.setText(text)

    def accept(self):
        if not any(
            clip.get("included", True) and clip.get("kept_ranges")
            for task in self.bundle.get("tasks", []) or []
            for clip in task.get("clips", []) or []
        ):
            QtWidgets.QMessageBox.warning(
                self, "无法导出", "没有可导出的保留视频区间。"
            )
            return
        self.reviewed_bundle = self.bundle
        self.timeline_review.close_player()
        super().accept()

    def reject(self):
        self.timeline_review.close_player()
        super().reject()

    def closeEvent(self, event):
        self.timeline_review.close_player()
        super().closeEvent(event)

    @classmethod
    def get_reviewed_bundle(cls, bundle, parent=None):
        dialog = cls(bundle, parent)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            return dialog.reviewed_bundle
        return None


class BreathCutResultDialog(QtWidgets.QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("气口剪辑完成")
        self.resize(850, 430)
        layout = QtWidgets.QVBoxLayout(self)
        completed = list(result.get("completed", []) or [])
        failed = list(result.get("failed", []) or [])
        skipped = list(result.get("skipped", []) or [])
        layout.addWidget(QtWidgets.QLabel(
            f"成功 {len(completed)}，跳过 {len(skipped)}，失败 {len(failed)}。"
            "双击成功项可打开成片。",
            self,
        ))
        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["视频", "状态", "输出 / 原因"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        for value in completed:
            path = str(value.get("video") or "")
            item = QtWidgets.QTreeWidgetItem([
                str(value.get("task_id") or ""), "已生成", Path(path).name,
            ])
            item.setData(0, QtCore.Qt.UserRole, path)
            item.setToolTip(2, path)
            self.tree.addTopLevelItem(item)
        for value in skipped:
            item = QtWidgets.QTreeWidgetItem([
                str(value.get("task_id") or ""), "已跳过",
                str(value.get("message") or ""),
            ])
            self.tree.addTopLevelItem(item)
        for value in failed:
            item = QtWidgets.QTreeWidgetItem([
                str(value.get("label") or value.get("task_id") or ""),
                "失败", str(value.get("error") or ""),
            ])
            item.setBackground(1, QtGui.QColor("#F4CCCC"))
            self.tree.addTopLevelItem(item)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.tree.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.tree, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_item(self, item, _column):
        path = Path(str(item.data(0, QtCore.Qt.UserRole) or ""))
        if path.is_file():
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(path.resolve()))
            )
