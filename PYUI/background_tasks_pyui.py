"""A host-owned view. Closing any plugin window never owns/cancels these jobs."""

import time
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import ACTIVE, TERMINAL


STATUS_TEXT = {"queued": "等待", "running": "运行中", "canceling": "取消中",
               "succeeded": "完成", "failed": "失败", "canceled": "已取消", "interrupted": "上次未完成"}


class _TaskCard(QtWidgets.QFrame):
    def __init__(self, manager, task_id, parent):
        super().__init__(parent)
        self.manager = manager
        self.task_id = task_id
        self.view = parent
        self._deleting = False
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        title_row = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel(self)
        self.title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.state = QtWidgets.QLabel(self)
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.state)
        layout.addLayout(title_row)
        self.stage = QtWidgets.QLabel(self)
        self.stage.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.stage.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.stage)
        actions = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar(self)
        actions.addWidget(self.progress, 1)
        self.cancel_button = QtWidgets.QPushButton("取消", self)
        self.retry_button = QtWidgets.QPushButton("重试", self)
        self.result_button = QtWidgets.QPushButton("结果", self)
        self.detail_button = QtWidgets.QPushButton("详情", self)
        self.delete_button = QtWidgets.QPushButton("删除", self)
        self.delete_button.setToolTip("删除此任务卡片和历史记录，不删除成品或源文件；删除后不能在这里重试。")
        for button in (self.cancel_button, self.retry_button, self.result_button, self.detail_button, self.delete_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.cancel_button.clicked.connect(lambda: manager.cancel(task_id))
        self.retry_button.clicked.connect(self.retry_task)
        self.result_button.clicked.connect(self.open_result)
        self.detail_button.clicked.connect(self.show_details)
        self.delete_button.clicked.connect(self.delete_task)

    def update_record(self, row):
        full_title = f"{row['title']} · {row['owner']}"
        self.title.setToolTip(full_title)
        self.title.setText(self.title.fontMetrics().elidedText(full_title, QtCore.Qt.ElideRight, max(80, self.title.width())))
        self.state.setText(STATUS_TEXT.get(row["status"], row["status"]))
        self.state.setStyleSheet("color:#D93025;font-weight:bold;" if row["status"] in {"failed", "interrupted"} else "")
        stage = self.manager.waiting_reason(row["id"]) if row["status"] == "queued" else row["error"] or row["stage"]
        self.stage.setToolTip(stage)
        self.stage.setText(self.stage.fontMetrics().elidedText(stage, QtCore.Qt.ElideRight, max(80, self.width() - 25)))
        busy = row["status"] in {"running", "canceling"} and row["percent"] is None
        self.progress.setRange(0, 0 if busy else 100)
        if not busy:
            self.progress.setValue(int(row["percent"] or 0))
        self.cancel_button.setVisible(row["status"] in ACTIVE)
        self.cancel_button.setEnabled(row["status"] != "canceling")
        self.retry_button.setVisible(self.manager.can_retry(row["id"]))
        handle = self.manager._handles.get(row["id"])
        self.result_button.setVisible(bool(row.get("paths")) or bool(handle and handle.has_result_handler()))
        pending = bool(handle and handle.has_pending_result())
        self.result_button.setText("核对" if pending else "结果")
        if pending:
            self.state.setText("待核对")
        self.delete_button.setVisible(row["status"] in TERMINAL)
        self.delete_button.setEnabled(self.manager.can_remove(row["id"]))

    def delete_task(self):
        if self._deleting:
            return
        self._deleting = True
        self.delete_button.setEnabled(False)
        try:
            if self.manager.get(self.task_id).get('discarded'):
                self.view.schedule_refresh()
                return
            if not self.manager.remove(self.task_id):
                self.view.show_message("暂不能删除", "任务还在执行或收尾，请等待结束后再删除。")
        except OSError as error:
            self.view.show_message("删除失败", str(error), warning=True)
        finally:
            if not self.manager.get(self.task_id).get('discarded'):
                self._deleting = False
                self.delete_button.setEnabled(self.manager.can_remove(self.task_id))

    def open_result(self):
        handle = self.manager._handles.get(self.task_id)
        if handle is not None and handle.show_result():
            return
        paths = [Path(value) for value in self.manager.get(self.task_id).get("paths", [])]
        paths = [path for path in paths if path.exists()]
        if paths:
            target = paths[0] if len(paths) == 1 else paths[0].parent
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))
        else:
            self.view.show_message("结果已移动", "结果文件已被移动或删除，请到原任务目录核对。")

    def retry_task(self):
        try:
            self.manager.retry(self.task_id)
        except (RuntimeError, ValueError) as error:
            self.view.show_message("无法重试", str(error))

    def show_details(self):
        row = self.manager.get(self.task_id)
        text = (f"{row['title']}\n插件：{row['owner']}\n状态：{STATUS_TEXT.get(row['status'], row['status'])}\n"
                    f"提交：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['created_at']))}\n"
                    f"执行方式：{'独占' if row['execution'] == 'exclusive' else '并发（遵循资源锁）'}\n{row['error'] or row['stage']}")
        self.view.show_message("后台任务详情", text,
            details=row.get("traceback") or "\n".join(row.get("paths", [])) or "没有更多信息。")


