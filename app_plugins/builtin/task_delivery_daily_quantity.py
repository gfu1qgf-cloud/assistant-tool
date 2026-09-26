"""Nonblocking status and manual refresh for daily quantity statistics."""

from qt_compat import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.DailyQuantityStats import (
    reconcile_daily_quantity,
    scan_daily_drive_date,
    scan_external_video_folder,
)
from model.TaskResultOrganizer import get_upload_batch


class _NoWheelComboBox(QtWidgets.QComboBox):
    """Scrolling the inventory must not silently change a classification."""

    def wheelEvent(self, event):
        event.ignore()


class DailyQuantityThread(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, config, root, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.root = str(root)

    def run(self):
        try:
            self.completed.emit(reconcile_daily_quantity(self.config, self.root))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DailyQuantityFolderThread(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, config, root, link, day, slot, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.root = str(root)
        self.link = link
        self.day = day
        self.slot = slot

    def run(self):
        try:
            self.completed.emit(scan_external_video_folder(
                self.config, self.root, self.link, self.day, self.slot
            ))
        except (Exception, SystemExit) as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DailyQuantityDateThread(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, config, root, day, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.root = str(root)
        self.day = day

    def run(self):
        try:
            self.completed.emit(scan_daily_drive_date(
                self.config, self.root, self.day
            ))
        except (Exception, SystemExit) as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DailyQuantityDialog(QtWidgets.QDialog):
    refresh_requested = QtCore.pyqtSignal()
    scan_requested = QtCore.pyqtSignal(str, str, str)
    scan_date_requested = QtCore.pyqtSignal(str)
    view_date_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("每日数量统计")
        self.resize(1040, 690)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "根据上传历史的首次交付日期与批次计数；刷新会重新读取本地任务表。"
            "只填写本人分类的三个时段数量，不改定额和合计。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QtWidgets.QLabel("尚未刷新")
        layout.addWidget(self.status)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.refresh_button = QtWidgets.QPushButton("刷新并自动修正")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self.refresh_button)

        source_box = QtWidgets.QGroupBox("网盘视频清单")
        source_layout = QtWidgets.QVBoxLayout(source_box)
        source_note = QtWidgets.QLabel(
            "扫描所选日期会核对网盘实有视频；已保存的分类不变，未分类的可在清单中选择。"
            "最左侧勾选框表示计入数量，和选中表格行是两回事；可右键批量操作。"
            "审核暂存和疑似旧任务修订版默认不计数；重复内容会标出供你核对。"
            "日期目录目前只按月日命名，跨年复用时请留意旧视频。只读取清单，不下载。"
        )
        source_note.setWordWrap(True)
        source_layout.addWidget(source_note)
        date_row = QtWidgets.QHBoxLayout()
        self.folder_link = QtWidgets.QComboBox()
        self.folder_link.setEditable(True)
        self.folder_link.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.folder_link.lineEdit().setPlaceholderText("粘贴 Google Drive 文件夹链接，或选择已导入的文件夹")
        batch_date, batch_slot = get_upload_batch({})
        self.folder_day = QtWidgets.QDateEdit()
        self.folder_day.setDisplayFormat("yyyy-MM-dd")
        self.folder_day.setCalendarPopup(True)
        self.folder_day.setDate(QtCore.QDate(batch_date.year, batch_date.month, batch_date.day))
        self.scan_date_button = QtWidgets.QPushButton("扫描日期目录")
        self.scan_date_button.clicked.connect(lambda: self.scan_date_requested.emit(
            self.folder_day.date().toString("yyyy-MM-dd")
        ))
        self.view_date_button = QtWidgets.QPushButton("查看已存清单")
        self.view_date_button.clicked.connect(lambda: self.view_date_requested.emit(
            self.folder_day.date().toString("yyyy-MM-dd")
        ))
        date_row.addWidget(QtWidgets.QLabel("交付日期："))
        date_row.addWidget(self.folder_day)
        date_row.addWidget(self.scan_date_button)
        date_row.addWidget(self.view_date_button)
        date_row.addStretch(1)
        source_layout.addLayout(date_row)
        source_row = QtWidgets.QHBoxLayout()
        self.folder_slot = QtWidgets.QComboBox()
        for slot, label in (("01", "12点"), ("02", "18点"), ("03", "24点")):
            self.folder_slot.addItem(label, slot)
        self.folder_slot.setCurrentIndex(self.folder_slot.findData(batch_slot))
        self.scan_button = QtWidgets.QPushButton("导入指定文件夹")
        self.scan_button.clicked.connect(self._request_scan)
        source_row.addWidget(self.folder_link, 1)
        source_row.addWidget(self.folder_slot)
        source_row.addWidget(self.scan_button)
        source_layout.addLayout(source_row)
        self.folder_status = QtWidgets.QLabel("尚未扫描文件夹")
        source_layout.addWidget(self.folder_status)
        self.external_table = QtWidgets.QTableWidget(0, 8)
        self.external_table.setHorizontalHeaderLabels([
            "计数", "文件夹", "视频", "交付日期", "时段", "统计分页", "统计类别", "状态"
        ])
        self.external_table.setAlternatingRowColors(True)
        self.external_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.external_table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.external_table.horizontalHeader().setStretchLastSection(True)
        self.external_table.setColumnWidth(0, 52)
        self.external_table.setColumnWidth(2, 250)
        self.external_table.setColumnWidth(3, 105)
        self.external_table.setColumnWidth(4, 55)
        self.external_table.setColumnWidth(5, 125)
        self.external_table.setColumnWidth(6, 155)
        self.external_table.cellDoubleClicked.connect(self._open_external_video)
        self.external_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.external_table.customContextMenuRequested.connect(self._show_external_context_menu)
        source_layout.addWidget(self.external_table, 1)
        bulk_row = QtWidgets.QHBoxLayout()
        self._category_options = {}
        self.bulk_sheet = _NoWheelComboBox()
        self.bulk_category = _NoWheelComboBox()
        self.bulk_sheet.addItem("选择统计分页", "")
        self.bulk_category.addItem("选择统计类别", "")
        self.bulk_sheet.currentIndexChanged.connect(self._refresh_bulk_categories)
        self.apply_bulk_button = QtWidgets.QPushButton("应用到选中视频")
        self.apply_bulk_button.clicked.connect(self._apply_bulk_category)
        bulk_row.addWidget(QtWidgets.QLabel("批量分类："))
        bulk_row.addWidget(self.bulk_sheet)
        bulk_row.addWidget(self.bulk_category)
        bulk_row.addWidget(self.apply_bulk_button)
        source_layout.addLayout(bulk_row)
        self.save_external_button = QtWidgets.QPushButton("保存分类并刷新数量")
        self.save_external_button.clicked.connect(self.refresh_requested.emit)
        self.copy_list_button = QtWidgets.QPushButton("复制当前清单")
        self.copy_list_button.clicked.connect(self._copy_current_list)
        action_row = QtWidgets.QHBoxLayout()
        action_row.addWidget(self.copy_list_button)
        action_row.addWidget(self.save_external_button, 1)
        source_layout.addLayout(action_row)
        layout.addWidget(source_box, 2)

    def _request_scan(self):
        link = self.folder_link.currentText().strip()
        if not link:
            QtWidgets.QMessageBox.warning(self, "扫描文件夹", "请先粘贴 Google Drive 文件夹链接。")
            return
        self.scan_requested.emit(
            link, self.folder_day.date().toString("yyyy-MM-dd"),
            str(self.folder_slot.currentData()),
        )

    def show_external_sources(self, sources):
        current = self.folder_link.currentText().strip()
        self.folder_link.clear()
        for source in sources:
            link = str(source.get("link") or "").strip()
            if link:
                self.folder_link.addItem(link)
                self.folder_link.setItemData(
                    self.folder_link.count() - 1,
                    f"{source.get('name') or ''} · 上次扫描 {source.get('last_scan_at') or ''}",
                    QtCore.Qt.ItemDataRole.ToolTipRole,
                )
        if current:
            self.folder_link.setCurrentText(current)
        elif self.folder_link.count():
            self.folder_link.setCurrentIndex(0)

    def show_external_records(self, records, day=None):
        self.external_table.setRowCount(0)
        if day:
            records = [item for item in records if str(item.get("batch_date") or "") == day]
        for record in sorted(records, key=lambda item: (
            str(item.get("batch_date") or ""), str(item.get("file_name") or "")
        )):
            row = self.external_table.rowCount()
            self.external_table.insertRow(row)
            include = QtWidgets.QTableWidgetItem()
            include.setFlags((include.flags() | QtCore.Qt.ItemIsUserCheckable)
                             & ~QtCore.Qt.ItemIsEditable)
            include.setCheckState(QtCore.Qt.Checked if record.get("included", True)
                                  else QtCore.Qt.Unchecked)
            include.setToolTip("勾选表示计入每日数量；请先确认交付时段为 01、02 或 03。")
            include.setData(QtCore.Qt.UserRole, str(record.get("id") or ""))
            self.external_table.setItem(row, 0, include)
            for col, key in ((1, "folder_name"), (2, "file_name"), (3, "batch_date"),
                             (4, "batch_slot")):
                display = record.get("relative_path") if col == 2 else record.get(key)
                if col == 4 and display == "00":
                    display = ""
                item = QtWidgets.QTableWidgetItem(
                    str(display or "") if col == 4
                    else str(display or record.get(key) or "")
                )
                if col in (1, 2):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if record.get("daily_scan_date") and col in (3, 4) and (
                    col == 3 or record.get("batch_slot") in {"01", "02", "03"}
                ):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if col == 2:
                    item.setToolTip(str(record.get("drive_link") or ""))
                    item.setData(QtCore.Qt.UserRole, str(record.get("drive_link") or ""))
                    item.setData(QtCore.Qt.UserRole + 1, str(record.get("file_name") or ""))
                self.external_table.setItem(row, col, item)
            sheet_combo = _NoWheelComboBox(self.external_table)
            category_combo = _NoWheelComboBox(self.external_table)
            for combo in (sheet_combo, category_combo):
                combo.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
                combo.customContextMenuRequested.connect(
                    lambda position, widget=combo: self._show_external_context_menu(
                        self.external_table.viewport().mapFromGlobal(
                            widget.mapToGlobal(position)
                        )
                    )
                )
            self.external_table.setCellWidget(row, 5, sheet_combo)
            self.external_table.setCellWidget(row, 6, category_combo)
            self._set_combo_choices(
                sheet_combo, sorted(self._category_options),
                str(record.get("sheet") or ""), "选择统计分页",
            )
            self._set_combo_choices(
                category_combo,
                self._category_options.get(str(record.get("sheet") or ""), []),
                str(record.get("category") or ""), "选择统计类别",
            )
            sheet_combo.currentIndexChanged.connect(
                lambda _index, selected_row=row: self._row_sheet_changed(selected_row)
            )
            category_combo.currentIndexChanged.connect(
                lambda _index, selected_row=row: self._update_row_status(selected_row)
            )
            state = "待分类" if not record.get("sheet") or not record.get("category") else "已分类"
            if record.get("missing_from_folder"):
                state += " · 历史保留"
            if record.get("missing_from_daily"):
                state += " · 已移出日期目录，停计"
            if record.get("outside_daily_scan"):
                state += " · 不在日期目录，停计"
            if record.get("review_path"):
                state += " · 审核暂存"
            if record.get("daily_scan_date") and record.get("batch_slot") not in {"01", "02", "03"}:
                state += " · 时段待确认"
            if record.get("possible_duplicate"):
                state += " · 疑似重复"
            if record.get("possible_revision"):
                state += " · 疑似旧任务修订版"
            status_item = QtWidgets.QTableWidgetItem(state)
            status_item.setFlags(status_item.flags() & ~QtCore.Qt.ItemIsEditable)
            status_item.setData(QtCore.Qt.UserRole, state.partition(" · ")[2])
            self.external_table.setItem(row, 7, status_item)
        self.mark_external_edits_saved()

    @staticmethod
    def _set_combo_choices(combo, choices, selected, placeholder):
        selected = str(selected or "").strip()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, "")
        for choice in choices:
            choice = str(choice).strip()
            if choice and combo.findData(choice) < 0:
                combo.addItem(choice, choice)
        index = combo.findData(selected)
        if selected and index < 0:
            combo.addItem(f"⚠ 未找到：{selected}", selected)
            index = combo.count() - 1
            combo.setItemData(index, "当前表格中未找到此项；选择有效分类后再保存。",
                              QtCore.Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _combo_value(self, row, column):
        combo = self.external_table.cellWidget(row, column)
        return str(combo.currentData() or "").strip() if combo else ""

    def _row_sheet_changed(self, row):
        sheet = self._combo_value(row, 5)
        category_combo = self.external_table.cellWidget(row, 6)
        if category_combo is not None:
            self._set_combo_choices(
                category_combo, self._category_options.get(sheet, []),
                "", "选择统计类别",
            )
        self._update_row_status(row)

    def _update_row_status(self, row):
        item = self.external_table.item(row, 7)
        if item is None:
            return
        state = "已分类" if self._combo_value(row, 5) and self._combo_value(row, 6) else "待分类"
        suffix = str(item.data(QtCore.Qt.UserRole) or "")
        item.setText(state + (" · " + suffix if suffix else ""))

    def external_edits(self):
        result = []
        for row in range(self.external_table.rowCount()):
            def value(col):
                if col in (5, 6):
                    return self._combo_value(row, col)
                item = self.external_table.item(row, col)
                return item.text().strip() if item else ""
            include = self.external_table.item(row, 0)
            result.append({
                "id": include.data(QtCore.Qt.UserRole),
                "included": include.checkState() == QtCore.Qt.Checked,
                "batch_date": value(3), "batch_slot": value(4),
                "sheet": value(5), "category": value(6),
            })
        return result

    def changed_external_edits(self):
        """Navigation must not resave every unchanged (possibly incomplete) row."""
        return [edit for edit in self.external_edits()
                if edit != self._saved_external_edits.get(str(edit["id"]))]

    def mark_external_edits_saved(self):
        self._saved_external_edits = {
            str(edit["id"]): edit for edit in self.external_edits()
        }

    def _apply_bulk_category(self):
        sheet = str(self.bulk_sheet.currentData() or "").strip()
        category = str(self.bulk_category.currentData() or "").strip()
        rows = sorted({index.row() for index in self.external_table.selectedIndexes()})
        if not rows or not sheet or not category:
            QtWidgets.QMessageBox.information(
                self, "批量分类", "请先选中视频，并选择统计分页及类别。"
            )
            return
        for row in rows:
            self._set_combo_choices(
                self.external_table.cellWidget(row, 5), sorted(self._category_options),
                sheet, "选择统计分页",
            )
            self._set_combo_choices(
                self.external_table.cellWidget(row, 6),
                self._category_options.get(sheet, []), category, "选择统计类别",
            )
            self._update_row_status(row)

    def set_category_options(self, options):
        self._category_options = dict(options or {})
        current = self.bulk_sheet.currentData()
        self._set_combo_choices(
            self.bulk_sheet, sorted(self._category_options), current, "选择统计分页"
        )
        self._refresh_bulk_categories()

        for row in range(self.external_table.rowCount()):
            sheet_combo = self.external_table.cellWidget(row, 5)
            category_combo = self.external_table.cellWidget(row, 6)
            sheet = self._combo_value(row, 5)
            category = self._combo_value(row, 6)
            self._set_combo_choices(
                sheet_combo, sorted(self._category_options), sheet, "选择统计分页"
            )
            self._set_combo_choices(
                category_combo, self._category_options.get(sheet, []),
                category, "选择统计类别",
            )

    def _refresh_bulk_categories(self, _index=None):
        sheet = str(self.bulk_sheet.currentData() or "").strip()
        current = str(self.bulk_category.currentData() or "").strip()
        choices = self._category_options.get(sheet, [])
        self._set_combo_choices(
            self.bulk_category, choices, current if current in choices else "",
            "选择统计类别",
        )

    def _open_external_video(self, row, column):
        if column != 2:
            return
        item = self.external_table.item(row, column)
        link = str(item.data(QtCore.Qt.UserRole) or "") if item else ""
        if link:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))

    def _selected_external_rows(self):
        return sorted(index.row() for index in self.external_table.selectionModel().selectedRows())

    def _show_external_context_menu(self, position):
        table = self.external_table
        index = table.indexAt(position)
        if index.isValid() and index.row() not in self._selected_external_rows():
            table.clearSelection()
            table.selectRow(index.row())
        menu = self._external_context_menu(self._selected_external_rows())
        menu.exec(table.viewport().mapToGlobal(position))

    def _external_context_menu(self, rows):
        menu = QtWidgets.QMenu(self)
        copy_name = menu.addAction("复制文件名")
        copy_name.triggered.connect(lambda: self._copy_external_values(rows, "name"))
        copy_link = menu.addAction("复制网盘链接")
        copy_link.triggered.connect(lambda: self._copy_external_values(rows, "link"))
        copy_both = menu.addAction("复制文件名和链接")
        copy_both.triggered.connect(lambda: self._copy_external_values(rows, "both"))
        for action in (copy_name, copy_link, copy_both):
            action.setEnabled(bool(rows))
        open_video = menu.addAction("在浏览器打开网盘视频")
        open_video.setEnabled(
            len(rows) == 1 and bool(self.external_table.item(rows[0], 2).data(QtCore.Qt.UserRole))
        )
        if len(rows) == 1:
            open_video.triggered.connect(lambda: self._open_external_video(rows[0], 2))
        menu.addSeparator()
        select_all = menu.addAction("选中全部可见行")
        select_all.triggered.connect(self.external_table.selectAll)
        clear = menu.addAction("清除行选择")
        clear.triggered.connect(self.external_table.clearSelection)
        clear.setEnabled(bool(rows))
        select_all.setEnabled(bool(self.external_table.rowCount()))
        count_menu = menu.addMenu("计入每日数量（操作选中行）")
        for label, mode in (("勾选计数", "check"), ("取消计数", "uncheck"),
                            ("反选计数", "invert")):
            action = count_menu.addAction(label)
            action.setEnabled(bool(rows))
            action.triggered.connect(
                lambda _checked=False, selected_mode=mode: self._set_external_inclusion(
                    rows, selected_mode
                )
            )
        return menu

    def _copy_external_values(self, rows, kind):
        lines = []
        for row in rows:
            item = self.external_table.item(row, 2)
            if item is None:
                continue
            name = str(item.data(QtCore.Qt.UserRole + 1) or "").strip()
            link = str(item.data(QtCore.Qt.UserRole) or "").strip()
            if kind == "name" and name:
                lines.append(name)
            elif kind == "link" and link:
                lines.append(link)
            elif kind == "both" and name and link:
                lines.append(f"{name}\t{link}")
        if lines:
            set_internal_clipboard_text("\n".join(lines))
            self.folder_status.setText(f"已复制 {len(lines)} 条{'文件名' if kind == 'name' else '网盘链接' if kind == 'link' else '文件名和链接'}。")
        else:
            self.folder_status.setText("所选视频没有可复制的文件名或网盘链接。")

    def _set_external_inclusion(self, rows, mode):
        changed = skipped = 0
        for row in rows:
            item = self.external_table.item(row, 0)
            slot_item = self.external_table.item(row, 4)
            if item is None:
                continue
            current = item.checkState() == QtCore.Qt.Checked
            target = mode == "check" or mode == "invert" and not current
            if target and (slot_item is None or slot_item.text().strip().zfill(2) not in {"01", "02", "03"}):
                skipped += 1
                continue
            if current != target:
                item.setCheckState(QtCore.Qt.Checked if target else QtCore.Qt.Unchecked)
                changed += 1
        message = f"已修改 {changed} 条视频的计数状态。"
        if skipped:
            message += f"另有 {skipped} 条缺少有效时段（01/02/03），未勾选。"
        self.folder_status.setText(message + " 点击“保存分类并刷新数量”后生效。")

    def _copy_current_list(self):
        if not self.external_table.rowCount():
            QtWidgets.QMessageBox.information(self, "复制清单", "当前日期没有已列出的视频。")
            return
        lines = ["计数\t文件夹\t视频\t交付日期\t时段\t统计分页\t统计类别\t状态\t网盘链接"]
        for row in range(self.external_table.rowCount()):
            include = self.external_table.item(row, 0)
            values = ["是" if include.checkState() == QtCore.Qt.Checked else "否"]
            values.extend(
                self._combo_value(row, col) if col in (5, 6)
                else self.external_table.item(row, col).text()
                for col in range(1, 8)
            )
            values.append(str(self.external_table.item(row, 2).data(QtCore.Qt.UserRole) or ""))
            lines.append("\t".join(values))
        set_internal_clipboard_text("\n".join(lines))

    def set_folder_busy(self, busy):
        self.scan_button.setEnabled(not busy)
        self.scan_date_button.setEnabled(not busy)
        self.view_date_button.setEnabled(not busy)
        self.save_external_button.setEnabled(not busy)
        self.apply_bulk_button.setEnabled(not busy)
        if busy:
            self.folder_status.setText("正在扫描 Google Drive 文件夹及子文件夹…")

    def set_busy(self, busy):
        self.refresh_button.setEnabled(not busy)
        self.save_external_button.setEnabled(not busy)
        if busy:
            self.status.setText("正在读取上传历史、本地任务表和 Google 表格…")

    def show_result(self, result):
        self.set_category_options(result.get("category_options", {}))
        warnings = result.get("warnings", [])
        updated = result.get("updated", [])
        self.status.setText(
            f"已归类视频 {result.get('counted', 0)} 个；更新数字格 {len(updated)} 个；"
            f"待处理提示 {len(warnings)} 条"
        )
        lines = [f"{item['range']} → {item['count']}" for item in updated]
        if warnings:
            lines += ["", "待处理："] + [f"• {text}" for text in warnings]
        self.details.setPlainText("\n".join(lines) or "数量已是最新，无需写入。")

    def show_error(self, error):
        self.status.setText("刷新失败，原统计数据未改动")
        self.details.setPlainText(error)
