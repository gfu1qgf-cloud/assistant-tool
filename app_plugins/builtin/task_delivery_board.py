"""Four-quadrant delivery to-do board; only the user can complete a task."""

import json
import threading
from datetime import datetime
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_links, set_internal_clipboard_text
from model.DeliveryTodoMedia import candidate_folders, thumbnail_bytes, verify_sendable_folders
from model.DeliveryTodoStore import QUADRANTS
from model.GoogleDriveHelper import TOKEN_FILE, load_drive_service


MIME_TYPE = "application/x-lzx-delivery-todo"
PANEL_COLORS = ("#e9f1ff", "#e9f8f2", "#fff2e6", "#f1eff8")
PANEL_ACCENTS = ("#275caa", "#187451", "#a15b16", "#665489")


def _source_time(value):
    text = str(value or "")
    try:
        if text and text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text)).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        pass
    return text.replace("T", " ")[:16]


def _completed_time(value):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, TypeError, ValueError):
        return ""


class _QuadrantList(QtWidgets.QListWidget):
    dropped = QtCore.pyqtSignal(str, int)
    copy_requested = QtCore.pyqtSignal(str)

    def __init__(self, quadrant, parent=None):
        super().__init__(parent)
        self.quadrant = quadrant
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setSpacing(5)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, _supported_actions):
        item = self.currentItem()
        if item is None:
            return
        mime = QtCore.QMimeData()
        mime.setData(MIME_TYPE, str(item.data(QtCore.Qt.ItemDataRole.UserRole)).encode("utf-8"))
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        drag.exec(QtCore.Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasFormat(MIME_TYPE):
            task_id = bytes(event.mimeData().data(MIME_TYPE)).decode("utf-8")
            event.acceptProposedAction()
            # Do not clear list items while Qt is still dispatching this drop.
            QtCore.QTimer.singleShot(0, lambda: self.dropped.emit(task_id, self.quadrant))
        else:
            super().dropEvent(event)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if item and event.matches(QtGui.QKeySequence.StandardKey.Copy):
            self.copy_requested.emit(str(item.data(QtCore.Qt.ItemDataRole.UserRole)))
            event.accept()
            return
        super().keyPressEvent(event)


class _HistoryTree(QtWidgets.QTreeWidget):
    copy_requested = QtCore.pyqtSignal(str)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if item and event.matches(QtGui.QKeySequence.StandardKey.Copy):
            self.copy_requested.emit(str(item.data(0, QtCore.Qt.ItemDataRole.UserRole)))
            event.accept()
            return
        super().keyPressEvent(event)


class _DetailTable(QtWidgets.QTableWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.copy_links = None

    def keyPressEvent(self, event):
        if event.matches(QtGui.QKeySequence.StandardKey.Copy) and self.copy_links:
            self.copy_links()
            event.accept()
            return
        super().keyPressEvent(event)


class _DetailMediaSignals(QtCore.QObject):
    folders_ready = QtCore.pyqtSignal(object, str)
    thumbnail_ready = QtCore.pyqtSignal(int, bytes)
    finished = QtCore.pyqtSignal()


class _DetailMediaWorker(QtCore.QRunnable):

    def __init__(self, items, verify_folders=False):
        super().__init__()
        self.setAutoDelete(False)
        self.items = list(items)
        self.verify_folders = bool(verify_folders)
        self.signals = _DetailMediaSignals()
        self.cancelled = threading.Event()

    def run(self):
        try:
            missing = []
            for index, item in enumerate(self.items):
                if self.cancelled.is_set():
                    break
                data = thumbnail_bytes(item)
                if data:
                    self.signals.thumbnail_ready.emit(index, data)
                else:
                    missing.append((index, item))
            if self.cancelled.is_set():
                return
            service = None
            if TOKEN_FILE.is_file() and (missing or (self.verify_folders and candidate_folders(self.items))):
                try:
                    service = load_drive_service()
                except (Exception, SystemExit):
                    pass
            folders = candidate_folders(self.items) if self.verify_folders else {}
            if folders and service:
                try:
                    self.signals.folders_ready.emit(verify_sendable_folders(self.items, service), "")
                except Exception as error:
                    self.signals.folders_ready.emit({}, f"文件夹核验失败：{error}")
            elif folders:
                self.signals.folders_ready.emit({}, "未找到网盘授权，暂不能核验文件夹")
            for index, item in missing:
                if self.cancelled.is_set():
                    break
                data = thumbnail_bytes(item, service)
                self.signals.thumbnail_ready.emit(index, data)
        finally:
            self.signals.finished.emit()


class _DeliveryDetailsDialog(QtWidgets.QDialog):
    def __init__(self, owner, task_id, items):
        super().__init__(owner)
        self.owner = owner
        self.task_id = task_id
        self.items = items
        self.folder_info = None
        self.table = None

    @QtCore.pyqtSlot(object, str)
    def show_folders(self, links, error):
        if not self.isVisible():
            return
        from html import escape
        signature = tuple(sorted(str(item.get("id")) for item in self.items))
        self.owner._verified_folders[self.task_id] = (signature, links)
        if links:
            self.folder_info.setText("可直接发送的文件夹：" + " · ".join(
                f'<a href="{escape(url, quote=True)}">{escape(folder_id)}</a>'
                for folder_id, url in links.items()
            ) + "；复制可发送链接会优先使用这些文件夹。")
        else:
            self.folder_info.setText(error or "文件夹中还有其他内容，继续使用单个视频链接。")

    @QtCore.pyqtSlot(int, bytes)
    def show_thumbnail(self, index, data):
        if not self.isVisible():
            return
        preview = self.table.cellWidget(index, 0)
        if preview is None:
            return
        pixmap = QtGui.QPixmap()
        if data and pixmap.loadFromData(data):
            preview.setPixmap(pixmap.scaled(
                160, 96, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            preview.setText("无预览")


class DeliveryBoardDialog(QtWidgets.QDialog):
    def __init__(self, store, sync_sources=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.sync_sources = sync_sources
        self.rows = {}
        self._verified_folders = {}
        self._media_workers = set()
        self._rendering = False
        self.setObjectName("delivery_todo_board")
        self.setWindowTitle("交付待办")
        self.resize(1160, 780)
        self.setStyleSheet("""
            QDialog#delivery_todo_board { background:#f6f8fc; color:#1d2b3a; }
            QLabel { color:#26384b; }
            QFrame#todo_summary { background:#ffffff; border:1px solid #dde5ef;
                border-radius:12px; }
            QLineEdit, QSpinBox, QComboBox { background:#ffffff; color:#1d2b3a;
                border:1px solid #cbd6e3; border-radius:7px; padding:5px; }
            QPushButton { background:#ffffff; color:#23415f; border:1px solid #cbd6e3;
                border-radius:7px; padding:6px 12px; }
            QPushButton:hover { background:#e8f2ff; border-color:#76a8e0; }
            QTabWidget::pane { border:0; }
            QTabBar::tab { background:#e8edf4; color:#354b61; padding:8px 18px;
                margin-right:4px; border-top-left-radius:7px; border-top-right-radius:7px; }
            QTabBar::tab:selected { background:#ffffff; color:#17518d; }
            QListWidget { background:transparent; border:0; outline:0; }
            QListWidget::item { background:#ffffff; color:#203144;
                border:1px solid #dce4ed; border-radius:9px; margin:2px; padding:7px; }
            QListWidget::item:hover, QListWidget::item:selected {
                background:#f7fbff; border-color:#71a9dd; color:#17334d; }
            QListWidget::indicator { width:17px; height:17px; background:#ffffff;
                border:2px solid #8195a9; border-radius:5px; }
            QListWidget::indicator:checked { background:#2d7bc5;
                border-color:#2d7bc5; }
            QTreeWidget { background:#ffffff; color:#203144; border:1px solid #dce4ed;
                border-radius:9px; alternate-background-color:#f7f9fc; }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        summary = QtWidgets.QFrame(self)
        summary.setObjectName("todo_summary")
        summary_layout = QtWidgets.QHBoxLayout(summary)
        heading = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("交付待办 · 四象限")
        title_font = title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        heading.addWidget(title)
        self.summary_text = QtWidgets.QLabel()
        heading.addWidget(self.summary_text)
        summary_layout.addLayout(heading, 1)
        summary_layout.addWidget(QtWidgets.QLabel("完成后保留"))
        self.days = QtWidgets.QSpinBox()
        self.days.setRange(1, 30)
        self.days.setValue(self.store.retention_days())
        self.days.setSuffix(" 天")
        self.days.setToolTip("到期只移入归档，不会删除")
        self.days.valueChanged.connect(self._retention_changed)
        summary_layout.addWidget(self.days)
        layout.addWidget(summary)

        toolbar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索任务、管理员或审核意见…")
        self.search.textChanged.connect(self.render)
        toolbar.addWidget(self.search, 1)
        add_button = QtWidgets.QPushButton("＋ 添加待办")
        add_button.clicked.connect(self.add_manual)
        toolbar.addWidget(add_button)
        refresh_button = QtWidgets.QPushButton("刷新记录")
        refresh_button.clicked.connect(self.refresh_sources)
        toolbar.addWidget(refresh_button)
        layout.addLayout(toolbar)

        self.tabs = QtWidgets.QTabWidget()
        self.board = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(self.board)
        grid.setSpacing(12)
        self.lists = []
        self.counts = []
        subtitles = ("重要 · 紧急", "重要 · 不紧急", "不重要 · 紧急", "不重要 · 不紧急")
        for quadrant, name in enumerate(QUADRANTS):
            panel = QtWidgets.QFrame()
            panel.setStyleSheet(
                f"QFrame {{ background:{PANEL_COLORS[quadrant]}; border:0; border-radius:12px; }}"
            )
            panel_layout = QtWidgets.QVBoxLayout(panel)
            panel_layout.setContentsMargins(11, 10, 11, 10)
            header = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(f"{quadrant + 1:02d}  {name}")
            label.setStyleSheet(f"color:{PANEL_ACCENTS[quadrant]}; font-weight:700; font-size:14px")
            header.addWidget(label)
            count = QtWidgets.QLabel("0 项")
            count.setStyleSheet(f"color:{PANEL_ACCENTS[quadrant]}; font-weight:600")
            header.addStretch(1)
            header.addWidget(count)
            panel_layout.addLayout(header)
            description = QtWidgets.QLabel(subtitles[quadrant] + " · 可拖动卡片调整")
            description.setStyleSheet("color:#617184; font-size:11px")
            panel_layout.addWidget(description)
            tasks = _QuadrantList(quadrant)
            tasks.itemChanged.connect(self._card_changed)
            tasks.itemDoubleClicked.connect(lambda _item: self.open_selected())
            tasks.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            tasks.customContextMenuRequested.connect(
                lambda position, widget=tasks: self._card_menu(widget, position)
            )
            tasks.dropped.connect(self.move_task)
            tasks.copy_requested.connect(self._copy_id)
            tasks.itemSelectionChanged.connect(
                lambda widget=tasks: self._quadrant_selected(widget)
            )
            tasks.setWordWrap(True)
            panel_layout.addWidget(tasks, 1)
            self.lists.append(tasks)
            self.counts.append(count)
            grid.addWidget(panel, quadrant // 2, quadrant % 2)
        self.tabs.addTab(self.board, "进行中")
        self.completed_tree = self._make_history_tree()
        self.archive_tree = self._make_history_tree()
        self.tabs.addTab(self.completed_tree, "刚完成")
        self.tabs.addTab(self.archive_tree, "归档")
        layout.addWidget(self.tabs, 1)

        footer = QtWidgets.QHBoxLayout()
        hint = QtWidgets.QLabel("勾选才算完成；完成记录几天后归档。双击或右键查看该管理员当日的视频与链接。")
        hint.setWordWrap(True)
        footer.addWidget(hint, 1)
        copy_button = QtWidgets.QPushButton("复制链接")
        copy_button.clicked.connect(self.copy_selected_link)
        footer.addWidget(copy_button)
        open_button = QtWidgets.QPushButton("打开视频")
        open_button.clicked.connect(self.open_selected)
        footer.addWidget(open_button)
        restore_button = QtWidgets.QPushButton("恢复待办")
        restore_button.clicked.connect(self.restore_selected)
        footer.addWidget(restore_button)
        layout.addLayout(footer)
        self.archive_timer = QtCore.QTimer(self)
        self.archive_timer.setInterval(60 * 60 * 1000)
        self.archive_timer.timeout.connect(self._archive_tick)
        self.archive_timer.start()
        self.render()

    def _make_history_tree(self):
        tree = _HistoryTree()
        tree.setHeaderLabels(("完成时间", "待办", "管理员", "来源"))
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        tree.itemDoubleClicked.connect(lambda _item, _column: self.open_selected())
        tree.copy_requested.connect(self._copy_id)
        return tree

    def refresh_sources(self):
        if self.sync_sources is not None:
            self.sync_sources()
        self.render()

    def _retention_changed(self, days):
        self.store.set_retention_days(days)
        self.render()

    def _archive_tick(self):
        if self.store.archive_completed() and self.isVisible():
            self.render()

    def render(self, *_args):
        self.store.archive_completed()
        query = self.search.text().strip().casefold()
        self._rendering = True
        try:
            for widget in self.lists:
                widget.clear()
            self.rows = {}
            open_rows = self.store.list_tasks("open")
            for row in open_rows:
                self.rows[row["id"]] = row
                if query and query not in " ".join(
                    str(row.get(key) or "") for key in ("title", "detail", "admin", "items_json")
                ).casefold():
                    continue
                quadrant = int(row["quadrant"])
                if quadrant not in range(4):
                    quadrant = 1
                item = QtWidgets.QListWidgetItem()
                title = ("旧版本待核对 · " if row["stale"] else "") + row["title"]
                if len(title) > 32:
                    title = title[:31] + "…"
                detail = str(row["detail"] or "").replace("\n", " ")
                if len(detail) > 32:
                    detail = detail[:31] + "…"
                subtitle = " · ".join(filter(None, (
                    row["admin"], _source_time(row["source_time"]),
                )))[:42]
                item.setText("\n".join(filter(None, (title, subtitle, detail))))
                warning = "这条待办属于较早提交版本；网盘链接可能已指向新内容，请核对后再发送。" if row["stale"] else ""
                item.setToolTip("\n".join(filter(None, (warning, row["title"], row["detail"], row["link"]))))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, row["id"])
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable |
                              QtCore.Qt.ItemFlag.ItemIsDragEnabled)
                item.setCheckState(QtCore.Qt.CheckState.Unchecked)
                item.setSizeHint(QtCore.QSize(0, 79 if detail else 63))
                self.lists[quadrant].addItem(item)
            for quadrant, widget in enumerate(self.lists):
                self.counts[quadrant].setText(f"{widget.count()} 项")
            self._fill_history(self.completed_tree, "completed", query)
            self._fill_history(self.archive_tree, "archived", query)
            open_count = len(open_rows)
            done_count = len(self.store.list_tasks("completed"))
            archive_count = len(self.store.list_tasks("archived"))
            self.summary_text.setText(f"{open_count} 项待完成  ·  {done_count} 项刚完成  ·  {archive_count} 项已归档")
            self.tabs.setTabText(0, f"进行中 ({open_count})")
            self.tabs.setTabText(1, f"刚完成 ({done_count})")
            self.tabs.setTabText(2, f"归档 ({archive_count})")
        finally:
            self._rendering = False

    def _fill_history(self, tree, status, query):
        tree.clear()
        for row in self.store.list_tasks(status):
            self.rows[row["id"]] = row
            if query and query not in " ".join(
                str(row.get(key) or "") for key in ("title", "detail", "admin")
            ).casefold():
                continue
            item = QtWidgets.QTreeWidgetItem(tree, (
                _completed_time(row["completed_at"]), row["title"], row["admin"], row["kind"],
            ))
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row["id"])
            item.setToolTip(1, "\n".join(filter(None, (row["detail"], row["link"]))))

    def _card_changed(self, item):
        if self._rendering or item.checkState() != QtCore.Qt.CheckState.Checked:
            return
        task_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        # A deferred refresh avoids destroying an item inside Qt's change event.
        QtCore.QTimer.singleShot(0, lambda: self._complete_id(task_id))

    def _quadrant_selected(self, selected_widget):
        if self._rendering or not selected_widget.selectedItems():
            return
        for widget in self.lists:
            if widget is not selected_widget:
                widget.clearSelection()

    def move_task(self, task_id, quadrant):
        self.store.set_quadrant(task_id, quadrant)
        self.render()

    def _card_menu(self, widget, position):
        item = widget.itemAt(position)
        if item is None:
            return
        widget.setCurrentItem(item)
        task_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        menu = QtWidgets.QMenu(self)
        if self.rows.get(task_id, {}).get("group_key"):
            menu.addAction("查看视频明细与链接", lambda: self._show_details(task_id))
        menu.addAction("✓ 标记完成", lambda: self._complete_id(task_id))
        menu.addAction("打开视频 / 链接", lambda: self._open_id(task_id))
        menu.addAction("复制网盘链接", lambda: self._copy_id(task_id))
        move = menu.addMenu("移到象限")
        for quadrant, name in enumerate(QUADRANTS):
            move.addAction(name, lambda checked=False, q=quadrant: self.move_task(task_id, q))
        menu.exec(widget.viewport().mapToGlobal(position))

    def _complete_id(self, task_id):
        if self.store.complete(task_id):
            self.render()

    def _selected(self):
        widget = self.tabs.currentWidget()
        if widget is self.board:
            for tasks in self.lists:
                item = tasks.currentItem()
                if item is not None and item.isSelected():
                    return self.rows.get(item.data(QtCore.Qt.ItemDataRole.UserRole))
            return None
        item = widget.currentItem()
        return self.rows.get(item.data(0, QtCore.Qt.ItemDataRole.UserRole)) if item else None

    def copy_selected_link(self):
        row = self._selected()
        if row:
            self._copy_id(row["id"])

    def _copy_id(self, task_id):
        row = self.rows.get(task_id)
        if not row:
            return
        items = self._items(row)
        verified = self._verified_folders.get(task_id)
        signature = tuple(sorted(str(item.get("id")) for item in items))
        folder_links = verified[1] if row["kind"] == "send" and not row["stale"] and verified and verified[0] == signature else {}
        pairs = [("文件夹", url) for url in folder_links.values()]
        pairs.extend((item.get("name") or "视频", item.get("link")) for item in items
                     if item.get("link") and item.get("folder_id") not in folder_links)
        if pairs:
            set_internal_clipboard_links(list(dict.fromkeys(pairs)))
        elif row["link"]:
            set_internal_clipboard_links([(row["title"], row["link"])])

    @staticmethod
    def _items(row):
        try:
            return json.loads(row.get("items_json") or "[]")
        except (TypeError, ValueError):
            return []

    def _show_details(self, task_id):
        row = self.rows.get(task_id)
        if not row:
            return
        items = self._items(row)
        if not items:
            return
        dialog = _DeliveryDetailsDialog(self, task_id, items)
        dialog.setWindowTitle(row["title"])
        dialog.resize(1100, 600)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(f"{len(items)} 个视频。双击打开；Ctrl+C 复制所选视频的可点击链接。"))
        can_check_folders = row["kind"] == "send" and not row["stale"] and bool(candidate_folders(items))
        folder_info = QtWidgets.QLabel(
            "正在核验可发送的文件夹…" if can_check_folders else
            ("需修改、核对或已过期的视频不提供文件夹链接。" if row["kind"] != "send" or row["stale"] else
             "暂无可核验的文件夹，使用单个视频链接。")
        )
        folder_info.setWordWrap(True)
        folder_info.setOpenExternalLinks(True)
        folder_info.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextBrowserInteraction
        )
        dialog.folder_info = folder_info
        layout.addWidget(folder_info)
        table = _DetailTable(len(items), 4, dialog)
        dialog.table = table
        table.setHorizontalHeaderLabels(("预览", "视频", "备注 / 审核意见", "谷歌网盘链接"))
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(0, 170)
        for index, video in enumerate(items):
            table.setRowHeight(index, 102)
            preview = QtWidgets.QLabel("加载预览…" if video.get("local_file") or video.get("drive_file_id") else "无预览")
            preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            preview.setFixedSize(160, 96)
            table.setCellWidget(index, 0, preview)
            for column, value in enumerate((video.get("name", ""), video.get("note", ""), video.get("link", "")), 1):
                table.setItem(index, column, QtWidgets.QTableWidgetItem(str(value)))
        table.itemDoubleClicked.connect(lambda item: self._open_video_item(items[item.row()]))
        table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        def show_row_menu(position):
            item = table.itemAt(position)
            if item is None:
                return
            video = items[item.row()]
            menu = QtWidgets.QMenu(dialog)
            menu.addAction("复制文件名", lambda: set_internal_clipboard_text(video.get("name", "")))
            menu.addAction("复制网盘链接", lambda: set_internal_clipboard_links(
                [(video.get("name", "视频"), video.get("link", ""))]
            ))
            menu.addAction("打开视频", lambda: self._open_video_item(video))
            menu.exec(table.viewport().mapToGlobal(position))
        table.customContextMenuRequested.connect(show_row_menu)
        def copy_selected_rows():
            selected = sorted({index.row() for index in table.selectionModel().selectedRows()})
            if not selected and table.currentRow() >= 0:
                selected = [table.currentRow()]
            set_internal_clipboard_links([
                (items[index].get("name") or "视频", items[index].get("link"))
                for index in selected if items[index].get("link")
            ])
        table.copy_links = copy_selected_rows
        layout.addWidget(table, 1)
        buttons = QtWidgets.QHBoxLayout()
        copy_one = QtWidgets.QPushButton("复制选中链接")
        copy_one.clicked.connect(copy_selected_rows)
        buttons.addWidget(copy_one)
        copy_all = QtWidgets.QPushButton("复制可发送链接")
        copy_all.clicked.connect(lambda: self._copy_id(task_id))
        buttons.addWidget(copy_all)
        buttons.addStretch(1)
        close = QtWidgets.QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        worker = _DetailMediaWorker(items, verify_folders=can_check_folders)
        self._media_workers.add(worker)
        worker.signals.folders_ready.connect(dialog.show_folders)
        worker.signals.thumbnail_ready.connect(dialog.show_thumbnail)
        worker.signals.finished.connect(self._media_finished)
        dialog.finished.connect(lambda _code: worker.cancelled.set())
        QtCore.QThreadPool.globalInstance().start(worker)
        dialog.exec()
        worker.cancelled.set()

    @QtCore.pyqtSlot()
    def _media_finished(self):
        signal_source = self.sender()
        self._media_workers = {
            worker for worker in self._media_workers if worker.signals is not signal_source
        }

    @staticmethod
    def _open_video_item(item):
        target = item.get("link") or item.get("local_file")
        if target:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromUserInput(target))

    def open_selected(self):
        row = self._selected()
        if row:
            if row.get("group_key"):
                self._show_details(row["id"])
                return
            target = row["link"] or row["local_file"]
            if target:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromUserInput(target))

    def _open_id(self, task_id):
        row = self.rows.get(task_id)
        if row:
            if row.get("group_key"):
                self._show_details(task_id)
                return
            target = row["link"] or row["local_file"]
            if target:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromUserInput(target))

    def restore_selected(self):
        if self.tabs.currentWidget() is self.board:
            return
        row = self._selected()
        if row and self.store.restore(row["id"]):
            self.render()

    def add_manual(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("添加待办")
        form = QtWidgets.QFormLayout(dialog)
        title = QtWidgets.QLineEdit()
        title.setPlaceholderText("写下下一步要做的事")
        detail = QtWidgets.QLineEdit()
        detail.setPlaceholderText("备注，可留空")
        quadrant = QtWidgets.QComboBox()
        quadrant.addItems(QUADRANTS)
        quadrant.setCurrentIndex(1)
        form.addRow("待办", title)
        form.addRow("备注", detail)
        form.addRow("象限", quadrant)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                self.store.add_manual(title.text(), detail.text(), quadrant.currentIndex())
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "添加待办", str(exc))
            self.render()
