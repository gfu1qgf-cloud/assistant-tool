from datetime import date

from qt_compat import QtCore, QtGui, QtWidgets
from qt_compat import QMessageBox

from model.ClipboardHelper import set_internal_clipboard_text
from model.DailyLinkHistory import (
    daily_link_counts,
    daily_task_sheet_failure_count,
    daily_task_sheet_failures,
    format_daily_links,
    format_daily_task_sheet_failures,
    format_person_daily_links,
    history_dates,
    normalize_daily_link_history,
)


class DailyLinksDialog(QtWidgets.QDialog):
    link_role = QtCore.Qt.UserRole

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = normalize_daily_link_history(history)
        self.setWindowTitle("每日链接与任务表记录（最近 7 天）")
        self.resize(900, 680)

        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            "上传完成后的人员文件夹链接会按日期和批次保存在这里。"
            "双击链接可以直接打开。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        date_row = QtWidgets.QHBoxLayout()
        date_row.addWidget(QtWidgets.QLabel("日期：", self))
        self.date_combo = QtWidgets.QComboBox(self)
        date_row.addWidget(self.date_combo, 1)
        layout.addLayout(date_row)

        self.link_tree = QtWidgets.QTreeWidget(self)
        self.link_tree.setHeaderLabels(["收件人", "上传批次", "Google Drive 链接"])
        self.link_tree.setRootIsDecorated(True)
        self.link_tree.setAlternatingRowColors(True)
        self.link_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.link_tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents
        )
        self.link_tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.link_tree, 1)

        self.failure_title_label = QtWidgets.QLabel("任务提交表待核对视频", self)
        failure_font = self.failure_title_label.font()
        failure_font.setBold(True)
        self.failure_title_label.setFont(failure_font)
        layout.addWidget(self.failure_title_label)

        self.failure_tree = QtWidgets.QTreeWidget(self)
        self.failure_tree.setHeaderLabels(["视频名称", "上传批次", "原因"])
        self.failure_tree.setAlternatingRowColors(True)
        self.failure_tree.setMaximumHeight(190)
        self.failure_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.failure_tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents
        )
        self.failure_tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.failure_tree)

        failure_buttons = QtWidgets.QHBoxLayout()
        self.copy_failure_btn = QtWidgets.QPushButton("复制选中名称", self)
        self.copy_all_failures_btn = QtWidgets.QPushButton("复制全部待核对信息", self)
        failure_buttons.addWidget(self.copy_failure_btn)
        failure_buttons.addWidget(self.copy_all_failures_btn)
        failure_buttons.addStretch(1)
        layout.addLayout(failure_buttons)

        self.status_label = QtWidgets.QLabel("", self)
        layout.addWidget(self.status_label)

        buttons = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QPushButton("打开选中链接", self)
        self.copy_link_btn = QtWidgets.QPushButton("复制选中链接", self)
        self.copy_person_btn = QtWidgets.QPushButton("复制选中人员", self)
        self.copy_day_btn = QtWidgets.QPushButton("复制当天全部", self)
        close_btn = QtWidgets.QPushButton("关闭", self)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.copy_link_btn)
        buttons.addWidget(self.copy_person_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_day_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        for day_key in history_dates(self.history):
            day = date.fromisoformat(day_key)
            people_count, link_count = daily_link_counts(self.history, day_key)
            failure_count = daily_task_sheet_failure_count(self.history, day_key)
            self.date_combo.addItem(
                f"{day:%Y-%m-%d}（{people_count} 人 / {link_count} 个链接 / "
                f"{failure_count} 个待核对）",
                day_key,
            )
        if self.date_combo.count() == 0:
            self.date_combo.addItem("最近 7 天暂无已保存链接", "")
            self.date_combo.setEnabled(False)

        self.date_combo.currentIndexChanged.connect(self.refresh_links)
        self.link_tree.itemDoubleClicked.connect(
            lambda _item, _column: self.open_selected_link()
        )
        self.open_btn.clicked.connect(self.open_selected_link)
        self.copy_link_btn.clicked.connect(self.copy_selected_link)
        self.copy_person_btn.clicked.connect(self.copy_selected_person)
        self.copy_day_btn.clicked.connect(self.copy_current_day)
        self.copy_failure_btn.clicked.connect(self.copy_selected_failure)
        self.copy_all_failures_btn.clicked.connect(self.copy_all_failures)
        close_btn.clicked.connect(self.accept)
        self.refresh_links()

    def current_day_key(self):
        return str(self.date_combo.currentData() or "")

    def refresh_links(self, _index=None):
        self.link_tree.clear()
        day_key = self.current_day_key()
        people = self.history.get(day_key, {}).get("people", {})
        for person, slots in sorted(people.items()):
            person_item = QtWidgets.QTreeWidgetItem(
                [person, "", f"{len(slots)} 个链接"]
            )
            self.link_tree.addTopLevelItem(person_item)
            for slot, entry in sorted(slots.items()):
                link = str(entry.get("link", "")).strip()
                child = QtWidgets.QTreeWidgetItem(["", slot, link])
                child.setData(0, self.link_role, link)
                person_item.addChild(child)
            person_item.setExpanded(True)
        failures = daily_task_sheet_failures(self.history, day_key)
        self.failure_tree.clear()
        for failure in failures:
            item = QtWidgets.QTreeWidgetItem(
                [failure["file_name"], failure["slot"], failure["reason"]]
            )
            item.setData(0, self.link_role, failure["file_name"])
            self.failure_tree.addTopLevelItem(item)
        self.failure_title_label.setText(
            f"任务提交表待核对视频（{len(failures)}）"
        )
        people_count, link_count = daily_link_counts(self.history, day_key)
        if day_key:
            self.status_label.setText(
                f"当天共 {people_count} 人、{link_count} 个批次链接；"
                f"{len(failures)} 个视频需要核对任务提交表。"
            )
        else:
            self.status_label.setText("上传成功后，链接会自动出现在这里。")

    def selected_link(self):
        item = self.link_tree.currentItem()
        if item is None:
            return ""
        return str(item.data(0, self.link_role) or "").strip()

    def selected_person_item(self):
        item = self.link_tree.currentItem()
        return None if item is None else (item.parent() or item)

    def open_selected_link(self):
        link = self.selected_link()
        if not link:
            QMessageBox.information(self, "查看每日链接", "请先选中一条具体链接。")
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(link)):
            QMessageBox.warning(self, "查看每日链接", f"无法打开链接：\n{link}")

    def copy_text(self, text, message):
        if text:
            set_internal_clipboard_text(text)
            self.status_label.setText(message)

    def copy_selected_link(self):
        link = self.selected_link()
        if not link:
            QMessageBox.information(self, "查看每日链接", "请先选中一条具体链接。")
            return
        self.copy_text(link, "已复制选中的链接。")

    def copy_selected_person(self):
        item = self.selected_person_item()
        if item is None:
            QMessageBox.information(self, "查看每日链接", "请先选中一位收件人。")
            return
        person = item.text(0).strip()
        slots = (
            self.history.get(self.current_day_key(), {})
            .get("people", {})
            .get(person, {})
        )
        self.copy_text(
            format_person_daily_links(person, slots),
            f"已复制 {person} 的当天链接。",
        )

    def copy_current_day(self):
        day_key = self.current_day_key()
        _people_count, link_count = daily_link_counts(self.history, day_key)
        if not day_key or not link_count:
            QMessageBox.information(self, "查看每日链接", "当天没有可以复制的链接。")
            return
        self.copy_text(format_daily_links(self.history, day_key), "已复制当天全部链接。")

    def copy_selected_failure(self):
        item = self.failure_tree.currentItem()
        if item is None:
            QMessageBox.information(self, "任务表待核对", "请先选中一个视频。")
            return
        self.copy_text(item.text(0).strip(), "已复制选中的视频名称。")

    def copy_all_failures(self):
        text = format_daily_task_sheet_failures(
            self.history,
            self.current_day_key(),
        )
        if not text:
            QMessageBox.information(self, "任务表待核对", "当天没有待核对视频。")
            return
        self.copy_text(text, "已复制当天全部待核对视频。")
