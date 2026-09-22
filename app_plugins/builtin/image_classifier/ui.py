from __future__ import annotations

import os
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from .classifier import (
    PENDING_CATEGORY,
    ImageClassifierEngine,
    apply_classification_results,
    discover_images,
    flatten_categories,
    normalize_image_classifier_settings,
)


class _PathDropList(QtWidgets.QListWidget):
    pathsDropped = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setToolTip("可把图片、一个或多个文件夹直接拖到这里")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class _ClassificationWorker(QtCore.QThread):
    progressChanged = QtCore.pyqtSignal(int, int, str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, paths, settings, parent=None):
        super().__init__(parent)
        self.paths = list(paths)
        self.settings = dict(settings)

    def run(self):
        try:
            engine = ImageClassifierEngine(
                self.settings,
                progress=self.progressChanged.emit,
                cancelled=self.isInterruptionRequested,
            )
            self.completed.emit(engine.classify(self.paths))
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class _ApplyWorker(QtCore.QThread):
    progressChanged = QtCore.pyqtSignal(int, int, str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, results, output_dir, operation, parent=None):
        super().__init__(parent)
        self.results = [dict(result) for result in results]
        self.output_dir = output_dir
        self.operation = operation

    def run(self):
        try:
            result = apply_classification_results(
                self.results,
                self.output_dir,
                self.operation,
                progress=self.progressChanged.emit,
                cancelled=self.isInterruptionRequested,
            )
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class ImageClassifierDialog(QtWidgets.QDialog):
    settingsChanged = QtCore.pyqtSignal(object)

    COL_FILE = 0
    COL_SUGGESTED = 1
    COL_ASSIGNED = 2
    COL_SIMILARITY = 3
    COL_MARGIN = 4
    COL_STATUS = 5

    def __init__(self, settings, logger=None, notifier=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("图片智能分类")
        self.resize(1180, 760)
        self.settings = normalize_image_classifier_settings(settings)
        self.logger = logger or (lambda _message: None)
        self.notifier = notifier or (lambda _title, _message, _critical=False: None)
        self.worker = None
        self.apply_worker = None
        self.results = []

        root = QtWidgets.QVBoxLayout(self)
        source_group = QtWidgets.QGroupBox("待分类图片", self)
        source_layout = QtWidgets.QVBoxLayout(source_group)
        self.source_list = _PathDropList(source_group)
        self.source_list.pathsDropped.connect(self.add_paths)
        source_layout.addWidget(self.source_list)
        source_actions = QtWidgets.QHBoxLayout()
        self.add_files_button = QtWidgets.QPushButton("添加图片…", source_group)
        self.add_folder_button = QtWidgets.QPushButton("添加文件夹…", source_group)
        self.remove_button = QtWidgets.QPushButton("移除选中", source_group)
        self.clear_button = QtWidgets.QPushButton("清空", source_group)
        source_actions.addWidget(self.add_files_button)
        source_actions.addWidget(self.add_folder_button)
        source_actions.addWidget(self.remove_button)
        source_actions.addWidget(self.clear_button)
        source_actions.addStretch(1)
        self.recursive_checkbox = QtWidgets.QCheckBox("递归扫描", source_group)
        source_actions.addWidget(self.recursive_checkbox)
        source_layout.addLayout(source_actions)

        output_row = QtWidgets.QHBoxLayout()
        output_row.addWidget(QtWidgets.QLabel("整理到：", self))
        self.output_edit = QtWidgets.QLineEdit(self)
        self.output_button = QtWidgets.QPushButton("选择…", self)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_button)
        output_row.addWidget(QtWidgets.QLabel("执行方式：", self))
        self.operation_combo = QtWidgets.QComboBox(self)
        self.operation_combo.addItem("复制（保留原图）", "copy")
        self.operation_combo.addItem("移动", "move")
        output_row.addWidget(self.operation_combo)

        root.addWidget(source_group, 2)
        root.addLayout(output_row)

        controls = QtWidgets.QHBoxLayout()
        self.analyze_button = QtWidgets.QPushButton("开始智能分类", self)
        self.stop_button = QtWidgets.QPushButton("停止", self)
        self.stop_button.setEnabled(False)
        self.filter_combo = QtWidgets.QComboBox(self)
        self.filter_combo.addItem("显示全部", "all")
        self.filter_combo.addItem("只看待人工确认", "pending")
        self.filter_combo.addItem("只看可自动分类", "ready")
        self.filter_combo.addItem("只看读取失败", "error")
        self.change_category_button = QtWidgets.QPushButton("修改选中分类…", self)
        self.open_source_button = QtWidgets.QPushButton("打开原图", self)
        controls.addWidget(self.analyze_button)
        controls.addWidget(self.stop_button)
        controls.addSpacing(20)
        controls.addWidget(QtWidgets.QLabel("结果筛选：", self))
        controls.addWidget(self.filter_combo)
        controls.addWidget(self.change_category_button)
        controls.addWidget(self.open_source_button)
        controls.addStretch(1)
        root.addLayout(controls)

        result_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        self.result_table = QtWidgets.QTableWidget(result_splitter)
        self.result_table.setColumnCount(6)
        self.result_table.setHorizontalHeaderLabels([
            "图片", "AI 建议", "最终分类", "相似度", "领先差距", "状态",
        ])
        self.result_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.result_table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.result_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.verticalHeader().setVisible(False)
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(self.COL_FILE, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_SUGGESTED, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_ASSIGNED, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_SIMILARITY, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_MARGIN, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_STATUS, QtWidgets.QHeaderView.ResizeToContents)

        preview_panel = QtWidgets.QWidget(result_splitter)
        preview_layout = QtWidgets.QVBoxLayout(preview_panel)
        self.preview_label = QtWidgets.QLabel("选择一条结果查看图片", preview_panel)
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setMinimumSize(260, 300)
        self.preview_label.setStyleSheet(
            "QLabel{background:#202124;color:#EEE;border:1px solid #555;}"
        )
        self.preview_label.setWordWrap(True)
        self.preview_detail = QtWidgets.QLabel("", preview_panel)
        self.preview_detail.setWordWrap(True)
        self.preview_detail.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.preview_detail)
        result_splitter.addWidget(self.result_table)
        result_splitter.addWidget(preview_panel)
        result_splitter.setSizes([880, 300])
        root.addWidget(result_splitter, 7)

        self.progress = QtWidgets.QProgressBar(self)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status_label = QtWidgets.QLabel(
            "先分析并核对结果，再执行复制或移动。原图不会在分析阶段改变。", self
        )
        root.addWidget(self.progress)
        root.addWidget(self.status_label)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        self.apply_button = QtWidgets.QPushButton("执行整理", self)
        self.apply_button.setEnabled(False)
        self.close_button = QtWidgets.QPushButton("关闭", self)
        bottom.addWidget(self.apply_button)
        bottom.addWidget(self.close_button)
        root.addLayout(bottom)

        self.add_files_button.clicked.connect(self._choose_files)
        self.add_folder_button.clicked.connect(self._choose_folder)
        self.remove_button.clicked.connect(self._remove_selected_sources)
        self.clear_button.clicked.connect(self.source_list.clear)
        self.output_button.clicked.connect(self._choose_output)
        self.analyze_button.clicked.connect(self.start_analysis)
        self.stop_button.clicked.connect(self.stop_current_work)
        self.filter_combo.currentIndexChanged.connect(self._render_results)
        self.change_category_button.clicked.connect(self._change_selected_categories)
        self.open_source_button.clicked.connect(self._open_selected_source)
        self.apply_button.clicked.connect(self.apply_results)
        self.close_button.clicked.connect(self.close)
        self.result_table.itemSelectionChanged.connect(self._update_preview)
        self.result_table.itemDoubleClicked.connect(
            lambda _item: self._open_selected_source()
        )
        self.update_settings(self.settings)

    def update_settings(self, settings):
        self.settings = normalize_image_classifier_settings(settings)
        if not self._busy():
            self.recursive_checkbox.setChecked(self.settings["recursive"])
            self.operation_combo.setCurrentIndex(max(
                0, self.operation_combo.findData(self.settings["operation"])
            ))
        if not self.source_list.count():
            self.add_paths([
                path for path in self.settings["last_sources"]
                if Path(path).exists()
            ])
        if not self.output_edit.text().strip():
            self.output_edit.setText(self.settings["last_output_dir"])

    def _busy(self):
        return bool(
            (self.worker is not None and self.worker.isRunning())
            or (self.apply_worker is not None and self.apply_worker.isRunning())
        )

    def _has_unapplied_results(self):
        return any(
            result.get("status") not in {"error", "completed"}
            and result.get("assigned_category")
            for result in self.results
        )

    def can_close(self):
        return not self._busy()

    def source_paths(self):
        return [self.source_list.item(index).text() for index in range(self.source_list.count())]

    def add_paths(self, paths):
        existing = {
            os.path.normcase(self.source_list.item(index).text())
            for index in range(self.source_list.count())
        }
        for raw_path in paths or []:
            path = str(Path(raw_path))
            key = os.path.normcase(path)
            if Path(path).exists() and key not in existing:
                existing.add(key)
                self.source_list.addItem(path)

    def _choose_files(self):
        files, _selected_filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            "",
            "图片 (*.jpg *.jpeg *.jfif *.png *.webp *.bmp *.gif *.tif *.tiff *.avif);;所有文件 (*)",
        )
        self.add_paths(files)

    def _choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if folder:
            self.add_paths([folder])

    def _remove_selected_sources(self):
        for item in self.source_list.selectedItems():
            self.source_list.takeItem(self.source_list.row(item))

    def _choose_output(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择分类输出目录", self.output_edit.text().strip()
        )
        if folder:
            self.output_edit.setText(folder)

    def _save_runtime_choices(self):
        self.settings = normalize_image_classifier_settings({
            **self.settings,
            "recursive": self.recursive_checkbox.isChecked(),
            "operation": self.operation_combo.currentData(),
            "last_sources": self.source_paths(),
            "last_output_dir": self.output_edit.text().strip(),
        })
        self.settingsChanged.emit(dict(self.settings))

    def start_analysis(self):
        if self._busy():
            return
        output_dir = self.output_edit.text().strip()
        paths = discover_images(
            self.source_paths(), output_dir, self.recursive_checkbox.isChecked()
        )
        if not paths:
            QtWidgets.QMessageBox.information(
                self, "图片智能分类", "没有找到支持的图片文件。"
            )
            return
        self._save_runtime_choices()
        self.results = []
        self._render_results()
        self._set_busy(True)
        self.progress.setRange(0, len(paths))
        self.progress.setValue(0)
        self.status_label.setText(
            f"发现 {len(paths)} 张图片，正在后台加载模型并分析…"
        )
        self.logger(f"开始分析 {len(paths)} 张图片。")
        self.worker = _ClassificationWorker(paths, self.settings, self)
        self.worker.progressChanged.connect(self._progress_changed)
        self.worker.completed.connect(self._analysis_completed)
        self.worker.failed.connect(self._work_failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _progress_changed(self, current, total, message):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        else:
            self.progress.setRange(0, 0)
        self.status_label.setText(message)

    def _analysis_completed(self, results):
        self.results = list(results or [])
        self._render_results()
        pending = sum(
            result.get("assigned_category") == PENDING_CATEGORY
            for result in self.results
        )
        errors = sum(result.get("status") == "error" for result in self.results)
        self.status_label.setText(
            f"分析完成：{len(self.results)} 张；待人工确认 {pending} 张；"
            f"读取失败 {errors} 张。双击结果可打开原图。"
        )
        self.logger(
            f"图片分类分析完成：{len(self.results)} 张，待确认 {pending} 张，"
            f"失败 {errors} 张。"
        )
        self.notifier(
            "图片智能分类",
            f"分析完成：{len(self.results)} 张，待人工确认 {pending} 张。",
            False,
        )
        self.apply_button.setEnabled(self._has_unapplied_results())

    def _worker_finished(self):
        self.worker = None
        self._set_busy(False)

    def _status_text(self, result):
        return {
            "ready": "可自动分类",
            "low_confidence": "相似度低",
            "ambiguous": "分类接近",
            "manual": "已人工指定",
            "error": "读取失败",
            "completed": "已整理",
        }.get(result.get("status"), result.get("reason", ""))

    def _visible_results(self):
        mode = str(self.filter_combo.currentData() or "all")
        if mode == "all":
            return list(self.results)
        if mode == "pending":
            return [
                result for result in self.results
                if result.get("assigned_category") == PENDING_CATEGORY
            ]
        return [result for result in self.results if result.get("status") == mode]

    def _render_results(self):
        visible = self._visible_results()
        self.result_table.setRowCount(len(visible))
        colors = {
            "ready": QtGui.QColor("#D9EAD3"),
            "manual": QtGui.QColor("#D9EAD3"),
            "low_confidence": QtGui.QColor("#FCE5CD"),
            "ambiguous": QtGui.QColor("#FCE5CD"),
            "error": QtGui.QColor("#F4CCCC"),
            "completed": QtGui.QColor("#D9EAD3"),
        }
        for row, result in enumerate(visible):
            values = (
                Path(result.get("source", "")).name,
                result.get("suggested_category", ""),
                result.get("assigned_category", ""),
                f"{float(result.get('similarity', 0.0)):.3f}",
                f"{float(result.get('margin', 0.0)):.3f}",
                self._status_text(result),
            )
            background = colors.get(result.get("status"), QtGui.QColor("#FFFFFF"))
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setBackground(background)
                if column == self.COL_FILE:
                    item.setData(QtCore.Qt.UserRole, result)
                    item.setToolTip(result.get("source", ""))
                elif column in {self.COL_SUGGESTED, self.COL_ASSIGNED}:
                    item.setToolTip(str(value))
                self.result_table.setItem(row, column, item)

    def _selected_results(self):
        selected = []
        for index in self.result_table.selectionModel().selectedRows():
            item = self.result_table.item(index.row(), self.COL_FILE)
            result = item.data(QtCore.Qt.UserRole) if item else None
            if isinstance(result, dict) and result not in selected:
                selected.append(result)
        return selected

    def _change_selected_categories(self):
        selected = self._selected_results()
        if not selected:
            QtWidgets.QMessageBox.information(self, "修改分类", "请先选择结果行。")
            return
        categories = [
            category["path"]
            for category in flatten_categories(self.settings["categories"])
        ]
        categories.append(PENDING_CATEGORY)
        value, accepted = QtWidgets.QInputDialog.getItem(
            self, "修改分类", f"将选中的 {len(selected)} 张图片设为：",
            categories, 0, False,
        )
        if not accepted or not value:
            return
        for result in selected:
            result["assigned_category"] = value
            result["status"] = "manual"
            result["reason"] = "人工指定"
        self._render_results()

    def _first_selected_result(self):
        selected = self._selected_results()
        return selected[0] if selected else None

    def _update_preview(self):
        result = self._first_selected_result()
        if result is None:
            self.preview_label.setPixmap(QtGui.QPixmap())
            self.preview_label.setText("选择一条结果查看图片")
            self.preview_detail.clear()
            return
        pixmap = QtGui.QPixmap(result.get("source", ""))
        if pixmap.isNull():
            self.preview_label.setPixmap(QtGui.QPixmap())
            self.preview_label.setText("图片预览失败")
        else:
            self.preview_label.setText("")
            self.preview_label.setPixmap(pixmap.scaled(
                self.preview_label.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            ))
        self.preview_detail.setText(
            f"建议：{result.get('suggested_category') or '—'}\n"
            f"最终：{result.get('assigned_category') or '—'}\n"
            f"说明：{result.get('reason') or '—'}"
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.result_table.selectedItems():
            self._update_preview()

    def _open_selected_source(self):
        result = self._first_selected_result()
        if result is None:
            return
        path = result.get("source", "")
        try:
            os.startfile(path)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "打开原图", str(error))

    def apply_results(self):
        if self._busy() or not self._has_unapplied_results():
            return
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QtWidgets.QMessageBox.warning(self, "执行整理", "请先选择输出目录。")
            return
        operation = str(self.operation_combo.currentData() or "copy")
        if operation == "move":
            answer = QtWidgets.QMessageBox.question(
                self,
                "移动原图片",
                "移动会改变原文件位置。已经核对分类结果，确定继续吗？",
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        self._save_runtime_choices()
        self._set_busy(True)
        self.progress.setRange(0, len(self.results))
        self.progress.setValue(0)
        self.apply_worker = _ApplyWorker(
            self.results, output_dir, operation, self
        )
        self.apply_worker.progressChanged.connect(self._progress_changed)
        self.apply_worker.completed.connect(self._apply_completed)
        self.apply_worker.failed.connect(self._work_failed)
        self.apply_worker.finished.connect(self._apply_worker_finished)
        self.apply_worker.start()

    def _apply_completed(self, report):
        completed = report.get("completed", [])
        failed = report.get("failed", [])
        completed_sources = {item.get("source") for item in completed}
        for result in self.results:
            if result.get("source") in completed_sources:
                result["status"] = "completed"
                result["reason"] = "已完成整理"
        self._render_results()
        action = "移动" if self.operation_combo.currentData() == "move" else "复制"
        self.status_label.setText(
            f"整理完成：已{action} {len(completed)} 张，失败 {len(failed)} 张。"
        )
        self.logger(
            f"图片分类整理完成：已{action} {len(completed)} 张，失败 {len(failed)} 张。"
        )
        self.notifier(
            "图片智能分类",
            f"已{action} {len(completed)} 张图片；失败 {len(failed)} 张。",
            bool(failed),
        )
        if failed:
            details = "\n".join(
                f"{Path(item.get('source', '')).name}：{item.get('error', '')}"
                for item in failed[:20]
            )
            QtWidgets.QMessageBox.warning(
                self, "部分图片整理失败", details
            )

    def _apply_worker_finished(self):
        self.apply_worker = None
        self._set_busy(False)

    def _work_failed(self, message):
        self.status_label.setText(f"失败：{message}")
        self.logger(f"图片分类失败：{message}")
        self.notifier("图片智能分类失败", message, True)
        QtWidgets.QMessageBox.critical(self, "图片智能分类", message)

    def _set_busy(self, busy):
        self.analyze_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy and self._has_unapplied_results())
        self.stop_button.setEnabled(busy)
        self.add_files_button.setEnabled(not busy)
        self.add_folder_button.setEnabled(not busy)
        self.output_button.setEnabled(not busy)

    def stop_current_work(self):
        worker = self.worker if self.worker is not None else self.apply_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            self.status_label.setText("正在安全停止；当前批次结束后停止…")

    def closeEvent(self, event):
        if self._busy():
            QtWidgets.QMessageBox.information(
                self,
                "图片智能分类",
                "任务仍在后台运行。窗口会暂时隐藏，完成后将发送桌面提醒。",
            )
        event.accept()
