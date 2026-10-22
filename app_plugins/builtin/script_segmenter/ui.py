from pathlib import Path
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from model.ClipboardHelper import set_internal_clipboard_text
from .engine import CONFIG_KEY, format_result, normalize_settings, run_segmentation
from .duration_warning import CopyDurationGuard, duration_check, duration_text, warning_text


class ScriptSegmenterDialog(QtWidgets.QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context, self.entries, self.handle, self._loading = context, [], None, False
        self.copy_guard = CopyDurationGuard(self, self)
        self.setWindowTitle("智能文案分段助手")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowMinMaxButtonsHint)
        self.resize(1080, 730)
        layout = QtWidgets.QVBoxLayout(self)
        menu_bar = QtWidgets.QMenuBar(self)
        menu = menu_bar.addMenu("文案")
        menu.addAction("新建 / 粘贴文案", self.new_manual)
        menu.addAction("读取主界面所选任务", self.read_selected_tasks)
        menu.addSeparator()
        menu.addAction("保存当前分段结果…", self.save_result)
        menu = menu_bar.addMenu("结果")
        menu.addAction("复制当前全部分段", self.copy_all)
        menu.addAction("复制选中分段", lambda: self.copy_selected())
        menu.addAction("复制选中文案（不带段号/秒数）", lambda: self.copy_selected(False))
        layout.setMenuBar(menu_bar)
        note = QtWidgets.QLabel("原有四种时长规则保留；可选择减少段数或优先完整句。只分段，不改写、不翻译。")
        note.setWordWrap(True)
        layout.addWidget(note)
        tools = QtWidgets.QHBoxLayout()
        self.mode = QtWidgets.QComboBox()
        for label, values in [("自动（优先10秒，短尾6/8秒）", [6, 8, 10]), ("仅10秒", [10]), ("仅8秒", [8]), ("仅6秒", [6])]:
            self.mode.addItem(label, values)
        tools.addWidget(self.mode)
        tools.addWidget(QtWidgets.QLabel("朗读速度"))
        self.speed = QtWidgets.QSpinBox()
        self.speed.setRange(40, 300)
        self.speed.setSuffix(" 词/分钟")
        self.speed.setToolTip("文字按完整词估计；数字按位折算朗读词数，数字内连接符也预留朗读时间，原文不会改写。可以根据实际声音调整速度；建议秒数仍为估算。")
        tools.addWidget(self.speed)
        more = QtWidgets.QToolButton()
        more.setText("更多参数")
        more.setCheckable(True)
        tools.addWidget(more)
        tools.addStretch()
        save_defaults = QtWidgets.QPushButton("保存默认参数")
        save_defaults.clicked.connect(self.save_settings)
        tools.addWidget(save_defaults)
        layout.addLayout(tools)
        strategy_row = QtWidgets.QHBoxLayout()
        strategy_row.addWidget(QtWidgets.QLabel("断句策略"))
        self.strategy = QtWidgets.QComboBox()
        self.strategy.addItem("减少段数（原策略）", "fewest")
        self.strategy.addItem("优先完整句（放不下就退回停顿处）", "punctuation_first")
        self.strategy.setToolTip("新策略优先在逗号、句号等停顿处结束，不为凑满而开始下一句；单句太长或没有标点时按完整词拆分。自动模式按估时选6/8/10秒，可能增加段数。不能替代实际音频计时。")
        strategy_row.addWidget(self.strategy)
        strategy_row.addStretch()
        layout.addLayout(strategy_row)
        advanced = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(advanced)
        row.setContentsMargins(0, 0, 0, 0)
        self.pause, self.padding = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        for spin in (self.pause, self.padding):
            spin.setRange(0, 2)
            spin.setSingleStep(.1)
            spin.setSuffix(" 秒")
        self.cjk_speed = QtWidgets.QSpinBox()
        self.cjk_speed.setRange(80, 600)
        self.cjk_speed.setSuffix(" 字/分钟")
        for label, widget in [("句末停顿", self.pause), ("每段预留", self.padding), ("无空格语言速度", self.cjk_speed)]:
            row.addWidget(QtWidgets.QLabel(label))
            row.addWidget(widget)
        row.addStretch()
        self.total_limit = QtWidgets.QSpinBox()
        self.total_limit.setRange(30, 3600)
        self.total_limit.setSuffix(' 秒')
        self.total_limit.setToolTip('整篇朗读时长上限，默认180秒。超时提醒，不改写或截断文案。')
        row.addWidget(QtWidgets.QLabel('总朗读上限'))
        row.addWidget(self.total_limit)
        advanced.hide()
        more.toggled.connect(advanced.setVisible)
        layout.addWidget(advanced)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        left, right = QtWidgets.QWidget(), QtWidgets.QWidget()
        ll, rl = QtWidgets.QVBoxLayout(left), QtWidgets.QVBoxLayout(right)
        for panel in (ll, rl):
            panel.setContentsMargins(0, 0, 0, 0)
        self.sources = QtWidgets.QListWidget()
        self.sources.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.sources.setMaximumHeight(150)
        self.sources.currentRowChanged.connect(self.show_entry)
        ll.addWidget(self.sources)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("任务文案来源"))
        self.source_field = QtWidgets.QComboBox()
        self.source_field.addItem("任务语音（原文）", "task_audio_text")
        self.source_field.addItem("任务名称", "task_name")
        self.source_field.currentIndexChanged.connect(self.change_source)
        row.addWidget(self.source_field)
        ll.addLayout(row)
        self.input = QtWidgets.QPlainTextEdit()
        self.input.setPlaceholderText("粘贴完整文案；或者在主界面任务右键选择“智能文案分段”。")
        self.input.textChanged.connect(self.edit_source)
        ll.addWidget(self.input, 1)
        label = QtWidgets.QLabel("编辑仅影响本次分段，不会回写登记表。")
        label.setWordWrap(True)
        ll.addWidget(label)
        row = QtWidgets.QHBoxLayout()
        self.generate_button = QtWidgets.QPushButton("分段当前文案")
        self.generate_all_button = QtWidgets.QPushButton("分段全部任务")
        self.generate_button.clicked.connect(lambda: self.generate(False))
        self.generate_all_button.clicked.connect(lambda: self.generate(True))
        row.addWidget(self.generate_button)
        row.addWidget(self.generate_all_button)
        ll.addLayout(row)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        rl.addWidget(self.summary)
        self.duration_note = QtWidgets.QLabel()
        self.duration_note.setWordWrap(True)
        self.duration_note.hide()
        rl.addWidget(self.duration_note)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["段", "生成秒数", "预计朗读", "原文"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        for column, width in enumerate([42, 100, 92]):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.result_menu)
        self.table.itemSelectionChanged.connect(self.show_detail)
        shortcut = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, self.table)
        shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.copy_selected)
        rl.addWidget(self.table, 1)
        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(170)
        self.detail.setPlaceholderText("选中分段查看完整原文；Ctrl+C复制选中分段。")
        rl.addWidget(self.detail)
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        self.copy_button, self.save_button = QtWidgets.QPushButton("复制全部分段"), QtWidgets.QPushButton("保存结果…")
        self.copy_button.clicked.connect(self.copy_all)
        self.save_button.clicked.connect(self.save_result)
        row.addWidget(self.copy_button)
        row.addWidget(self.save_button)
        rl.addLayout(row)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([340, 700])
        layout.addWidget(splitter, 1)
        self.status = QtWidgets.QLabel("本地处理，不需要API Key，不会上传文案。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        saved = normalize_settings(context.load_config().get(CONFIG_KEY))
        self.mode.setCurrentIndex(next((i for i in range(self.mode.count()) if self.mode.itemData(i) == saved["durations"]), 0))
        self.strategy.setCurrentIndex(self.strategy.findData(saved["boundary_strategy"]))
        self.speed.setValue(int(saved["words_per_minute"]))
        self.pause.setValue(saved["pause_seconds"])
        self.padding.setValue(saved["padding_seconds"])
        self.cjk_speed.setValue(int(saved["characters_per_minute"]))
        self.total_limit.setValue(saved['total_limit_seconds'])
        for widget in [self.mode, self.strategy, self.speed, self.pause, self.padding, self.cjk_speed, self.total_limit]:
            signal = widget.currentIndexChanged if widget in (self.mode, self.strategy) else widget.valueChanged
            signal.connect(self.invalidate_results)
        self.new_manual()

    def settings(self):
        durations = self.mode.currentData()
        return normalize_settings({"durations": durations, "preferred_seconds": max(durations),
            "words_per_minute": self.speed.value(), "pause_seconds": self.pause.value(),
            "padding_seconds": self.padding.value(), "characters_per_minute": self.cjk_speed.value(),
            "boundary_strategy": self.strategy.currentData(), 'total_limit_seconds': self.total_limit.value()})

    def current_entry(self):
        row = self.sources.currentRow()
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def is_busy(self):
        return self.handle is not None and self.handle.isRunning()

    def new_manual(self):
        if self.is_busy():
            return
        self.entries = [{"key": "manual", "label": "自由文案", "text": "", "target_dir": "", "originals": {}}]
        self.rebuild_sources()

    def rebuild_sources(self):
        self.sources.clear()
        for entry in self.entries:
            item = QtWidgets.QListWidgetItem(entry["label"])
            item.setToolTip(entry["label"])
            self.sources.addItem(item)
        self.sources.setCurrentRow(0)

    def load_tasks(self, targets, generate=False):
        if self.is_busy():
            self.status.setText("正在分段，请完成后再载入其他任务；关闭窗口不会中断。")
            return False
        if not targets:
            self.status.setText("没有选中的已加载任务，请先在主界面选择任务。")
            return False
        entries = []
        for index, target in enumerate(targets):
            task = target["task"]
            originals = {field: str(getattr(task, field, "") or "") for field in ("task_audio_text", "task_name")}
            entries.append({"key": str(index), "label": str(target["label"]), "originals": originals,
                            "text": originals[self.source_field.currentData()], "target_dir": target["target_dir"]})
        self.entries = entries
        self.rebuild_sources()
        if generate:
            self.generate(True)
        return True

    def read_selected_tasks(self):
        self.load_tasks(self.context.task_targets(self.context.selected_task_rows()), generate=True)

    def show_entry(self, _row):
        entry = self.current_entry()
        self._loading = True
        self.input.setPlainText(entry["text"] if entry else "")
        self._loading = False
        self.render_result()

    def edit_source(self):
        if not self._loading and self.current_entry() is not None:
            self.current_entry()["text"] = self.input.toPlainText()
            self.current_entry().pop("result", None)
            self.current_entry().pop("error", None)
            self.refresh_source_labels()
            self.render_result()

    def change_source(self):
        if self.is_busy():
            return
        for entry in self.entries:
            if entry["originals"]:
                entry["text"] = entry["originals"][self.source_field.currentData()]
                entry.pop("result", None)
                entry.pop("error", None)
        self.refresh_source_labels()
        self.show_entry(self.sources.currentRow())

    def invalidate_results(self, *_args):
        for entry in self.entries:
            entry.pop("result", None)
            entry.pop("error", None)
        self.refresh_source_labels()
        self.render_result()

    def refresh_source_labels(self):
        for index, entry in enumerate(self.entries):
            suffix = f" · {len(entry['result']['segments'])}段" if entry.get("result") else (" · 未生成" if entry.get("error") else "")
            self.sources.item(index).setText(entry["label"] + suffix)

    def generate(self, all_entries=False):
        if self.is_busy():
            return
        entries = self.entries if all_entries else [self.current_entry()]
        entries = [entry for entry in entries if entry is not None]
        if not entries:
            return
        try:
            self._completion_message = "分段结束。"
            self.handle = self.context.submit_background_task(
                title=f"文案分段 · {len(entries)}篇", runner=run_segmentation,
                payload={"entries": [{"key": entry["key"], "text": entry["text"]} for entry in entries],
                         "settings": self.settings()}, kind="cpu", retryable=False)
            self.handle.completed.connect(self.on_completed)
            self.handle.failed.connect(self.on_failed)
            self.handle.finished.connect(self.on_finished)
            self.handle.progressChanged.connect(lambda message, _percent: self.status.setText(message))
            self.set_busy(True)
        except Exception as error:
            self.context.log("提交文案分段失败：\n" + traceback.format_exc())
            self.status.setText(str(error))

    def set_busy(self, value):
        for widget in [self.input, self.mode, self.strategy, self.speed, self.pause, self.padding, self.cjk_speed,
                       self.source_field, self.generate_button, self.generate_all_button, self.total_limit]:
            widget.setEnabled(not value)
        self.status.setText("后台分段中；关闭此窗口也会继续。" if value else "分段完成。")

    def on_completed(self, output):
        by_key = {entry["key"]: entry for entry in self.entries}
        for item in output["entries"]:
            entry = by_key.get(item["key"])
            if entry is not None:
                entry.pop("result", None)
                entry.pop("error", None)
                entry.update({key: item[key] for key in ("result", "error") if key in item})
        errors = sum("error" in item for item in output["entries"])
        warnings = sum(item.get("result", {}).get("warning_count", 0) for item in output["entries"])
        self.refresh_source_labels()
        self._completion_message = f"完成 {len(output['entries'])}篇；未生成 {errors}篇，超时需处理 {warnings}段。"
        self.context.log(self._completion_message)
        self.render_result()

    def on_failed(self, message):
        self._completion_message = "分段未完成：" + message
        self.context.log(self._completion_message)

    def on_finished(self):
        self.handle = None
        self.set_busy(False)
        self.status.setText(self._completion_message)

    def render_result(self):
        self.table.setRowCount(0)
        self.detail.clear()
        entry = self.current_entry() or {}
        result = entry.get("result")
        self.duration_note.hide()
        self.copy_button.setEnabled(bool(result))
        self.save_button.setEnabled(bool(result))
        if not result:
            self.summary.setText(entry.get("error") or "修改文案/参数后点击分段。建议秒数是估算，实际声音需试听。")
            return
        self.summary.setText(f"{len(result['segments'])}段 · 预计朗读 {result['estimated_seconds']:.1f}秒 · 视频合计 {result['video_seconds']}秒"
                             + (f" · {result['warning_count']}段超时需处理" if result["warning_count"] else " · 原文完整性已校验"))
        check = duration_check(result)
        if check['seconds'] > check['limit']:
            self.duration_note.setText(warning_text(check) + ' 实际时长以生成音频为准；复制前会再次确认。')
            self.duration_note.setStyleSheet('color:#8a3600;background:#fff0db;padding:6px;')
            self.duration_note.show()
        elif check['seconds'] >= check['limit'] - 10:
            self.duration_note.setText(f"接近{duration_text(check['limit'])}上限，建议留余量；实际时长以生成音频为准。")
            self.duration_note.setStyleSheet('color:#735900;background:#fff9dc;padding:6px;')
            self.duration_note.show()
        for segment in result["segments"]:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [str(segment["index"]), f"{segment['seconds']}秒" if not segment["warning"] else "超时需处理",
                      f"{segment['estimated_seconds']:.1f}秒", segment["text"]]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                timing = (f"\n朗读折算 {segment['spoken_word_count']:g} 词（数字按位计入；无空格语言另按字数估时）"
                          if "spoken_word_count" in segment else "")
                item.setToolTip((segment["warning"] or f"{segment['boundary']}处结束 · {segment['word_count']}个原文词/文本单元")
                                + timing + f"\n{segment['text']}")
                if segment["warning"]:
                    item.setBackground(QtGui.QColor("#ffe0b2"))
                    item.setForeground(QtGui.QColor("#522800"))
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, min(110, max(48, self.table.rowHeight(row))))

    def selected_indices(self):
        return sorted({index.row() + 1 for index in self.table.selectionModel().selectedRows()})

    def show_detail(self):
        result = (self.current_entry() or {}).get("result")
        self.detail.setPlainText(format_result(result, self.selected_indices()) if result else "")

    def copy_all(self):
        result = (self.current_entry() or {}).get("result")
        if result:
            def approved():
                if (self.current_entry() or {}).get('result') is result:
                    set_internal_clipboard_text(format_result(result))
                    self.status.setText("已复制全部分段（含段号和建议秒数）。")
            self.copy_guard.request([duration_check(result)], approved)

    def copy_selected(self, with_labels=True):
        result = (self.current_entry() or {}).get("result")
        indices = self.selected_indices()
        if result and indices:
            def approved():
                if (self.current_entry() or {}).get('result') is result:
                    set_internal_clipboard_text(format_result(result, indices, with_labels))
                    self.status.setText(f"已复制 {len(indices)}段。")
            self.copy_guard.request([duration_check(result, indices)], approved)

    def result_menu(self, point):
        index = self.table.indexAt(point)
        if index.isValid() and not self.table.selectionModel().isRowSelected(index.row(), QtCore.QModelIndex()):
            self.table.selectRow(index.row())
        menu = QtWidgets.QMenu(self.table)
        menu.addAction("复制选中分段", lambda: self.copy_selected())
        menu.addAction("复制选中文案（不带段号/秒数）", lambda: self.copy_selected(False))
        menu.addSeparator()
        menu.addAction("全选", self.table.selectAll)
        menu.addAction("取消选择", self.table.clearSelection)
        menu.exec(self.table.viewport().mapToGlobal(point))

    def save_settings(self):
        try:
            original = self.context.load_config()
            saved = self.context.patch_config({CONFIG_KEY: self.settings()}, expected=original)
            self.status.setText("默认参数已保存，仅更新本插件参数。" if saved else "参数未保存，请查看主程序日志。")
        except Exception as error:
            self.context.log("保存文案分段参数失败：\n" + traceback.format_exc())
            self.status.setText("参数未保存：" + str(error))

    def save_result(self):
        entry = self.current_entry() or {}
        result = entry.get("result")
        if not result:
            return
        default = str(Path(entry["target_dir"]) / "Flow文案分段.txt") if entry.get("target_dir") else "Flow文案分段.txt"
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存分段结果", default, "文本文件 (*.txt)")
        if not filename:
            return
        try:
            output = QtCore.QSaveFile(filename)
            if not output.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            data = ("说明：建议秒数按朗读速度估算，实际声音请试听。\n\n" + format_result(result)).encode("utf-8-sig")
            if output.write(data) != len(data) or not output.commit():
                raise OSError(output.errorString())
            self.status.setText("已保存：" + filename)
        except Exception as error:
            self.context.log("保存分段结果失败：\n" + traceback.format_exc())
            QtWidgets.QMessageBox.warning(self, "保存失败", str(error))