class BackgroundTasksWidget(QtWidgets.QWidget):
    def __init__(self, manager, parent=None, *, shutdown_controller=None):
        super().__init__(parent)
        self.manager = manager
        self._cards = {}
        self._messages = {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        toolbar = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel(self)
        toolbar.addWidget(self.summary, 1)
        self.show_completed = QtWidgets.QCheckBox("显示近期完成", self)
        self.show_completed.setToolTip("本地保存最近七天、最多300条任务记录；失败任务不会自动隐藏。")
        toolbar.addWidget(self.show_completed)
        self.cleanup_button = QtWidgets.QToolButton(self)
        self.cleanup_button.setText("清理 ▾")
        self.cleanup_button.setToolTip("仅清理已结束的任务记录，不删除文件，也不会停止正在运行的任务。")
        self.cleanup_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        cleanup_menu = QtWidgets.QMenu(self.cleanup_button)
        self.clear_failed_action = cleanup_menu.addAction("清理失败 / 未完成记录")
        self.clear_finished_action = cleanup_menu.addAction("清理所有已结束记录")
        self.clear_failed_action.triggered.connect(lambda: self.clear_records({"failed", "interrupted"}))
        self.clear_finished_action.triggered.connect(lambda: self.clear_records(TERMINAL))
        self.cleanup_button.setMenu(cleanup_menu)
        toolbar.addWidget(self.cleanup_button)
        layout.addLayout(toolbar)
        self.limits_label=QtWidgets.QLabel(self)
        self.limits_label.setWordWrap(True)
        self.limits_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        self.limits_label.setToolTip('修改入口：程序设置 → 后台任务与并发。分类上限同时受总上限和资源锁约束。')
        layout.addWidget(self.limits_label)
        self.shutdown_controller = shutdown_controller
        self.shutdown_dialog = None
        if shutdown_controller is not None:
            self.setup_shutdown_controls(layout)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        body = QtWidgets.QWidget(scroll)
        self.card_layout = QtWidgets.QVBoxLayout(body)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.empty_label = QtWidgets.QLabel("暂无后台任务。插件提交后会在这里显示进度。", self)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.refresh)
        self.age_timer = QtCore.QTimer(self)
        self.age_timer.setInterval(1000)
        self.age_timer.timeout.connect(self._age_refresh)
        self.age_timer.start()
        manager.changed.connect(self.schedule_refresh)
        self.show_completed.toggled.connect(self.schedule_refresh)
        self.refresh()

    def setup_shutdown_controls(self, layout):
        row = QtWidgets.QHBoxLayout()
        self.shutdown_enabled = QtWidgets.QCheckBox('任务结束后关机', self)
        self.shutdown_enabled.setToolTip('仅本次启动有效。当前及新加入的后台任务结束后，60秒倒计时，可随时取消。不会等待达芬奇等其他软件，也不会自动替你审核。')
        self.shutdown_mode = QtWidgets.QComboBox(self)
        self.shutdown_mode.addItem('全部成功后', False)
        self.shutdown_mode.addItem('全部结束后（含失败／取消）', True)
        self.shutdown_mode.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.shutdown_mode.setMinimumContentsLength(12)
        row.addWidget(self.shutdown_enabled)
        row.addWidget(self.shutdown_mode, 1)
        self.cancel_shutdown_button = QtWidgets.QPushButton('取消关机', self)
        self.cancel_shutdown_button.clicked.connect(lambda: self.shutdown_controller.cancel())
        row.addWidget(self.cancel_shutdown_button)
        layout.addLayout(row)
        self.shutdown_status = QtWidgets.QLabel(self)
        self.shutdown_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.shutdown_status.setWordWrap(True)
        layout.addWidget(self.shutdown_status)
        self.shutdown_enabled.toggled.connect(self.toggle_shutdown)
        self.shutdown_controller.changed.connect(self.sync_shutdown_controls)
        self.sync_shutdown_controls()

    def toggle_shutdown(self, enabled):
        controller = self.shutdown_controller
        if not enabled:
            controller.cancel()
            return
        condition = '全部结束（包括失败／取消）' if self.shutdown_mode.currentData() else '全部成功'
        answer = QtWidgets.QMessageBox.question(self, '开启后台任务完成后关机',
            f'当前及随后加入的后台任务{condition}后，倒计时60秒关机。\n'
            '空队列不会立即关机。倒计时期间可随时取消，新任务会重新等待。\n\n'
            '仅等待本软件后台队列，不等待达芬奇／浏览器等外部程序，也不会代替人工审核。\n'
            '请先保存其他软件文档；程序会正常保存和退出，不强制关闭其他软件。\n'
            '此开关不保存到配置，重启后默认关闭。是否开启？',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            self.sync_shutdown_controls()
            return
        try:
            controller.arm(allow_failed=bool(self.shutdown_mode.currentData()))
        except (RuntimeError, ValueError, OSError) as error:
            controller.cancel('未开启自动关机：' + str(error))
            QtWidgets.QMessageBox.warning(self, '无法开启自动关机', str(error))

    def sync_shutdown_controls(self):
        controller = self.shutdown_controller
        blocker = QtCore.QSignalBlocker(self.shutdown_enabled)
        self.shutdown_enabled.setChecked(controller.armed)
        del blocker
        self.shutdown_enabled.setEnabled(not controller.finishing)
        self.shutdown_mode.setEnabled(not controller.armed and not controller.finishing)
        self.cancel_shutdown_button.setVisible(controller.armed)
        self.shutdown_status.setText(controller.status)
        if controller.deadline is None:
            if self.shutdown_dialog is not None:
                self.shutdown_dialog.hide()  # Not reject(): queue changes are not cancellation.
            return
        if self.shutdown_dialog is None:
            self.shutdown_dialog = QtWidgets.QDialog(self.window())
            self.shutdown_dialog.setWindowTitle('后台任务已结束 · 即将关机')
            self.shutdown_dialog.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
            self.shutdown_dialog.setModal(False)
            content = QtWidgets.QVBoxLayout(self.shutdown_dialog)
            self.shutdown_countdown = QtWidgets.QLabel(self.shutdown_dialog)
            self.shutdown_countdown.setWordWrap(True)
            self.shutdown_countdown.setMinimumWidth(350)
            content.addWidget(self.shutdown_countdown)
            cancel = QtWidgets.QPushButton('取消自动关机', self.shutdown_dialog)
            cancel.setDefault(True)
            cancel.clicked.connect(self.shutdown_dialog.reject)
            content.addWidget(cancel)
            self.shutdown_dialog.rejected.connect(lambda: controller.cancel())
        self.shutdown_countdown.setText(f'全部后台任务已结束。\n将在 {controller.remaining} 秒后保存、退出程序并关机。\n\n'
            '请保存其他软件中的文档。\n关闭此窗口或点击取消，都将取消本次自动关机。')
        if not self.shutdown_dialog.isVisible():
            QtWidgets.QApplication.beep()
            self.shutdown_dialog.show()
            self.shutdown_dialog.raise_()
            self.shutdown_dialog.activateWindow()

    def schedule_refresh(self, *_args):
        if self.isVisible() and not self.timer.isActive():
            self.timer.start()

    def _age_refresh(self):
        if self.isVisible():
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def show_message(self, title, text, *, warning=False, details=''):
        """Never run a nested dialog loop under a disposable task card.

        A refresh can delete a card while its static QMessageBox is executing.
        That path causes a native heap-corruption crash on our Windows Qt build.
        The stable view owns asynchronous dialogs and retains their wrappers.
        """
        key = (title, text, bool(warning), details)
        existing = self._messages.get(key)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return existing
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(title)
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning if warning else QtWidgets.QMessageBox.Icon.Information)
        box.setText(text)
        if details:
            box.setDetailedText(details)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        box.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._messages[key] = box
        box.finished.connect(lambda _result, current=box: self._forget_message(key, current))
        box.open()
        return box

    def _forget_message(self, key, box):
        if self._messages.get(key) is box:
            self._messages.pop(key, None)

    def clear_records(self, statuses):
        selected = [row["id"] for row in self.manager.records()
                    if row["status"] in statuses and self.manager.can_remove(row["id"])]
        if not selected:
            return
        answer = QtWidgets.QMessageBox.question(self, "清理后台任务记录",
            f"删除 {len(selected)} 条已结束的任务记录？\n不会删除任何成品或源文件，不影响正在运行的任务。\n删除后不能从这些记录重试或查看详情。",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.remove_many(selected)
            self.refresh()
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "清理失败", str(error))

    def refresh(self):
        rows = self.manager.records()
        remaining = {row["id"] for row in rows}
        for task_id in list(self._cards):
            if task_id not in remaining:
                card = self._cards.pop(task_id)
                self.card_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
        removable = [row for row in rows if self.manager.can_remove(row["id"])]
        self.clear_failed_action.setEnabled(any(row["status"] in {"failed", "interrupted"} for row in removable))
        self.clear_finished_action.setEnabled(bool(removable))
        running = sum(row["status"] in {"running", "canceling"} for row in rows)
        queued = sum(row["status"] == "queued" for row in rows)
        failed = sum(row["status"] in {"failed", "interrupted"} for row in rows)
        pending = sum(bool(self.manager._handles.get(row['id']) and
                           self.manager._handles[row['id']].has_pending_result()) for row in rows)
        self.summary.setText(f"运行 {running} · 等待 {queued} · 失败/未完成 {failed}"
                             + (f" · 待核对 {pending}" if pending else ""))
        self.limits_label.setText(f'并发上限：总{self.manager.max_parallel}／媒体{self.manager.kind_limits.get("media",1)}／联网{self.manager.kind_limits.get("network",1)}；资源锁仍有效')
        visible_count = 0
        for row in reversed(rows):
            task_id = row["id"]
            card = self._cards.get(task_id)
            if card is None:
                card = _TaskCard(self.manager, task_id, self)
                self._cards[task_id] = card
                self.card_layout.insertWidget(0, card)
            handle = self.manager._handles.get(task_id)
            visible = self.show_completed.isChecked() or row["status"] not in {"succeeded", "canceled"} \
                or bool(handle and handle.has_pending_result()) \
                or time.time() - float(row.get("finished_at") or 0) < 3
            card.setVisible(visible)
            if visible:
                visible_count += 1
                card.update_record(row)
        self.empty_label.setVisible(not visible_count)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_refresh()
