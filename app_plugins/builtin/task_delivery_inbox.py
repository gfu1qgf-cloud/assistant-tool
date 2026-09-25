"""Read-only delivery checklist built from the app's existing local histories."""

from datetime import datetime

from qt_compat import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.DailyLinkHistory import daily_task_sheet_failures, history_dates
from model.ReviewSubmissionHistory import canonical_review_link, review_history_snapshot
from model.VideoUploadHistory import load_video_upload_history, normalize_video_identity


def _review_time(item):
    try:
        stamp = float(item.get("status_updated_at") or item.get("submitted_at") or 0)
        return datetime.fromtimestamp(stamp).isoformat(timespec="seconds") if stamp > 0 else ""
    except (OverflowError, OSError, TypeError, ValueError):
        return ""


def build_delivery_rows(upload_records, review_items, daily_history):
    """Join by Drive file ID; never guess that similar filenames are the same upload."""
    latest = {}
    for record in upload_records:
        if not isinstance(record, dict) or (record.get("replacement") or {}).get("state") == "replaced":
            continue
        key = str(record.get("logical_key") or normalize_video_identity(record.get("file_name"))).strip()
        if not key:
            continue
        if key not in latest or str(record.get("recorded_at") or "") >= str(latest[key].get("recorded_at") or ""):
            latest[key] = record

    rows = []
    by_link = {}
    by_batch_name = {}
    for record in latest.values():
        task = record.get("task") or {}
        sheet = record.get("task_submission") or {}
        link = str(record.get("drive_link") or "")
        row = {
            "time": str(record.get("recorded_at") or ""),
            "name": str(record.get("file_name") or ""),
            "admin": str(task.get("admin") or ""),
            "batch": "/".join(filter(None, (str(record.get("batch_date") or ""), str(record.get("batch_slot") or "")))),
            "link": link,
            "local_file": str(record.get("local_file") or ""),
            "sheet": str(sheet.get("status") or ""),
            "flags": set(),
            "notes": [],
        }
        if row["sheet"] in {"failed", "not_matched", "pending"}:
            row["flags"].add("任务表待核对")
            if sheet.get("reason"):
                row["notes"].append(str(sheet["reason"]))
        rows.append(row)
        if link:
            by_link[canonical_review_link(link)] = row
        by_batch_name[(str(record.get("batch_date") or ""), str(record.get("batch_slot") or ""), normalize_video_identity(row["name"]))] = row

    for review in review_items:
        if not isinstance(review, dict):
            continue
        link = str(review.get("link") or "")
        row = by_link.get(canonical_review_link(link)) if link else None
        if row is None:
            row = {
                "time": _review_time(review), "name": str(review.get("name") or ""),
                "admin": str(review.get("admin") or ""), "batch": "",
                "link": link, "local_file": "", "sheet": "", "flags": set(), "notes": [],
            }
            rows.append(row)
        if review.get("note"):
            row["notes"].append(str(review["note"]))
        status = str(review.get("status") or "")
        row["time"] = max(row["time"], _review_time(review))
        if status in {"passed", "needs_changes"} and review.get("acknowledged_status") != status:
            row["flags"].add("通过待发送" if status == "passed" else "审核需修改")
        elif status == "pending":
            row["flags"].add("审核中")

    for day in history_dates(daily_history):
        for failure in daily_task_sheet_failures(daily_history, day):
            key = (day, failure["slot"], normalize_video_identity(failure["file_name"]))
            row = by_batch_name.get(key)
            if row is None:
                row = {
                    "time": failure["saved_at"] or day, "name": failure["file_name"],
                    "admin": "", "batch": f"{day}/{failure['slot']}", "link": "",
                    "local_file": "", "sheet": "", "flags": set(), "notes": [],
                }
                rows.append(row)
            row["flags"].add("任务表待核对")
            if failure["reason"]:
                row["notes"].append(failure["reason"])

    rows.sort(key=lambda row: row["time"], reverse=True)
    return rows


