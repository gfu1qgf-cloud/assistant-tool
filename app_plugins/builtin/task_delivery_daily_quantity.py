"""Nonblocking status and manual refresh for daily quantity statistics."""

from datetime import datetime

from qt_compat import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.DailyQuantityStats import (
    effective_batch_slot,
    preview_external_day,
    reconcile_daily_quantity,
    scan_daily_drive_date,
    scan_external_video_folder,
)
from model.TaskResultOrganizer import get_upload_batch
from model.ReviewSubmissionHistory import read_review_history


class _NoWheelComboBox(QtWidgets.QComboBox):
    """Scrolling the inventory must not silently change a classification."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def wheelEvent(self, event):
        event.ignore()


class _NoWheelDateEdit(QtWidgets.QDateEdit):
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
            self.completed.emit(reconcile_daily_quantity(self.config, self.root, verify_content=True))
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
    edit_sheet_requested = QtCore.pyqtSignal()
    edit_categories_requested = QtCore.pyqtSignal()
    refresh_requested = QtCore.pyqtSignal()
    scan_requested = QtCore.pyqtSignal(str, str, str)
    scan_date_requested = QtCore.pyqtSignal(str)
    view_date_requested = QtCore.pyqtSignal(str)
    review_check_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("每日数量统计")
        self.resize(1120, 740)
        layout = QtWidgets.QVBoxLayout(self)
        self._daily_counts = []
        self._overall_count = 0
        self._has_summary = False
        self._inventory_preview = None
        self._preview_day = ""
        self._show_preview = False
        self._visible_records = []
        self._populating_records = False
        self._sync_progress = {}
        self._progress_refresh_failed = False

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("交付日期"))
        batch_date, batch_slot = get_upload_batch({})
        self.folder_day = _NoWheelDateEdit()
        self.folder_day.setDisplayFormat("yyyy-MM-dd")
        self.folder_day.setCalendarPopup(True)
        self.folder_day.setMaximumDate(QtCore.QDate.currentDate())
        self.folder_day.setToolTip("交付日期不能在未来；滚轮不会修改日期或年份。")
        self.folder_day.setDate(QtCore.QDate(batch_date.year, batch_date.month, batch_date.day))
        self.folder_day.dateChanged.connect(self._show_daily_summary)
        toolbar.addWidget(self.folder_day)
        self.view_date_button = QtWidgets.QPushButton("查看已存清单")
        self.view_date_button.clicked.connect(lambda: self.view_date_requested.emit(
            self.folder_day.date().toString("yyyy-MM-dd")
        ))
        toolbar.addWidget(self.view_date_button)
        self.scan_date_button = QtWidgets.QPushButton("扫描网盘日期目录")
        self.scan_date_button.clicked.connect(lambda: self.scan_date_requested.emit(
            self.folder_day.date().toString("yyyy-MM-dd")
        ))
        toolbar.addWidget(self.scan_date_button)
        toolbar.addStretch(1)
        self.refresh_button = QtWidgets.QPushButton("保存并刷新数量")
        self.refresh_button.setToolTip("保存清单里的修改，重新核对本地任务表并同步每日数量表格。")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        progress_card = QtWidgets.QFrame(self)
        progress_card.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        progress_layout = QtWidgets.QVBoxLayout(progress_card)
        progress_row = QtWidgets.QHBoxLayout()
        self.sync_progress_heading = QtWidgets.QLabel("统计记录至：尚无成功同步记录", progress_card)
        progress_font = self.sync_progress_heading.font()
        progress_font.setBold(True)
        progress_font.setPointSize(max(11, progress_font.pointSize() + 2))
        self.sync_progress_heading.setFont(progress_font)
        self.sync_progress_heading.setWordWrap(True)
        progress_row.addWidget(self.sync_progress_heading, 1)
        self.latest_date_button = QtWidgets.QPushButton("跳到最近统计日期", progress_card)
        self.latest_date_button.setEnabled(False)
        self.latest_date_button.clicked.connect(self._jump_to_latest_date)
        progress_row.addWidget(self.latest_date_button)
        progress_layout.addLayout(progress_row)
        self.sync_progress_note = QtWidgets.QLabel("以本地成功同步记录为准；扫描清单不代表已填写谷歌数量表。", progress_card)
        self.sync_progress_note.setWordWrap(True)
        progress_layout.addWidget(self.sync_progress_note)
        layout.addWidget(progress_card)

        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)

        overview = QtWidgets.QWidget()
        overview_layout = QtWidgets.QVBoxLayout(overview)
        overview_layout.setSpacing(12)
        self.tabs.addTab(overview, "数量概览")
        metric_row = QtWidgets.QHBoxLayout()
        self.metric_values = {}
        for title, key in (("当日合计", "total"), ("12 点", "01"),
                           ("18 点", "02"), ("24 点", "03")):
            card = QtWidgets.QFrame()
            card.setObjectName("quantityCard")
            card.setStyleSheet(
                "QFrame#quantityCard { background:#F3F7FC; border:1px solid #D8E4F2;"
                " border-radius:7px; }"
                "QLabel { color:#24364A; border:0; background:transparent; }"
            )
            card_layout = QtWidgets.QVBoxLayout(card)
            caption = QtWidgets.QLabel(title)
            value = QtWidgets.QLabel("—")
            font = value.font()
            font.setPointSize(20)
            font.setBold(True)
            value.setFont(font)
            card_layout.addWidget(caption)
            card_layout.addWidget(value)
            self.metric_values[key] = value
            metric_row.addWidget(card, 1)
        overview_layout.addLayout(metric_row)
        self.summary_heading = QtWidgets.QLabel("尚无统计结果；选择日期后查看已存清单，或保存并刷新数量。")
        heading_font = self.summary_heading.font()
        heading_font.setBold(True)
        self.summary_heading.setFont(heading_font)
        self.summary_heading.setWordWrap(True)
        overview_layout.addWidget(self.summary_heading)
        self.summary_table = QtWidgets.QTableWidget(0, 6)
        self.summary_table.setHorizontalHeaderLabels([
            "统计分页", "统计类别", "12点", "18点", "24点", "合计"
        ])
        self.summary_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.summary_table.setAlternatingRowColors(True)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.horizontalHeader().setStretchLastSection(True)
        self.summary_table.setColumnWidth(0, 145)
        self.summary_table.setColumnWidth(1, 170)
        overview_layout.addWidget(self.summary_table, 1)
        self.summary_note = QtWidgets.QLabel(
            "这里显示上次刷新时已归类、参与计数的视频；有待处理提示时，部分数字可能尚未写入表格。"
        )
        self.summary_note.setWordWrap(True)
        overview_layout.addWidget(self.summary_note)
        self.review_queue_toggle = QtWidgets.QToolButton()
        self.review_queue_toggle.setText("待审核未计数：0")
        self.review_queue_toggle.setToolTip("查看尚未通过审核、暂不计数的视频。")
        self.review_queue_toggle.clicked.connect(self._show_review_tab)
        overview_layout.addWidget(self.review_queue_toggle, 0, QtCore.Qt.AlignLeft)

        videos = QtWidgets.QWidget()
        source_layout = QtWidgets.QVBoxLayout(videos)
        self.video_tab_index = self.tabs.addTab(videos, "视频清单")
        source_note = QtWidgets.QLabel(
            "在清单中核对时段和分类；勾选表示允许计数，审核视频仍须通过审核。"
        )
        source_note.setToolTip(
            "扫描日期目录只读取清单，不下载。已保存的分类会保留；疑似旧任务修订版默认不计数。"
            "右键可批量调整勾选和时段。跨年复用月日目录时，请留意旧视频。"
        )
        source_layout.addWidget(source_note)
        source_row = QtWidgets.QHBoxLayout()
        self.folder_link = QtWidgets.QComboBox()
        self.folder_link.setEditable(True)
        self.folder_link.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.folder_link.lineEdit().setPlaceholderText("粘贴 Google Drive 文件夹链接，或选择已导入的文件夹")
        self.folder_slot = QtWidgets.QComboBox()
        for slot, label in (("01", "12点"), ("02", "18点"), ("03", "24点")):
            self.folder_slot.addItem(label, slot)
        self.folder_slot.setCurrentIndex(self.folder_slot.findData(batch_slot))
        self.scan_button = QtWidgets.QPushButton("读取指定文件夹")
        self.scan_button.setToolTip("只读取网盘文件清单，不下载视频。")
        self.scan_button.clicked.connect(self._request_scan)
        source_row.addWidget(self.folder_link, 1)
        source_row.addWidget(self.folder_slot)
        source_row.addWidget(self.scan_button)
        source_layout.addLayout(source_row)
        self.folder_status = QtWidgets.QLabel("尚未扫描文件夹")
        self.folder_status.setWordWrap(True)
        source_layout.addWidget(self.folder_status)

        reviews = QtWidgets.QWidget()
        review_layout = QtWidgets.QVBoxLayout(reviews)
        self.review_tab_index = self.tabs.addTab(reviews, "待审核")
        review_header = QtWidgets.QHBoxLayout()
        review_header.addWidget(QtWidgets.QLabel(
            "待审核、需修改和缺少审核记录的视频暂不计数；通过后自动补计到原交付日期。"
        ), 1)
        self.review_check_button = QtWidgets.QPushButton("立即检查审核状态")
        self.review_check_button.clicked.connect(self.review_check_requested.emit)
        review_header.addWidget(self.review_check_button)
        review_layout.addLayout(review_header)
        self.review_queue = QtWidgets.QTableWidget(0, 3)
        self.review_queue.setHorizontalHeaderLabels(["交付日期/时段", "视频", "审核状态"])
        self.review_queue.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.review_queue.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.review_queue.setAlternatingRowColors(True)
        self.review_queue.verticalHeader().setVisible(False)
        self.review_queue.horizontalHeader().setStretchLastSection(True)
        self.review_queue.setColumnWidth(0, 135)
        self.review_queue.setColumnWidth(1, 450)
        self.review_queue.setToolTip("双击视频可打开网盘链接；通过审核后会自动补计到原交付日期。")
        self.review_queue.cellDoubleClicked.connect(self._open_pending_review)
        review_layout.addWidget(self.review_queue, 1)

        settings = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings)
        self.settings_tab_index = self.tabs.addTab(settings, "设置与记录")
        sheet_row = QtWidgets.QHBoxLayout()
        sheet_row.addWidget(QtWidgets.QLabel("统计表链接"))
        self.sheet_url = QtWidgets.QLineEdit()
        self.sheet_url.setReadOnly(True)
        self.sheet_url.setPlaceholderText("尚未设置每日数量表格")
        sheet_row.addWidget(self.sheet_url, 1)
        self.edit_sheet_button = QtWidgets.QPushButton("更改…")
        self.edit_sheet_button.clicked.connect(self.edit_sheet_requested.emit)
        sheet_row.addWidget(self.edit_sheet_button)
        settings_layout.addLayout(sheet_row)
        self.category_status = QtWidgets.QLabel("分类来自固定的本地列表，可通过“管理类别”增删改。")
        settings_layout.addWidget(self.category_status)
        settings_note = QtWidgets.QLabel(
            "根据首次交付日期计数；刷新时重读本地任务表，只写本人分类的三个时段，不修改定额和合计。"
        )
        settings_note.setWordWrap(True)
        settings_layout.addWidget(settings_note)
        self.details_toggle = QtWidgets.QToolButton()
        self.details_toggle.setText("查看表格写入记录与待处理提示")
        self.details_toggle.setCheckable(True)
        settings_layout.addWidget(self.details_toggle, 0, QtCore.Qt.AlignLeft)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setVisible(False)
        self.details_toggle.toggled.connect(self.details.setVisible)
        self.details_toggle.setChecked(True)
        self.external_table = QtWidgets.QTableWidget(0, 8)
        self.external_table.setHorizontalHeaderLabels([
            "允许计数", "文件夹", "视频", "交付日期", "时段", "统计分页", "统计类别", "状态"
        ])
        self.external_table.setAlternatingRowColors(True)
        self.external_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.external_table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.external_table.horizontalHeader().setStretchLastSection(True)
        self.external_table.setColumnWidth(0, 75)
        self.external_table.setColumnWidth(2, 250)
        self.external_table.setColumnWidth(3, 105)
        self.external_table.setColumnWidth(4, 105)
        self.external_table.setColumnWidth(5, 125)
        self.external_table.setColumnWidth(6, 155)
        self.external_table.cellDoubleClicked.connect(self._open_external_video)
        self.external_table.itemChanged.connect(self._refresh_inventory_preview)
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
        self.edit_categories_button = QtWidgets.QPushButton("管理类别…")
        self.edit_categories_button.setToolTip("编辑固定的本地类别；刷新统计和更换表格链接不会覆盖。")
        self.edit_categories_button.clicked.connect(self.edit_categories_requested.emit)
        bulk_row.addWidget(self.edit_categories_button)
        source_layout.addLayout(bulk_row)
        self.save_external_button = QtWidgets.QPushButton("保存分类并刷新数量")
        self.save_external_button.clicked.connect(self.refresh_requested.emit)
        self.copy_list_button = QtWidgets.QPushButton("复制当前清单")
        self.copy_list_button.clicked.connect(self._copy_current_list)
        action_row = QtWidgets.QHBoxLayout()
        action_row.addWidget(self.copy_list_button)
        action_row.addWidget(self.save_external_button, 1)
        source_layout.addLayout(action_row)
        settings_layout.addWidget(self.details, 1)
        settings_layout.addStretch(1)

        self.status = QtWidgets.QLabel("尚未刷新")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _show_review_tab(self):
        self.tabs.setCurrentIndex(self.review_tab_index)

    def show_video_list(self):
        self.tabs.setCurrentIndex(self.video_tab_index)

    def set_sheet_url(self, url):
        url = str(url or "").strip()
        self.sheet_url.setText(url)

    def show_pending_reviews(self, records):
        labels = {"pending": "待审核", "needs_changes": "需修改",
                  "untracked": "未找到审核记录"}
        self.review_queue.setRowCount(0)
        for record in records:
            row = self.review_queue.rowCount()
            self.review_queue.insertRow(row)
            date_text = "{} / {}".format(record.get("batch_date") or "日期待确认",
                                          effective_batch_slot(record.get("batch_slot")))
            for col, value in enumerate((date_text, record.get("file_name") or "",
                                         labels.get(record.get("status"), "待审核"))):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(str(record.get("drive_link") or ""))
                color = {
                    "needs_changes": "#FFE3DF",
                    "pending": "#FFF1CF",
                    "untracked": "#EEF2F6",
                }.get(record.get("status"))
                if color:
                    item.setBackground(QtGui.QColor(color))
                    item.setForeground(QtGui.QColor("#202124"))
                if col == 1:
                    item.setData(QtCore.Qt.UserRole, str(record.get("drive_link") or ""))
                self.review_queue.setItem(row, col, item)
        self.review_queue_toggle.setText(f"待审核未计数：{len(records)}")
        self.review_queue_toggle.setToolTip("包括待审核、需修改以及尚未写入审核记录的视频。")
        self.tabs.setTabText(self.review_tab_index, f"待审核 · {len(records)}")
        self.review_queue_toggle.setStyleSheet(
            "background-color:#FFF1CF;color:#202124;font-weight:bold;" if records else ""
        )
        self._render_sync_progress()

    def _open_pending_review(self, row, _column):
        item = self.review_queue.item(row, 1)
        link = str(item.data(QtCore.Qt.UserRole) or "") if item else ""
        if link:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))

    def set_category_loading(self):
        self.category_status.setText("正在加载本地类别列表…")

    def show_category_options(self, options):
        self.set_category_options(options)
        count = sum(len(values) for values in options.values())
        self.category_status.setText(
            f"固定类别：{len(options)} 个统计分页、{count} 个类别（本地保存，不联网识别）"
            if count else "类别列表为空；点击“管理类别”添加，刷新不会恢复已删除的选项。"
        )

    def show_category_error(self, error):
        self.category_status.setText("分类读取失败：" + str(error))

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
        self._populating_records = True
        self.external_table.setRowCount(0)
        if day:
            records = [item for item in records if str(item.get("batch_date") or "") == day]
        self._visible_records = sorted(records, key=lambda item: (
            str(item.get("batch_date") or ""), str(item.get("file_name") or "")
        ))
        self.tabs.setTabText(self.video_tab_index, f"视频清单 · {len(self._visible_records)}")
        self._preview_day = day or self.folder_day.date().toString("yyyy-MM-dd")
        for record in self._visible_records:
            row = self.external_table.rowCount()
            self.external_table.insertRow(row)
            include = QtWidgets.QTableWidgetItem()
            include.setFlags((include.flags() | QtCore.Qt.ItemIsUserCheckable)
                             & ~QtCore.Qt.ItemIsEditable)
            include.setCheckState(QtCore.Qt.Checked if record.get("included", True)
                                  else QtCore.Qt.Unchecked)
            include.setToolTip("勾选表示允许计数；审核视频仍须通过审核才会实际计入。")
            include.setData(QtCore.Qt.UserRole, str(record.get("id") or ""))
            self.external_table.setItem(row, 0, include)
            for col, key in ((1, "folder_name"), (2, "file_name"), (3, "batch_date")):
                display = record.get("relative_path") if col == 2 else record.get(key)
                item = QtWidgets.QTableWidgetItem(str(display or record.get(key) or ""))
                if col in (1, 2):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if record.get("daily_scan_date") and col == 3:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if col == 2:
                    item.setToolTip(str(record.get("drive_link") or ""))
                    item.setData(QtCore.Qt.UserRole, str(record.get("drive_link") or ""))
                    item.setData(QtCore.Qt.UserRole + 1, str(record.get("file_name") or ""))
                self.external_table.setItem(row, col, item)
            slot_combo = _NoWheelComboBox(self.external_table)
            slot_combo.addItem("留空默认 03", "")
            for slot, label in (("01", "01 · 12点"), ("02", "02 · 18点"),
                                ("03", "03 · 24点")):
                slot_combo.addItem(label, slot)
            saved_slot = effective_batch_slot(record.get("batch_slot"))
            slot_combo.setCurrentIndex(max(slot_combo.findData(saved_slot), 0))
            detected_slot = record.get(
                "detected_batch_slot",
                "" if record.get("manual_batch_slot") else str(record.get("batch_slot") or ""),
            )
            locked = bool(record.get("daily_scan_date") and detected_slot in {"01", "02", "03"})
            slot_combo.setEnabled(not locked)
            slot_combo.setToolTip(
                "由网盘 01/02/03 目录确定，不能在此修改" if locked
                else "没有时段默认计入 03（24点）；可手动修改，疑似旧任务修订版除外"
            )
            self.external_table.setCellWidget(row, 4, slot_combo)
            sheet_combo = _NoWheelComboBox(self.external_table)
            category_combo = _NoWheelComboBox(self.external_table)
            for combo in (slot_combo, sheet_combo, category_combo):
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
            slot_combo.currentIndexChanged.connect(
                lambda _index, selected_row=row: self._row_slot_changed(selected_row)
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
                state += {
                    "passed": " · 已通过", "needs_changes": " · 需修改",
                    "pending": " · 待审核", "untracked": " · 未找到审核记录",
                }.get(record.get("review_status"), " · 待审核")
                if record.get("review_status") != "passed":
                    state += " · 暂不计数"
            if str(record.get("batch_slot") or "").strip() in {"", "00"}:
                state += " · 默认时段 03"
            elif effective_batch_slot(record.get("batch_slot")) not in {"01", "02", "03"}:
                state += " · 时段值无效"
            if record.get("possible_duplicate"):
                state += " · 疑似重复"
            if record.get("possible_revision"):
                state += " · 疑似旧任务修订版"
            status_item = QtWidgets.QTableWidgetItem(state)
            status_item.setFlags(status_item.flags() & ~QtCore.Qt.ItemIsEditable)
            status_item.setData(QtCore.Qt.UserRole, state.partition(" · ")[2])
            self.external_table.setItem(row, 7, status_item)
        self._populating_records = False
        self.mark_external_edits_saved()
        self._refresh_inventory_preview()

    @staticmethod
    def _set_combo_choices(combo, choices, selected, placeholder):
        selected = str(selected or "").strip()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, "")
        for choice in choices:
            choice = str(choice).strip()
            if choice and combo.findData(choice) < 0:
                combo.addItem(" ".join(choice.split()), choice)
                combo.setItemData(combo.count() - 1, choice, QtCore.Qt.ItemDataRole.ToolTipRole)
        index = combo.findData(selected)
        if selected and index < 0:
            combo.addItem(f"⚠ 未找到：{selected}", selected)
            index = combo.count() - 1
            combo.setItemData(index, "本地可选列表中未找到此项，原值仍保留；可到“管理类别”添加。\n" + selected,
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

    def _row_slot_changed(self, row):
        include = self.external_table.item(row, 0)
        record = self._visible_records[row]
        slot = effective_batch_slot(self._combo_value(row, 4))
        blocked = any(record.get(flag) for flag in (
            "possible_revision", "missing_from_daily", "outside_daily_scan"
        ))
        include.setCheckState(
            QtCore.Qt.Checked if slot in {"01", "02", "03"} and not blocked else QtCore.Qt.Unchecked
        )
        self._update_row_status(row)

    def _update_row_status(self, row):
        item = self.external_table.item(row, 7)
        if item is None:
            return
        state = "已分类" if self._combo_value(row, 5) and self._combo_value(row, 6) else "待分类"
        suffixes = [part for part in str(item.data(QtCore.Qt.UserRole) or "").split(" · ")
                    if part and part not in {"时段待确认", "默认时段 03"}]
        if not self._combo_value(row, 4):
            suffixes.append("默认时段 03")
        item.setText(state + (" · " + " · ".join(suffixes) if suffixes else ""))

    def refresh_review_statuses(self):
        """Update visible review states without discarding unsaved classifications."""
        history = read_review_history().get("items", {})
        labels = {"passed": "已通过", "needs_changes": "需修改", "pending": "待审核",
                  "untracked": "未找到审核记录"}
        changed = False
        for row, record in enumerate(self._visible_records):
            if not record.get("review_path"):
                continue
            key = "google:" + str(record.get("drive_file_id") or "")
            status = str(history.get(key, {}).get("status") or "untracked")
            if status == record.get("review_status"):
                continue
            changed = True
            record["review_status"] = status
            item = self.external_table.item(row, 7)
            if item is None:
                continue
            parts = [part for part in str(item.data(QtCore.Qt.UserRole) or "").split(" · ")
                     if part and part not in labels.values() and part != "暂不计数"]
            if status in labels:
                parts.append(labels[status])
            if status != "passed":
                parts.append("暂不计数")
            item.setData(QtCore.Qt.UserRole, " · ".join(parts))
            self._update_row_status(row)
        if changed:
            self._refresh_inventory_preview()

    def external_edits(self):
        result = []
        for row in range(self.external_table.rowCount()):
            def value(col):
                if col in (4, 5, 6):
                    return self._combo_value(row, col)
                item = self.external_table.item(row, col)
                return item.text().strip() if item else ""
            include = self.external_table.item(row, 0)
            result.append({
                "id": include.data(QtCore.Qt.UserRole),
                "included": include.checkState() == QtCore.Qt.Checked,
                "batch_date": value(3), "batch_slot": effective_batch_slot(value(4)),
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

    def _refresh_inventory_preview(self, _item=None):
        if self._populating_records:
            return
        records = [
            {**record, **edit}
            for record, edit in zip(self._visible_records, self.external_edits())
        ]
        self._inventory_preview = preview_external_day(records, self._preview_day)
        self._show_preview = True
        self._show_daily_summary()

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
        current_category = self.bulk_category.currentData()
        self._set_combo_choices(
            self.bulk_sheet, sorted(self._category_options), current, "选择统计分页"
        )
        self._refresh_bulk_categories()
        if current_category:
            self._set_combo_choices(self.bulk_category, self._category_options.get(current, []),
                                    current_category, "选择统计类别")

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
        slot_menu = menu.addMenu("设置交付时段并计数（选中行）")
        for slot, label in (("01", "01 · 12点"), ("02", "02 · 18点"),
                            ("03", "03 · 24点")):
            action = slot_menu.addAction(label)
            action.setEnabled(bool(rows))
            action.triggered.connect(
                lambda _checked=False, selected_slot=slot: self._set_external_slot(
                    rows, selected_slot
                )
            )
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

    def _set_external_slot(self, rows, slot):
        changed = locked = 0
        for row in rows:
            combo = self.external_table.cellWidget(row, 4)
            if combo is None or not combo.isEnabled():
                locked += 1
                continue
            if self._combo_value(row, 4) != slot:
                combo.setCurrentIndex(combo.findData(slot))
                changed += 1
        message = f"已为 {changed} 条视频设置时段 {slot}。"
        if locked:
            message += f"另有 {locked} 条时段由网盘目录确定，未修改。"
        self.folder_status.setText(message + " 点击“保存分类并刷新数量”后生效。")

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
            raw_slot = self._combo_value(row, 4)
            slot = effective_batch_slot(raw_slot)
            if item is None:
                continue
            current = item.checkState() == QtCore.Qt.Checked
            target = mode == "check" or mode == "invert" and not current
            if target and slot not in {"01", "02", "03"}:
                skipped += 1
                continue
            if target and raw_slot in {"", "00"}:
                combo = self.external_table.cellWidget(row, 4)
                combo.setCurrentIndex(combo.findData("03"))
            if current != target:
                item.setCheckState(QtCore.Qt.Checked if target else QtCore.Qt.Unchecked)
                changed += 1
        message = f"已修改 {changed} 条视频的计数状态。"
        if skipped:
            message += f"另有 {skipped} 条时段值无效（应为01/02/03），未勾选。"
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
                self._combo_value(row, col) if col in (4, 5, 6)
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
        # Online labels are used by the writer to locate cells, not as choices.
        # A partial response must never clear the user-owned local catalog.
        warnings = result.get("warnings", [])
        updated = result.get("updated", [])
        self._daily_counts = list(result.get("daily_counts", []))
        self._overall_count = int(result.get("counted", 0))
        self._has_summary = True
        self._show_preview = False
        if "sync_progress" in result:
            self.show_sync_progress(result["sync_progress"])
        self._show_daily_summary()
        self.status.setText(
            f"全部日期已归类视频 {self._overall_count} 个；本次更新数字格 {len(updated)} 个；"
            f"待处理提示 {len(warnings)} 条"
        )
        if warnings and not updated:
            self.status.setText("本次未写入，有待处理问题；不能据此判断线上数量已完整。 " + self.status.text())
        lines = [
            "以下是本次实际改写的谷歌表格单元格，不是今日视频总数。",
            "写入 0 表示该格原有的程序计数需要清零；今日数量请看上方统计概览。",
            "",
            "表格写入记录：",
        ]
        lines.extend(f"{item['range']} → {item['count']}" for item in updated)
        if not updated:
            lines.append("本次未改写数字格；仍有待处理问题，部分数量可能未写入。" if warnings
                         else "本次无需改写数字格，现有表格已是最新。")
        duplicates = result.get("duplicate_exclusions", [])
        if duplicates:
            lines += ["", f"重复副本已排除（{len(duplicates)} 个，不增加数量）："]
            lines.extend(f"• {item['file_name']}（{item['date']} / {item['category']}）："
                         f"保留 {item['retained_date']} / {item['retained_category']}；"
                         f"{item['reason']}" for item in duplicates)
        adjustments = result.get("automatic_classifications", [])
        if adjustments:
            lines += ["", "按时长自动调整统计类别（本地表格未改动）："]
            lines.extend(f"• {item['file_name']}（{item['duration_millis'] / 1000:.3f} 秒）："
                         f"{item['from']} → {item['to']}" for item in adjustments)
        if warnings:
            lines += ["", "待处理："] + [f"• {text}" for text in warnings]
            self.details_toggle.setChecked(True)
        self.tabs.setTabText(
            self.settings_tab_index, "设置与记录 · 待处理" if warnings else "设置与记录"
        )
        self.details.setPlainText("\n".join(lines))

    def _show_daily_summary(self, _date=None):
        self._render_sync_progress()
        day = self.folder_day.date().toString("yyyy-MM-dd")
        preview = self._inventory_preview if self._show_preview and self._preview_day == day else None
        if preview:
            rows = preview["daily_counts"]
            self.status.setText(
                f"{day} 已存清单预览：{preview['counted']} 个计数，"
                f"{preview['not_counted']} 个未计数；尚未核对谷歌表格"
            )
            suffix = (f"｜已存清单 {preview['total_files']} 条，未计入 {preview['not_counted']} 条"
                      f"（缺时段 {preview['missing_slot']}、缺分类 {preview['missing_category']}）"
                      "；这是本地预览，尚未核对谷歌表格")
            self.summary_note.setText(
                "本地清单预览会随勾选、时段和分类变化；点击“保存分类并刷新数量”后核对并同步谷歌表格。"
            )
        elif self._has_summary:
            rows = [item for item in self._daily_counts if item.get("date") == day]
            suffix = f"｜全部日期总合计 {self._overall_count} 个（上次刷新）"
            self.status.setText(
                f"上次刷新：全部日期已归类视频 {self._overall_count} 个；"
                "切换日期不会自动重读谷歌表格"
            )
            self.summary_note.setText(
                "这里显示上次刷新时已归类、参与计数的视频；有待处理提示时，部分数字可能尚未写入表格。"
            )
        else:
            self.status.setText("尚未刷新")
            self.summary_heading.setText(
                f"{day}：尚无统计结果；点击“查看已存清单”或“刷新并自动修正”。"
            )
            self.summary_table.setRowCount(0)
            for value in self.metric_values.values():
                value.setText("—")
            return
        slots = {slot: sum(int(item.get(slot, 0)) for item in rows)
                 for slot in ("01", "02", "03")}
        daily_total = sum(slots.values())
        for key, count in (("total", daily_total), ("01", slots["01"]),
                           ("02", slots["02"]), ("03", slots["03"])):
            self.metric_values[key].setText(str(count))
        self.summary_heading.setText(
            f"{day}：合计 {daily_total} 个  ·  12点 {slots['01']} / "
            f"18点 {slots['02']} / 24点 {slots['03']}"
            f"  {suffix}"
        )
        self.summary_table.setRowCount(len(rows) + 1)
        for row_number, item in enumerate(rows):
            values = [item.get("sheet", ""), item.get("category", ""),
                      item.get("01", 0), item.get("02", 0), item.get("03", 0),
                      item.get("total", 0)]
            for column, value in enumerate(values):
                self.summary_table.setItem(
                    row_number, column, QtWidgets.QTableWidgetItem(str(value))
                )
        total_values = ["合计", "", slots["01"], slots["02"], slots["03"], daily_total]
        for column, value in enumerate(total_values):
            cell = QtWidgets.QTableWidgetItem(str(value))
            font = cell.font()
            font.setBold(True)
            cell.setFont(font)
            self.summary_table.setItem(len(rows), column, cell)

    def show_error(self, error):
        self._progress_refresh_failed = True
        self._render_sync_progress()
        self.status.setText("刷新失败，原统计数据未改动")
        self.details.setPlainText(error)
        self.details_toggle.setChecked(True)
        self.tabs.setTabText(self.settings_tab_index, "设置与记录 · 错误")
        self.tabs.setCurrentIndex(self.settings_tab_index)

    def show_sync_progress(self, progress):
        self._sync_progress = dict(progress or {})
        self._progress_refresh_failed = False
        self._render_sync_progress()

    def _render_sync_progress(self):
        p = self._sync_progress
        latest = str(p.get("latest_date") or "")
        recorded = QtCore.QDate.fromString(latest, "yyyy-MM-dd")
        valid = recorded.isValid() and recorded <= self.folder_day.maximumDate()
        text = f"统计记录至：{latest}" if valid else "统计记录至：尚无成功同步记录"
        raw = str(p.get("updated_at") or "")
        if raw:
            try:
                text += "  ｜  上次刷新：" + datetime.fromisoformat(raw).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                text += "  ｜  上次刷新时间未知"
        self.sync_progress_heading.setText(text)
        self.latest_date_button.setEnabled(valid)
        dates = list(p.get("dates") or [])
        selected = self.folder_day.date().toString("yyyy-MM-dd")
        count = next((item.get("cells", 0) for item in dates if item.get("date") == selected), 0)
        selected_note = f"当前所选 {selected}：已有 {count} 条同步记录" if count else f"当前所选 {selected}：尚无同步记录"
        warning_count = p.get("warning_count")
        if isinstance(warning_count, int) and warning_count > 0:
            detail = f"最近刷新仍有 {warning_count} 条待处理，不能视为全部完成。"
        elif warning_count == 0:
            detail = "最近刷新无待处理提示；这里只证明已有记录，不保证未扫描的视频都已统计。"
        else:
            detail = "历史记录未保存待处理数量，请刷新确认完整性。"
        if self._progress_refresh_failed:
            detail = "本次刷新失败；以上保留的是此前成功同步记录。"
        queue = getattr(self, "review_queue", None)
        if queue is not None and queue.rowCount():
            detail += f" 另有 {queue.rowCount()} 条待审核未计数。"
        range_note = f"记录范围 {p.get('first_date', '')} 至 {latest}（{len(dates)} 个日期）；" if dates else ""
        self.sync_progress_note.setText(range_note + selected_note + "。\n" + detail)
        self.sync_progress_heading.setToolTip(
            "成功同步记录中的最晚交付日期，不代表之前每一天都完整；不把扫描日期当成同步日期。\n"
            + "\n".join(f"{item.get('date')}：{item.get('cells', 0)} 条同步记录" for item in reversed(dates[-31:]))
        )

    def _jump_to_latest_date(self):
        day = str(self._sync_progress.get("latest_date") or "")
        value = QtCore.QDate.fromString(day, "yyyy-MM-dd")
        if value.isValid() and value <= self.folder_day.maximumDate():
            self.folder_day.setDate(value)
            self.tabs.setCurrentIndex(0)
            self.view_date_requested.emit(day)
