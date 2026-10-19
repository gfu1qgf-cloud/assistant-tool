"""PyQt6 dialog for Antigravity multi-account switcher."""

from __future__ import annotations
from functools import wraps

from qt_compat import QtCore, QtGui, QtWidgets
from model.AppDataStorage import data_directory
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active

from .launcher import (
    LaunchError,
    close_antigravity,
    is_antigravity_running,
    launch_antigravity,
    open_path,
)
from .quota import AntigravityQuotaInfo, AntigravityQuotaWorker
from .store import AccountSwitcherError, AntigravityProfile, AntigravityProfileStore
from .operations import AccountOperationWorker


def account_action(callback):
    """A storage failure must never escape a Qt button callback and abort Python."""
    @wraps(callback)
    def invoke(self, *_signal_arguments):
        try:
            return callback(self)
        except (AccountSwitcherError, LaunchError, OSError, ValueError, KeyError) as error:
            QtWidgets.QMessageBox.warning(self, "账号操作未完成", str(error))
        except Exception:
            QtWidgets.QMessageBox.warning(self, "账号操作未完成", "操作未完成，未自动重置账号档案。请保留账号仓库并核对客户端状态。")
    return invoke


class NameDialog(QtWidgets.QDialog):
    def __init__(self, title, prompt, value="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.edit = QtWidgets.QLineEdit(value, self)
        self.edit.setPlaceholderText("例如：主号 / 备用号 / Google AI Ultra")
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


class AntigravitySwitcherDialog(QtWidgets.QDialog):
    def __init__(self, store: AntigravityProfileStore, parent=None, *, clients=(), change_client=None):
        super().__init__(parent)
        self.store = store
        self._quotas: dict[str, AntigravityQuotaInfo] = {}
        self._quota_worker: AntigravityQuotaWorker | None = None
        self._operation_worker = None
        self._live_info = None
        self._clients = list(clients)
        self._change_client = change_client
        self.setWindowTitle("Antigravity 多账号切换")
        self.resize(1020, 680)
        self.setMinimumSize(860, 600)
        self._build_ui()
        self.refresh_table()

        self.check_all_quotas()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Antigravity 多账号切换", self)
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel(
            "保存和切换自己的 Antigravity 登录档案。只更换登录记录，不搬动工作区、项目和历史对话。",
            self,
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        warning = QtWidgets.QLabel(
            "账号仓库使用主程序当前加密数据位置。切换前须保存工作，插件只请求客户端正常退出，不强杀进程。\n"
            "本地套餐记录可能过期；当前不查询实时剩余额度。首次使用先保存当前账号，切换后客户端可能仍要求重新登录。",
            self,
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #8a5a00;")
        layout.addWidget(warning)
        self.source_combo = QtWidgets.QComboBox(self)
        for client in self._clients:
            self.source_combo.addItem(client.title, client)
        if self.store.client is not None:
            for index, client in enumerate(self._clients):
                if client.identity == self.store.client.identity:
                    self.source_combo.setCurrentIndex(index)
        self.source_combo.setVisible(bool(self._clients))
        self.source_combo.currentIndexChanged.connect(self._select_client)
        layout.addWidget(self.source_combo)
        self.source_label = QtWidgets.QLabel(self)
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(QtWidgets.QLabel("已保存账号档案", self))
        heading.addStretch(1)

        self.adopt_button = QtWidgets.QPushButton("保存当前账号为首个档案", self)
        self.add_button = QtWidgets.QPushButton("＋ 添加新账号", self)
        self.refresh_button = QtWidgets.QPushButton("刷新状态", self)
        self.quota_button = QtWidgets.QPushButton("检测本地账号状态", self)

        self.adopt_button.clicked.connect(self.adopt_current)
        self.add_button.clicked.connect(self.add_profile)
        self.refresh_button.clicked.connect(self.refresh_all)
        self.quota_button.clicked.connect(self.check_all_quotas)

        for btn in (self.adopt_button, self.add_button, self.refresh_button, self.quota_button):
            heading.addWidget(btn)
        layout.addLayout(heading)

        self.table = QtWidgets.QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["账号名称", "账号 / 邮箱", "套餐 / 状态", "最近使用", "当前状态"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)

        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.switch_selected())
        layout.addWidget(self.table, 1)

        self.active_label = QtWidgets.QLabel("当前：尚未建立账号档案", self)
        self.active_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.active_label)
        self.recover_button = QtWidgets.QPushButton("恢复上次未完成的切换", self)
        self.recover_button.clicked.connect(self.recover_previous)
        layout.addWidget(self.recover_button)

        primary = QtWidgets.QHBoxLayout()
        self.switch_button = QtWidgets.QPushButton("切换并打开 Antigravity", self)
        self.switch_button.setStyleSheet("font-weight: 600;")
        self.next_button = QtWidgets.QPushButton("切换到下一个就绪账号", self)
        self.login_button = QtWidgets.QPushButton("首次登录/绑定此账号", self)

        self.switch_button.clicked.connect(self.switch_selected)
        self.next_button.clicked.connect(self.switch_next)
        self.login_button.clicked.connect(self.bind_selected)

        primary.addWidget(self.switch_button, 2)
        primary.addWidget(self.next_button)
        primary.addWidget(self.login_button)
        layout.addLayout(primary)

        management = QtWidgets.QHBoxLayout()
        self.open_button = QtWidgets.QPushButton("打开账号凭据仓库", self)
        self.rename_button = QtWidgets.QPushButton("重命名", self)
        self.remove_button = QtWidgets.QPushButton("归档选中账号", self)

        self.open_button.clicked.connect(self.open_store)
        self.rename_button.clicked.connect(self.rename_selected)
        self.remove_button.clicked.connect(self.remove_selected)

        management.addWidget(self.open_button)
        management.addStretch(1)
        management.addWidget(self.rename_button)
        management.addWidget(self.remove_button)
        layout.addLayout(management)

    def refresh_table(self):
        profiles = self.store.profiles()
        active_id = self.store.active_profile_id
        selected_id = self.selected_profile_id()

        self.table.setRowCount(len(profiles))
        select_row = -1

        for row, profile in enumerate(profiles):
            quota = self._quotas.get(profile.profile_id)
            is_active = bool(quota and self._live_info and quota.email and
                             self._live_info.email and quota.email.casefold() == self._live_info.email.casefold())

            item_name = QtWidgets.QTableWidgetItem(profile.name)
            item_name.setData(QtCore.Qt.UserRole, profile.profile_id)

            email_str = quota.email if (quota and quota.email) else "—"
            item_email = QtWidgets.QTableWidgetItem(email_str)

            plan_str = quota.status_text if quota else ("已就绪" if self.store.has_auth(profile) else "未绑定")
            item_plan = QtWidgets.QTableWidgetItem(plan_str)
            if quota:
                item_plan.setToolTip(quota.detail_text)

            last_used = profile.last_used_at[:19].replace("T", " ") if profile.last_used_at else "从未"
            item_used = QtWidgets.QTableWidgetItem(last_used)

            status_str = ("等待完成首次登录" if profile.profile_id == self.store.binding_profile_id else
                          "与客户端缓存账号一致" if is_active else
                          "可切换" if self.store.has_auth(profile) else "待首次登录")
            item_status = QtWidgets.QTableWidgetItem(status_str)
            if is_active:
                item_status.setForeground(QtGui.QBrush(QtGui.QColor("#008800")))
                font = item_name.font()
                font.setBold(True)
                item_name.setFont(font)

            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_email)
            self.table.setItem(row, 2, item_plan)
            self.table.setItem(row, 3, item_used)
            self.table.setItem(row, 4, item_status)

            if profile.profile_id == selected_id or (selected_id is None and is_active):
                select_row = row

        if select_row >= 0:
            self.table.selectRow(select_row)

        current_email = self._live_info.email if self._live_info else None
        self.active_label.setText('所选客户端缓存账号：' + (current_email or '尚未确认') + '（未联网验证）')
        self.source_label.setText('登录数据来源：' + self.store.auth_source +
            ('\n客户端：' + str(self.store.client.executable) if self.store.client else '') +
            ('\n旧版本档案来源未绑定，仍保留在原仓库，没有当作本客户端当前账号；请先核对上方来源及检测邮箱，再保存当前账号。'
             if getattr(self.store, 'legacy_archives', False) else ''))
        if self.store.needs_recovery:
            self.active_label.setText("上次切换未完成。请先保存并关闭 Antigravity，再点击恢复。")
        self.recover_button.setVisible(self.store.needs_recovery)

        self.adopt_button.setEnabled(len(profiles) == 0)
        self.update_buttons()

    def update_buttons(self):
        try:
            self._update_buttons()
        except Exception as err:
            self.active_label.setText(str(err))
            for button in (self.switch_button, self.next_button, self.login_button,
                           self.rename_button, self.remove_button, self.adopt_button,
                           self.add_button, self.quota_button, self.recover_button):
                button.setEnabled(False)

    def _update_buttons(self):
        profile_id = self.selected_profile_id()
        profile = self.store.profile(profile_id) if profile_id else None
        has_sel = profile is not None
        is_active = profile is not None and profile.profile_id == self.store.active_profile_id
        is_ready = profile is not None and self.store.has_auth(profile)

        busy = thread_is_active(self._quota_worker) or thread_is_active(self._operation_worker)
        editable = not busy and not self.store.needs_recovery
        self.switch_button.setEnabled(editable and has_sel and not is_active and is_ready)
        self.next_button.setEnabled(editable and len([p for p in self.store.profiles() if self.store.has_auth(p)]) > 1)
        self.login_button.setEnabled(editable and has_sel and not is_ready and profile_id != self.store.binding_profile_id)
        self.rename_button.setEnabled(editable and has_sel)
        self.remove_button.setEnabled(editable and has_sel and not is_active)
        self.adopt_button.setEnabled(editable and not self.store.profiles())
        self.add_button.setEnabled(editable and self.store.active_profile is not None)
        self.refresh_button.setEnabled(not busy)
        self.quota_button.setEnabled(not busy)
        self.recover_button.setEnabled(not busy)
        self.source_combo.setEnabled(not busy)

    def selected_profile_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(QtCore.Qt.UserRole) if item else None

    def refresh_all(self):
        if thread_is_active(self._operation_worker) or thread_is_active(self._quota_worker):
            return
        try:
            if not self.store.needs_recovery and self.store.active_profile is not None:
                self.store.capture_current()
            self.refresh_table()
            self.check_all_quotas()
        except Exception as err:
            self._live_info = None
            self.active_label.setText('当前账号尚未确认；本次刷新失败，未改读其他来源。')
            QtWidgets.QMessageBox.warning(self, "无法刷新账号", str(err))

    def check_all_quotas(self):
        try:
            self._check_all_quotas()
        except Exception as err:
            self.active_label.setText(str(err))

    def _check_all_quotas(self):
        if thread_is_active(self._quota_worker) or thread_is_active(self._operation_worker):
            return
        targets = [('__live__', self.store.live_db_path.parent)]
        for p in self.store.profiles():
            cred_dir = self.store.profile_db_path(p).parent
            targets.append((p.profile_id, cred_dir))
        if not targets:
            self.update_buttons()
            return
        self.quota_button.setEnabled(False)
        self.quota_button.setText("正在检测…")
        self._quota_worker = AntigravityQuotaWorker(targets, self, live_reader=self.store.live_records)
        self._quota_worker.quota_ready.connect(self._on_quota_ready)
        connect_thread_finished(self._quota_worker, self, "_quota_worker", self._on_quota_finished)
        self._quota_worker.start()
        self.update_buttons()

    def _on_quota_ready(self, profile_id: str, quota: AntigravityQuotaInfo):
        if profile_id == '__live__':
            self._live_info = quota
        else:
            self._quotas[profile_id] = quota
        try:
            self.refresh_table()
        except (OSError, AccountSwitcherError) as err:
            self.active_label.setText(str(err))

    def _on_quota_finished(self):
        self._quota_worker = None
        self.quota_button.setEnabled(True)
        self.quota_button.setText("检测本地账号状态")
        self.update_buttons()

    @account_action
    def _select_client(self):
        client = self.source_combo.currentData()
        if client is None or self._change_client is None:
            return
        try:
            self._change_client(client)
        finally:
            blocker = QtCore.QSignalBlocker(self.source_combo)
            for index, target in enumerate(self._clients):
                if self.store.client and target.identity == self.store.client.identity:
                    self.source_combo.setCurrentIndex(index)
            del blocker

    def set_store(self, store):
        if self._quota_worker is not None or self._operation_worker is not None:
            return False
        self.store = store
        self._live_info = None
        self._quotas.clear()
        self.refresh_table()
        self.check_all_quotas()
        return True

    def set_clients(self, clients):
        self._clients = list(clients)
        blocker = QtCore.QSignalBlocker(self.source_combo)
        self.source_combo.clear()
        for client in self._clients:
            self.source_combo.addItem(client.title, client)
            if self.store.client and client.identity == self.store.client.identity:
                self.source_combo.setCurrentIndex(self.source_combo.count() - 1)
        self.source_combo.setVisible(bool(self._clients))
        del blocker

    def adopt_current(self):
        dialog = NameDialog("保存当前账号", "账号名称：", "主账号", self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            self.store.adopt_current(dialog.value)
            self.refresh_table()
            self.check_all_quotas()
            QtWidgets.QMessageBox.information(self, "保存成功", "已成功将当前登录账号保存为第一个档案。")
        except Exception as err:
            QtWidgets.QMessageBox.critical(self, "操作失败", str(err))

    def add_profile(self):
        dialog = NameDialog("添加新账号", "新账号名称：", "", self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            self.store.add_profile(dialog.value)
            self.refresh_table()
            QtWidgets.QMessageBox.information(
                self,
                "添加成功",
                "新档案已添加。若要完成绑定，请选中该账号后点击【首次登录/绑定此账号】。",
            )
        except Exception as err:
            QtWidgets.QMessageBox.critical(self, "添加失败", str(err))

    @account_action
    def switch_selected(self):
        profile_id = self.selected_profile_id()
        if not profile_id:
            return
        self._do_switch(profile_id)

    @account_action
    def switch_next(self):
        next_p = self.store.next_ready_profile()
        if not next_p:
            QtWidgets.QMessageBox.information(self, "提示", "没有其他已登录的账号可供切换。")
            return
        self._do_switch(next_p.profile_id)

    def _do_switch(self, target_id: str):
        target = self.store.profile(target_id)
        if not target:
            return

        reply = QtWidgets.QMessageBox.question(
            self, "准备切换",
            f"切换到【{target.name}】前会尝试正常退出 Antigravity，再重新打开。\n请先保存工作。是否继续？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self._start_operation("switch", target_id)

    @account_action
    def bind_selected(self):
        profile_id = self.selected_profile_id()
        profile = self.store.profile(profile_id)
        if not profile:
            return

        reply = QtWidgets.QMessageBox.question(
            self,
            "首次绑定新账号",
            f"绑定流程：\n1. 点击确定后将切换当前环境并打开 Antigravity\n2. 请在客户端中完成 Google 账号登录\n3. 登录完成后回到此插件点击【刷新状态】即可完成凭据存档。\n\n是否继续？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        self._start_operation("bind", profile_id)

    @account_action
    def recover_previous(self):
        reply = QtWidgets.QMessageBox.question(
            self, "恢复上次账号", "请先保存 Antigravity 中的工作。是否尝试正常退出客户端并恢复上次账号？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self._start_operation("recover")

    def _start_operation(self, operation, target_id=None):
        if thread_is_active(self._operation_worker) or thread_is_active(self._quota_worker):
            return
        worker = AccountOperationWorker(self.store, operation, target_id, self)
        self._operation_worker = worker
        connect_thread_finished(worker, self, "_operation_worker", self._operation_finished, pass_thread=True)
        self.active_label.setText("正在安全处理账号，请稍候；不会强制结束客户端。")
        worker.start()
        self.update_buttons()

    def _operation_finished(self, worker):
        try:
            self._operation_worker = None
            self._quotas.clear()
            self.refresh_table()
            if worker.error:
                QtWidgets.QMessageBox.warning(self, "账号操作提示", worker.error)
            else:
                self.check_all_quotas()
        except Exception as err:
            self.active_label.setText(str(err))

    @account_action
    def rename_selected(self):
        profile_id = self.selected_profile_id()
        profile = self.store.profile(profile_id)
        if not profile:
            return
        dialog = NameDialog("重命名账号", "新名称：", profile.name, self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            try:
                self.store.rename_profile(profile_id, dialog.value)
                self.refresh_table()
            except Exception as err:
                QtWidgets.QMessageBox.critical(self, "重命名失败", str(err))

    @account_action
    def remove_selected(self):
        profile_id = self.selected_profile_id()
        profile = self.store.profile(profile_id)
        if not profile:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "确认移除",
            f"确定移除账号【{profile.name}】吗？其凭据将被安全归档。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply == QtWidgets.QMessageBox.Yes:
            try:
                self.store.archive_profile(profile_id)
                self._quotas.pop(profile_id, None)
                self.refresh_table()
            except Exception as err:
                QtWidgets.QMessageBox.critical(self, "移除失败", str(err))

    def open_store(self):
        try:
            data_directory(self.store.root).exists()
            open_path(self.store.root)
        except (LaunchError, OSError) as err:
            QtWidgets.QMessageBox.critical(self, "无法打开", str(err))

    def _show_first_run(self):
        QtWidgets.QMessageBox.information(
            self,
            "欢迎使用 Antigravity 多账号切换",
            "检测到您尚未建立账号档案。\n点击顶部的【保存当前账号为首个档案】即可立即将当前登录态纳管！",
        )

    def shutdown(self) -> bool:
        if thread_is_active(self._quota_worker):
            self._quota_worker.cancel()
        # A QThread's result signal is not proof native cleanup has finished.
        return not thread_is_active(self._quota_worker) and not thread_is_active(self._operation_worker)

    def closeEvent(self, event):
        if not self.shutdown():
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)