class DeliveryInboxDialog(QtWidgets.QDialog):
    SHEET_TEXT = {
        "confirmed": "已确认", "failed": "写入失败", "not_matched": "未确认",
        "pending": "待确认", "not_attempted": "未尝试",
    }

    def __init__(self, config, daily_history, parent=None):
        super().__init__(parent)
        self.config = config
        self.daily_history = daily_history
        self.rows = []
        self.setWindowTitle("交付待办")
        self.resize(1040, 600)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("汇总本地记录，不实时查询网盘或表格；需修改审核状态请进入“审核提醒”。", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        filters = QtWidgets.QHBoxLayout()
        self.filter_box = QtWidgets.QComboBox(self)
        self.filter_box.addItems(("待处理", "全部", "任务表待核对", "通过待发送", "审核需修改", "审核中"))
        self.search = QtWidgets.QLineEdit(self)
        self.search.setPlaceholderText("搜索视频名或管理员")
        filters.addWidget(self.filter_box)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setHeaderLabels(("最近记录", "待办状态", "管理员", "视频", "批次", "任务表"))
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)
        buttons = QtWidgets.QHBoxLayout()
        copy_button = QtWidgets.QPushButton("复制选中链接", self)
        open_button = QtWidgets.QPushButton("打开选中视频", self)
        refresh_button = QtWidgets.QPushButton("刷新本地记录", self)
        close_button = QtWidgets.QPushButton("关闭", self)
        for button in (copy_button, open_button, refresh_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.filter_box.currentTextChanged.connect(self.render)
        self.search.textChanged.connect(self.render)
        copy_button.clicked.connect(self.copy_links)
        open_button.clicked.connect(self.open_selected)
        refresh_button.clicked.connect(self.reload)
        close_button.clicked.connect(self.accept)
        self.tree.itemDoubleClicked.connect(lambda _item, _column: self.open_selected())
        shortcut = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, self.tree)
        shortcut.activated.connect(self.copy_links)
        self.reload()

    def reload(self):
        self.rows = build_delivery_rows(
            load_video_upload_history(self.config).get("records", []),
            review_history_snapshot().get("all", []),
            self.daily_history,
        )
        self.render()

    def render(self, *_):
        self.tree.clear()
        selected_filter = self.filter_box.currentText()
        query = self.search.text().strip().casefold()
        for row in self.rows:
            flags = row["flags"]
            if selected_filter == "待处理" and not flags.intersection({"任务表待核对", "通过待发送", "审核需修改"}):
                continue
            if selected_filter not in {"全部", "待处理"} and selected_filter not in flags:
                continue
            if query and query not in (row["name"] + " " + row["admin"]).casefold():
                continue
            item = QtWidgets.QTreeWidgetItem(self.tree, (
                row["time"].replace("T", " ")[:19], "、".join(sorted(flags)) or "无待办",
                row["admin"], row["name"], row["batch"], self.SHEET_TEXT.get(row["sheet"], row["sheet"]),
            ))
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row)
            item.setToolTip(3, "\n".join(row["notes"]) or row["name"])
            if "审核需修改" in flags:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#b42318")))
            elif "任务表待核对" in flags:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#9a6700")))
            elif "通过待发送" in flags:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#16703a")))
        self.setWindowTitle(f"交付待办（显示 {self.tree.topLevelItemCount()} 条）")

    def _selected_rows(self):
        return [item.data(0, QtCore.Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]

    def copy_links(self):
        links = list(dict.fromkeys(row["link"] for row in self._selected_rows() if row["link"]))
        if links:
            set_internal_clipboard_text("\n".join(links))

    def open_selected(self):
        rows = self._selected_rows()
        if rows:
            row = rows[0]
            target = row["link"] or row["local_file"]
            if target:
                url = QtCore.QUrl.fromUserInput(target)
                QtGui.QDesktopServices.openUrl(url)
