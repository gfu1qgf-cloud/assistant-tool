"""PyQt6 dialog version of the standalone Codex account switcher UI."""

from __future__ import annotations

from datetime import datetime

from qt_compat import QtCore, QtGui, QtWidgets

from .launcher import (
    LaunchError,
    close_chatgpt,
    is_chatgpt_running,
    launch_chatgpt_windows,
    open_path,
)
from .quota import QuotaInfo, QuotaWorker
from .store import AccountProfile, AccountProfileStore, AccountSwitcherError


class NameDialog(QtWidgets.QDialog):
    def __init__(self, title, prompt, value="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.edit = QtWidgets.QLineEdit(value, self)
        self.edit.setPlaceholderText("例如：个人账号 / 工作账号")
        self.edit.setMaxLength(40)
        form.addRow(prompt, self.edit)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=self,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("确定")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def value(self):
        return self.edit.text().strip()

    def _accept(self):
        if not self.value:
            QtWidgets.QMessageBox.warning(self, "缺少名称", "请填写账号名称。")
            return
        self.accept()


class AccountSwitcherDialog(QtWidgets.QDialog):
    """Full account-management UI hosted by the auxiliary-tools plugin menu."""

    def __init__(self, store: AccountProfileStore, parent=None):
        super().__init__(parent)
        self.store = store
        self._quotas = {}
        self._quota_worker = None
        self._lingering_workers = []
        self.setWindowTitle("Codex 多账号切换")
        self.resize(1020, 680)
        self.setMinimumSize(860, 600)
        self._build_ui()
        self.refresh_table()
        if self.store.migration_performed:
            QtCore.QTimer.singleShot(0, self._show_migration_notice)
        elif not self.store.profiles():
            QtCore.QTimer.singleShot(0, self._show_first_run)
        else:
            QtCore.QTimer.singleShot(400, self.check_all_quotas)

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Codex 多账号切换", self)
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(title)
        subtitle = QtWidgets.QLabel(
            "每个账号只登录一次；以后仅替换 Codex 登录缓存，本地对话、项目和历史在各账号间共用。",
            self,
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        warning = QtWidgets.QLabel(
            "切换前会请求 Codex 正常退出，绝不强制结束进程。请先等当前任务结束并保存输入框草稿。",
            self,
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #8a5a00;")
        layout.addWidget(warning)

        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(QtWidgets.QLabel("桌面账号档案", self))
        heading.addStretch(1)
        self.adopt_button = QtWidgets.QPushButton("保存当前账号为第一个档案", self)
        self.add_button = QtWidgets.QPushButton("＋ 添加账号", self)
        self.refresh_button = QtWidgets.QPushButton("刷新状态", self)
        self.quota_button = QtWidgets.QPushButton("检测额度", self)
        self.adopt_button.clicked.connect(self.adopt_current)
        self.add_button.clicked.connect(self.add_profile)
        self.refresh_button.clicked.connect(self.refresh_all)
        self.quota_button.clicked.connect(lambda: self.check_all_quotas(force=True))
        for button in (self.adopt_button, self.add_button, self.refresh_button, self.quota_button):
            heading.addWidget(button)
        layout.addLayout(heading)

        self.table = QtWidgets.QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ["账号名称", "剩余额度", "登录缓存", "最近使用", "状态"]
        )
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.switch_selected())
        layout.addWidget(self.table, 1)

        self.active_label = QtWidgets.QLabel("当前：尚未建立桌面账号档案", self)
        self.active_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.active_label)

        primary = QtWidgets.QHBoxLayout()
        self.switch_button = QtWidgets.QPushButton("切换并打开 Codex 桌面版", self)
        self.switch_button.setStyleSheet("font-weight: 600;")
        self.next_button = QtWidgets.QPushButton("下一账号并打开", self)
        self.login_button = QtWidgets.QPushButton("首次登录此账号", self)
        self.switch_button.clicked.connect(self.switch_selected)
        self.next_button.clicked.connect(self.switch_next)
        self.login_button.clicked.connect(self.bind_selected)
        primary.addWidget(self.switch_button, 2)
        primary.addWidget(self.next_button)
        primary.addWidget(self.login_button)
        layout.addLayout(primary)

        management = QtWidgets.QHBoxLayout()
        self.open_button = QtWidgets.QPushButton("打开账号档案仓库", self)
        self.rename_button = QtWidgets.QPushButton("重命名", self)
        self.remove_button = QtWidgets.QPushButton("移除未使用账号", self)
        self.open_button.clicked.connect(self.open_store)
        self.rename_button.clicked.connect(self.rename_selected)
        self.remove_button.clicked.connect(self.remove_selected)
        management.addWidget(self.open_button)
        management.addWidget(self.rename_button)
        management.addStretch(1)
        management.addWidget(self.remove_button)
        layout.addLayout(management)

        self.status_label = QtWidgets.QLabel("就绪", self)
        self.status_label.setStyleSheet("color: #4b5563;")
        layout.addWidget(self.status_label)
        self.update_buttons()

    def _show_first_run(self):
        QtWidgets.QMessageBox.information(
            self,
            "先保存当前账号",
            "第一次使用，请先点击“保存当前账号为第一个档案”。\n\n"
            "这个操作不会移动当前 .codex；会备份登录缓存，并把 Codex "
            "凭据存储方式设为本地文件。原 config.toml 会先保留备份。",
        )

    def _show_migration_notice(self):
        QtWidgets.QMessageBox.information(
            self,
            "已升级为共享本地对话模式",
            "旧版账号档案已无损升级：现在仅替换 auth.json，当前 .codex、"
            "本地对话、历史数据库和项目记录都会固定留在原位。\n\n"
            "旧版完整账号目录仍作为安全备份保留。",
        )

    def _selected_profile(self):
        selection = self.table.selectionModel()
        rows = selection.selectedRows() if selection else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return self.store.get_profile(str(item.data(QtCore.Qt.UserRole))) if item else None

    def _select(self, profile_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(QtCore.Qt.UserRole) == profile_id:
                self.table.selectRow(row)
                return

    @staticmethod
    def _display_time(value):
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return str(value)

    @staticmethod
    def _quota_item(quota):
        item = QtWidgets.QTableWidgetItem(quota.status_text)
        item.setToolTip(quota.detail_text)
        if not quota.success:
            color = "#6b7280" if quota.status_text == "未绑定" else "#dc2626"
            if quota.status_text in ("查询超时", "查询频繁"):
                color = "#d97706"
        else:
            remaining = [value for value in (quota.primary_remaining_percent, quota.secondary_remaining_percent) if value is not None]
            minimum = min(remaining) if remaining else None
            color = "#047857"
            if quota.limit_reached or minimum == 0:
                color = "#dc2626"
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            elif minimum is not None and minimum <= 20:
                color = "#d97706"
        item.setForeground(QtGui.QColor(color))
        return item

    def refresh_table(self, select_id=None):
        selected = self._selected_profile()
        selected_id = select_id or (selected.profile_id if selected else None)
        profiles = self.store.profiles()
        self.table.setRowCount(len(profiles))
        for row, profile in enumerate(profiles):
            active = profile.profile_id == self.store.active_profile_id
            name = QtWidgets.QTableWidgetItem(("● " if active else "") + profile.name)
            name.setData(QtCore.Qt.UserRole, profile.profile_id)
            if active:
                name.setForeground(QtGui.QColor("#047857"))
                font = name.font()
                font.setBold(True)
                name.setFont(font)
            quota = self._quotas.get(profile.profile_id)
            if quota:
                quota_item = self._quota_item(quota)
            elif self._quota_worker and self._quota_worker.isRunning():
                quota_item = QtWidgets.QTableWidgetItem("查询中...")
            else:
                quota_item = QtWidgets.QTableWidgetItem("待检测" if self.store.has_file_auth(profile) else "未绑定")
            ready = self.store.has_file_auth(profile)
            cache = QtWidgets.QTableWidgetItem("已保存" if ready else "待首次登录")
            cache.setForeground(QtGui.QColor("#047857" if ready else "#b45309"))
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, quota_item)
            self.table.setItem(row, 2, cache)
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(self._display_time(profile.last_used_at)))
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem("当前使用" if active else "凭据已保存"))
        if selected_id:
            self._select(selected_id)
        elif profiles:
            self.table.selectRow(0)
        active = self.store.active_profile
        self.active_label.setText(f"当前桌面账号档案：{active.name}" if active else "当前：尚未建立桌面账号档案")
        self.adopt_button.setVisible(not profiles)
        self.add_button.setEnabled(bool(active))
        self.update_buttons()

    def update_buttons(self):
        profile = self._selected_profile()
        self.switch_button.setEnabled(profile is not None)
        self.login_button.setEnabled(bool(profile and not self.store.has_file_auth(profile)))
        self.rename_button.setEnabled(profile is not None)
        self.remove_button.setEnabled(bool(profile and profile.profile_id != self.store.active_profile_id))
        ready_count = sum(self.store.has_file_auth(item) for item in self.store.profiles())
        self.next_button.setEnabled(ready_count > 1)

    def _stop_quota_worker(self):
        worker = self._quota_worker
        if not worker:
            return
        self._quota_worker = None
        try:
            worker.quota_ready.disconnect()
            worker.all_finished.disconnect()
        except TypeError:
            pass
        worker.cancel()
        if worker.wait(200):
            worker.deleteLater()
        else:
            self._lingering_workers.append(worker)

    def _cleanup_worker(self, worker):
        if self._quota_worker is worker:
            self._quota_worker = None
        if worker in self._lingering_workers:
            self._lingering_workers.remove(worker)
        worker.deleteLater()

    def check_all_quotas(self, force=False):
        profiles = self.store.profiles()
        if not profiles:
            return
        if self._quota_worker and self._quota_worker.isRunning():
            if not force:
                return
            self._stop_quota_worker()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            profile = self.store.get_profile(str(item.data(QtCore.Qt.UserRole))) if item else None
            if profile and self.store.has_file_auth(profile) and (force or profile.profile_id not in self._quotas):
                self.table.setItem(row, 1, QtWidgets.QTableWidgetItem("查询中..."))
        self.status_label.setText("正在检测各账号剩余额度……")
        worker = QuotaWorker(
            [(profile.profile_id, self.store.effective_auth_path(profile)) for profile in profiles],
            on_token_refreshed=self.store.update_profile_tokens,
            parent=self,
        )
        self._quota_worker = worker
        worker.quota_ready.connect(self._on_quota_ready)
        worker.all_finished.connect(self._on_quota_finished)
        worker.finished.connect(
            lambda value=worker: self._cleanup_worker(value)
        )
        worker.start()

    def _on_quota_ready(self, profile_id, quota):
        if self.sender() is not self._quota_worker or self._quota_worker.is_cancelled:
            return
        if not self.store.get_profile(profile_id):
            return
        self._quotas[profile_id] = quota
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(QtCore.Qt.UserRole) == profile_id:
                self.table.setItem(row, 1, self._quota_item(quota))
                return

    def _on_quota_finished(self):
        if self.sender() is self._quota_worker and not self._quota_worker.is_cancelled:
            self.status_label.setText("各账号额度检测完成。")

    def refresh_all(self):
        self.refresh_table()
        self.check_all_quotas(force=True)

    def _ensure_app_closed(self, reason="切换登录缓存前必须完全退出 Codex。"):
        if not is_chatgpt_running():
            return True
        answer = QtWidgets.QMessageBox.question(
            self,
            "需要关闭 Codex",
            reason + "\n\n请确认当前任务已结束、输入框草稿已保存。现在请求正常关闭吗？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return False
        if close_chatgpt():
            return True
        QtWidgets.QMessageBox.warning(
            self,
            "请手动退出",
            "Codex 没有在 10 秒内完全退出。插件没有强制结束它，也没有修改登录缓存。\n\n"
            "请从任务栏托盘退出 Codex，再重试。",
        )
        return False

    def adopt_current(self):
        dialog = NameDialog("保存当前账号", "账号名称", "当前账号", self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        was_running = is_chatgpt_running()
        if not self._ensure_app_closed("建立第一个账号档案前需要让 Codex 完全退出。"):
            return
        self._stop_quota_worker()
        try:
            profile = self.store.adopt_current(dialog.value)
        except (AccountSwitcherError, ValueError, OSError) as error:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(error))
            return
        self.refresh_table(profile.profile_id)
        self.check_all_quotas(force=True)
        if was_running:
            try:
                launch_chatgpt_windows()
            except (LaunchError, OSError) as error:
                QtWidgets.QMessageBox.warning(
                    self,
                    "账号已保存，但 Codex 未启动",
                    f"账号档案已经安全保存。请手动打开 Codex。\n\n{error}",
                )
        if not self.store.has_file_auth(profile):
            QtWidgets.QMessageBox.warning(
                self,
                "需要补建一次登录缓存",
                "没有在 .codex 中检测到 auth.json。当前可能使用了 Windows 凭据库。\n\n"
                "请打开 ChatGPT 的账号菜单退出，再用这个账号重新登录一次。",
            )

    def add_profile(self):
        dialog = NameDialog("添加桌面账号", "账号名称", "", self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            profile = self.store.add_empty_profile(dialog.value)
        except (AccountSwitcherError, ValueError, OSError) as error:
            QtWidgets.QMessageBox.critical(self, "创建失败", str(error))
            return
        self.refresh_table(profile.profile_id)
        self.check_all_quotas(force=True)
        answer = QtWidgets.QMessageBox.question(
            self,
            "账号档案已创建",
            "现在清空当前登录缓存并打开 Codex，完成这个账号唯一一次登录吗？\n\n"
            "本地对话不会移动或删除。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self._switch_and_launch(profile, allow_unbound=True)

    def _switch_and_launch(self, profile, allow_unbound=False):
        if not allow_unbound and not self.store.has_file_auth(profile):
            QtWidgets.QMessageBox.information(self, "尚未首次登录", "请先点击“首次登录此账号”。")
            return
        if profile.profile_id == self.store.active_profile_id:
            try:
                launch_chatgpt_windows()
            except (LaunchError, OSError) as error:
                QtWidgets.QMessageBox.critical(self, "启动失败", str(error))
                return
            if allow_unbound and not self.store.has_file_auth(profile):
                QtWidgets.QMessageBox.information(
                    self,
                    "请完成一次文件缓存登录",
                    "Codex 已打开。请在账号菜单中退出当前登录，然后用这个账号重新登录一次。\n\n"
                    "登录完成后回到本窗口点击“刷新状态”；以后即可直接切换。",
                )
            return
        if not self._ensure_app_closed():
            return
        self._stop_quota_worker()
        try:
            switched = self.store.switch_to(profile.profile_id, allow_unbound=allow_unbound)
        except (AccountSwitcherError, OSError) as error:
            QtWidgets.QMessageBox.critical(self, "切换失败", str(error))
            return
        self.refresh_table(switched.profile_id)
        self.check_all_quotas(force=True)
        try:
            launch_chatgpt_windows()
        except (LaunchError, OSError) as error:
            self.status_label.setText(
                f"已切换到“{switched.name}”，但 Codex 未能自动启动。"
            )
            QtWidgets.QMessageBox.warning(
                self,
                "已切换，但 Codex 未启动",
                f"登录缓存已成功切换到“{switched.name}”。请手动打开 Codex。\n\n{error}",
            )
            return
        self.status_label.setText(
            f"已打开“{switched.name}”的登录页；请完成首次登录。"
            if allow_unbound and not self.store.has_file_auth(switched)
            else f"已切换到“{switched.name}”。"
        )

    def switch_selected(self):
        profile = self._selected_profile()
        if profile:
            self._switch_and_launch(profile)

    def bind_selected(self):
        profile = self._selected_profile()
        if profile:
            self._switch_and_launch(profile, allow_unbound=True)

    def switch_next(self):
        profile = self.store.next_ready_profile()
        if not profile:
            QtWidgets.QMessageBox.information(self, "没有可用账号", "请先完成至少一个账号的首次登录。")
            return
        self._select(profile.profile_id)
        self._switch_and_launch(profile)

    def rename_selected(self):
        profile = self._selected_profile()
        if not profile:
            return
        dialog = NameDialog("重命名账号", "新名称", profile.name, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            self.store.rename_profile(profile.profile_id, dialog.value)
        except (ValueError, KeyError) as error:
            QtWidgets.QMessageBox.warning(self, "重命名失败", str(error))
            return
        self.refresh_table(profile.profile_id)

    def remove_selected(self):
        profile = self._selected_profile()
        if not profile:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "移除账号档案",
            f"确定移除“{profile.name}”吗？\n\n它会移入 archive 备份，不会永久删除。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self._stop_quota_worker()
        try:
            destination = self.store.archive_profile(profile.profile_id)
        except (AccountSwitcherError, KeyError, OSError) as error:
            QtWidgets.QMessageBox.critical(self, "移除失败", str(error))
            return
        self._quotas.pop(profile.profile_id, None)
        self.refresh_all()
        self.status_label.setText(f"已移到可恢复备份：{destination}")

    def open_store(self):
        try:
            open_path(self.store.root)
        except LaunchError as error:
            QtWidgets.QMessageBox.warning(self, "打开账号档案仓库", str(error))

    def shutdown(self):
        self._stop_quota_worker()
        for worker in list(self._lingering_workers):
            worker.wait(300)
        self._lingering_workers = []

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
