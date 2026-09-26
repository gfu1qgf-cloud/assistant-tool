"""Nonblocking status and manual refresh for daily quantity statistics."""

from qt_compat import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.DailyQuantityStats import (
    reconcile_daily_quantity,
    scan_daily_drive_date,
    scan_external_video_folder,
)
from model.TaskResultOrganizer import get_upload_batch


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
            "扫描所选日期会核对网盘实有视频；已保存的分类不变，未分类的可在下方填写。"
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
        source_layout.addWidget(self.external_table, 1)
        bulk_row = QtWidgets.QHBoxLayout()
        self._category_options = {}
        self.bulk_sheet = QtWidgets.QComboBox()
        self.bulk_sheet.setEditable(True)
        self.bulk_sheet.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.bulk_sheet.lineEdit().setPlaceholderText("统计分页")
        self.bulk_category = QtWidgets.QComboBox()
        self.bulk_category.setEditable(True)
        self.bulk_category.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.bulk_category.lineEdit().setPlaceholderText("统计类别")
        self.bulk_sheet.currentTextChanged.connect(self._refresh_bulk_categories)
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
            include.setData(QtCore.Qt.UserRole, str(record.get("id") or ""))
            self.external_table.setItem(row, 0, include)
            for col, key in ((1, "folder_name"), (2, "file_name"), (3, "batch_date"),
                             (4, "batch_slot"), (5, "sheet"), (6, "category")):
                display = record.get("relative_path") if col == 2 else record.get(key)
                item = QtWidgets.QTableWidgetItem(str(display or record.get(key) or ""))
                if col in (1, 2):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if record.get("daily_scan_date") and col in (3, 4) and (
                    col == 3 or record.get("batch_slot") in {"01", "02", "03"}
                ):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if col == 2:
                    item.setToolTip(str(record.get("drive_link") or ""))
                    item.setData(QtCore.Qt.UserRole, str(record.get("drive_link") or ""))
                self.external_table.setItem(row, col, item)
            state = "待分类" if not record.get("sheet") or not record.get("category") else "已分类"
            if record.get("missing_from_folder"):
                state += " · 历史保留"
            if record.get("missing_from_daily"):
                state += " · 已移出日期目录，停计"
            if record.get("outside_daily_scan"):
                state += " · 不在日期目录，停计"
            if record.get("review_path"):
                state += " · 审核暂存"
            if record.get("daily_scan_date") and not record.get("batch_slot"):
                state += " · 时段待确认"
            if record.get("possible_duplicate"):
                state += " · 疑似重复"
            if record.get("possible_revision"):
                state += " · 疑似旧任务修订版"
            status_item = QtWidgets.QTableWidgetItem(state)
            status_item.setFlags(status_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self.external_table.setItem(row, 7, status_item)

    def external_edits(self):
        result = []
        for row in range(self.external_table.rowCount()):
            def value(col):
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

    def _apply_bulk_category(self):
        sheet = self.bulk_sheet.currentText().strip()
        category = self.bulk_category.currentText().strip()
        rows = sorted({index.row() for index in self.external_table.selectedIndexes()})
        if not rows or not sheet and not category:
            QtWidgets.QMessageBox.information(
                self, "批量分类", "请先选中视频，并填写统计分页或类别。"
            )
            return
        for row in rows:
            if sheet:
                self.external_table.item(row, 5).setText(sheet)
            if category:
                self.external_table.item(row, 6).setText(category)

    def set_category_options(self, options):
        self._category_options = dict(options or {})
        current = self.bulk_sheet.currentText().strip()
        self.bulk_sheet.blockSignals(True)
        self.bulk_sheet.clear()
        self.bulk_sheet.addItems(sorted(self._category_options))
        if current:
            self.bulk_sheet.setCurrentText(current)
        self.bulk_sheet.blockSignals(False)
        self._refresh_bulk_categories()

    def _refresh_bulk_categories(self, _text=None):
        current = self.bulk_category.currentText().strip()
        self.bulk_category.clear()
        self.bulk_category.addItems(
            self._category_options.get(self.bulk_sheet.currentText().strip(), [])
        )
        if current and current in self._category_options.get(
            self.bulk_sheet.currentText().strip(), []
        ):
            self.bulk_category.setCurrentText(current)

    def _open_external_video(self, row, column):
        if column != 2:
            return
        item = self.external_table.item(row, column)
        link = str(item.data(QtCore.Qt.UserRole) or "") if item else ""
        if link:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))

    def _copy_current_list(self):
        if not self.external_table.rowCount():
            QtWidgets.QMessageBox.information(self, "复制清单", "当前日期没有已列出的视频。")
            return
        lines = ["计数\t文件夹\t视频\t交付日期\t时段\t统计分页\t统计类别\t状态\t网盘链接"]
        for row in range(self.external_table.rowCount()):
            include = self.external_table.item(row, 0)
            values = ["是" if include.checkState() == QtCore.Qt.Checked else "否"]
            values.extend(self.external_table.item(row, col).text() for col in range(1, 8))
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
