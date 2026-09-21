import logging
import os
import pathlib

from qt_compat import QtCore, QtGui, QtWidgets

from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand, PluginSettingsPage
from model.ClipboardHelper import is_internal_clipboard_content
from model.GlobalHotkey import (
    DEFAULT_INVENTORY_MANAGER_HOTKEY,
    GlobalHotkeyManager,
    INVENTORY_MANAGER_HOTKEY_CONFIG_KEY,
    INVENTORY_MANAGER_HOTKEY_ID,
    normalize_hotkey_sequence,
)
from model.InventoryManager import (
    InventoryStore,
    STATUS_CRITICAL,
    STATUS_INITIAL,
    STATUS_LABELS,
    STATUS_MODERATE,
    STATUS_NORMAL,
)
from model.MaterialDriveSync import (
    DEFAULT_MATERIAL_SYNC_DIRECTORY,
    MaterialSyncStateStore,
    normalize_material_sync_settings,
    sync_material_drive_folder,
    update_material_sync_config,
)
from model.MaterialSourceDownloader import parse_material_drive_link
from PYUI.utility_managers_pyui import (
    InventoryManagerDialog,
    MaterialGroupAssignmentDialog,
    google_drive_urls_from_mime_data,
)


class MaterialSyncThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(
        self,
        settings,
        state_store,
        force_ids=None,
        adopt_existing=False,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = dict(settings)
        self.state_store = state_store
        self.force_ids = tuple(force_ids or ())
        self.adopt_existing = bool(adopt_existing)

    def run(self):
        try:
            result = sync_material_drive_folder(
                self.settings,
                state_store=self.state_store,
                force_ids=self.force_ids,
                adopt_existing=self.adopt_existing,
                progress_callback=self.progress.emit,
            )
        except (Exception, SystemExit) as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
            return
        self.completed.emit(result)


class InventorySettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("库存与素材管理器", self.widget)
        title.setStyleSheet("font-weight:600;font-size:14px;")
        layout.addWidget(title)
        layout.addWidget(
            QtWidgets.QLabel(
                "管理普通库存、素材库和人物素材；窗口与主界面可以同时操作。",
                self.widget,
            )
        )
        form = QtWidgets.QFormLayout()
        self.hotkey_edit = QtWidgets.QKeySequenceEdit(self.widget)
        form.addRow("全局唤起快捷键：", self.hotkey_edit)
        self.clipboard_checkbox = QtWidgets.QCheckBox(
            "监听剪贴板中的 Google Drive/Docs 链接",
            self.widget,
        )
        form.addRow("剪贴板：", self.clipboard_checkbox)
        self.sync_interval_spin = QtWidgets.QSpinBox(self.widget)
        self.sync_interval_spin.setRange(1, 1440)
        self.sync_interval_spin.setSuffix(" 分钟")
        form.addRow("检查间隔：", self.sync_interval_spin)
        layout.addLayout(form)
        hint = QtWidgets.QLabel(
            "需要持续同步时，请在素材管理中勾选“持续监视网盘”。"
            "程序会记住每个文件的版本；本地素材被移动或删除后视为已使用，"
            "不会反复下载；只有网盘文件更新或你手动强制下载时才会再次下载。\n"
            "全局快捷键至少包含 Ctrl、Alt、Shift 或 Win 中的一个。",
            self.widget,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def load_config(self, config):
        value = config.get(
            INVENTORY_MANAGER_HOTKEY_CONFIG_KEY,
            DEFAULT_INVENTORY_MANAGER_HOTKEY,
        )
        try:
            value = normalize_hotkey_sequence(value)
        except ValueError:
            value = DEFAULT_INVENTORY_MANAGER_HOTKEY
        self.hotkey_edit.setKeySequence(QtGui.QKeySequence(value))
        self.clipboard_checkbox.setChecked(
            bool(config.get("clipboard_google_drive_monitor_enabled", False))
        )
        sync = normalize_material_sync_settings(config)
        self.sync_interval_spin.setValue(sync["interval_minutes"])

    def normalized_hotkey(self):
        return normalize_hotkey_sequence(
            self.hotkey_edit.keySequence().toString(QtGui.QKeySequence.PortableText)
        )

    def hotkey_fields(self):
        return (("库存与素材管理器", self.hotkey_edit, self.widget),)

    def validate(self):
        self.normalized_hotkey()

    def update_config(self, config):
        config[INVENTORY_MANAGER_HOTKEY_CONFIG_KEY] = self.normalized_hotkey()
        config["clipboard_google_drive_monitor_enabled"] = (
            self.clipboard_checkbox.isChecked()
        )
        sync = normalize_material_sync_settings(config)
        sync["interval_minutes"] = self.sync_interval_spin.value()
        update_material_sync_config(config, sync)


class InventoryPlugin:
    plugin_id = "inventory"
    display_name = "库存与素材管理"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.store = None
        self.dialog = None
        self.hotkey_manager = None
        self.global_hotkey = DEFAULT_INVENTORY_MANAGER_HOTKEY
        self.status_timer = None
        self._alert_signature = None
        self._inventory_error = ""
        self.clipboard_monitor_enabled = False
        self._last_clipboard_text = None
        self._clipboard_connected = False
        self.material_sync_settings = normalize_material_sync_settings({})
        self.material_sync_state_store = MaterialSyncStateStore()
        self.material_sync_timer = None
        self.material_sync_thread = None
        self.material_sync_tab = None
        self.material_sync_table = None
        self.material_sync_status_label = None

    def register(self, context):
        self.context = context
        context.register_command(
            PluginCommand(
                command_id="open",
                title="库存与素材管理器",
                callback=lambda _rows: self.open_manager(),
                locations=frozenset({MAIN_MENU}),
                tooltip="管理库存、素材库和人物素材",
                order=10,
            )
        )
        context.register_command(
            PluginCommand(
                command_id="clipboard_monitor",
                title="监听剪贴板中的 Google 链接",
                callback=lambda _rows, checked: self.set_clipboard_monitor(checked),
                locations=frozenset({MAIN_MENU}),
                tooltip=(
                    "发现 Google Drive/Docs 链接时自动显示素材管理器并填入链接；"
                    "程序自身复制的链接不会触发"
                ),
                order=20,
                checkable=True,
                checked=lambda: self.clipboard_monitor_enabled,
            )
        )
        context.register_command(
            PluginCommand(
                command_id="assign_images",
                title="分配库存图片…",
                callback=self.assign_images_to_tasks,
                locations=frozenset({TASK_CONTEXT_MENU}),
                tooltip="从素材或人物素材条目分配图片到所选任务",
                order=10,
                enabled=lambda rows: bool(rows),
            )
        )
        context.register_settings_page(
            PluginSettingsPage(
                page_id="settings",
                title="库存插件",
                factory=InventorySettingsPage,
                order=200,
            )
        )

    def start(self):
        self.store = InventoryStore()
        config = self.context.load_config()
        try:
            self.global_hotkey = normalize_hotkey_sequence(
                config.get(
                    INVENTORY_MANAGER_HOTKEY_CONFIG_KEY,
                    DEFAULT_INVENTORY_MANAGER_HOTKEY,
                )
            )
        except ValueError:
            self.global_hotkey = DEFAULT_INVENTORY_MANAGER_HOTKEY
        self.clipboard_monitor_enabled = bool(
            config.get("clipboard_google_drive_monitor_enabled", False)
        )
        self.material_sync_settings = normalize_material_sync_settings(config)
        self.hotkey_manager = GlobalHotkeyManager(
            self.context.parent_widget,
            hotkey_id=INVENTORY_MANAGER_HOTKEY_ID,
        )
        self.hotkey_manager.activated.connect(self.open_manager)
        self._register_hotkey(self.global_hotkey, show_error=False)
        self.status_timer = QtCore.QTimer(self.context.parent_widget)
        self.status_timer.setInterval(60_000)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start()
        self.material_sync_timer = QtCore.QTimer(self.context.parent_widget)
        self.material_sync_timer.timeout.connect(self.trigger_material_sync)
        self._configure_material_sync_timer()
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.dataChanged.connect(self.schedule_clipboard_inspection)
        self._clipboard_connected = True
        self.context.update_command(
            "clipboard_monitor",
            checked=self.clipboard_monitor_enabled,
        )
        if self.clipboard_monitor_enabled:
            QtCore.QTimer.singleShot(350, self.inspect_clipboard)
        if self.material_sync_settings["enabled"]:
            QtCore.QTimer.singleShot(1200, self.trigger_material_sync)
        QtCore.QTimer.singleShot(500, self.refresh_status)

    def _register_hotkey(self, shortcut, show_error=True):
        try:
            shortcut = normalize_hotkey_sequence(shortcut)
        except ValueError as error:
            self.context.log(f"全局快捷键无效：{error}", logging.ERROR)
            if show_error:
                QtWidgets.QMessageBox.warning(
                    self.context.parent_widget,
                    "全局快捷键无效",
                    f"库存与素材管理器：{error}",
                )
            return False
        if self.hotkey_manager.register(shortcut):
            self.global_hotkey = shortcut
            self.context.log(f"全局快捷键已启用：{shortcut}")
            self.refresh_status()
            return True
        message = (
            f"{shortcut} 无法注册，可能已被其他程序占用。"
            f" {self.hotkey_manager.last_error}"
        )
        self.context.log(f"全局快捷键未启用：{message}", logging.ERROR)
        if show_error:
            QtWidgets.QMessageBox.warning(
                self.context.parent_widget,
                "全局快捷键注册失败",
                message,
            )
        return False

    def apply_settings(self, config):
        shortcut = config.get(
            INVENTORY_MANAGER_HOTKEY_CONFIG_KEY,
            self.global_hotkey,
        )
        hotkey_registered = self._register_hotkey(shortcut)
        self.set_clipboard_monitor(
            bool(config.get("clipboard_google_drive_monitor_enabled", False)),
            save=False,
        )
        self.material_sync_settings = normalize_material_sync_settings(config)
        self._configure_material_sync_timer()
        return hotkey_registered

    def update_config(self, config):
        config[INVENTORY_MANAGER_HOTKEY_CONFIG_KEY] = self.global_hotkey
        config["clipboard_google_drive_monitor_enabled"] = bool(
            self.clipboard_monitor_enabled
        )
        update_material_sync_config(config, self.material_sync_settings)

    def _configure_material_sync_timer(self):
        if self.material_sync_timer is None:
            return
        self.material_sync_timer.stop()
        self.material_sync_timer.setInterval(
            int(self.material_sync_settings["interval_minutes"]) * 60_000
        )
        if self.material_sync_settings["enabled"]:
            self.material_sync_timer.start()

    def _ensure_material_sync_tab(self, dialog):
        if self.material_sync_tab is not None:
            return
        tab = QtWidgets.QWidget(dialog.tabs)
        layout = QtWidgets.QVBoxLayout(tab)
        intro = QtWidgets.QLabel(
            "这里是单向素材收件箱：网盘新增或更新会下载到本地；本地已移动/删除的旧版本不会自动补回。",
            tab,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        toolbar = QtWidgets.QHBoxLayout()
        sync_button = QtWidgets.QPushButton("立即检查", tab)
        force_button = QtWidgets.QPushButton("强制下载所选", tab)
        open_button = QtWidgets.QPushButton("打开同步目录", tab)
        toolbar.addWidget(sync_button)
        toolbar.addWidget(force_button)
        toolbar.addWidget(open_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        table = QtWidgets.QTableWidget(0, 4, tab)
        table.setHorizontalHeaderLabels(["状态", "网盘文件", "网盘更新时间", "本地位置"])
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(table, 1)
        status = QtWidgets.QLabel("尚未检查", tab)
        status.setWordWrap(True)
        layout.addWidget(status)
        dialog.tabs.addTab(tab, "网盘素材同步")
        self.material_sync_tab = tab
        self.material_sync_table = table
        self.material_sync_status_label = status
        sync_button.clicked.connect(self.trigger_material_sync)
        force_button.clicked.connect(self.force_selected_material_sync)
        open_button.clicked.connect(self.open_material_sync_directory)
        table.doubleClicked.connect(lambda _index: self.open_material_sync_directory())
        self._refresh_material_sync_table()

    def _refresh_material_sync_table(self, state=None):
        if self.material_sync_table is None:
            return
        if state is None:
            try:
                state = self.material_sync_state_store.load()
            except ValueError as error:
                self.material_sync_table.setRowCount(0)
                if self.material_sync_status_label is not None:
                    self.material_sync_status_label.setText(str(error))
                return
        records = sorted(
            state.get("files", {}).values(),
            key=lambda row: (not row.get("remote_present", True), str(row.get("name", "")).casefold()),
        )
        labels = {
            "present": "本地可用",
            "consumed": "已使用/移走",
            "remote_removed": "网盘已移除",
            "unsupported": "不支持下载",
            "error": "下载失败",
        }
        self.material_sync_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = (
                labels.get(record.get("local_status"), "未下载"),
                record.get("name", ""),
                str(record.get("modified_time", "")).replace("T", " ")[:19],
                record.get("local_path", ""),
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value or ""))
                item.setData(QtCore.Qt.UserRole, record.get("id"))
                if record.get("local_status") == "error":
                    item.setBackground(QtGui.QColor("#F4CCCC"))
                    item.setToolTip(str(record.get("last_error") or ""))
                elif record.get("local_status") == "consumed":
                    item.setBackground(QtGui.QColor("#D9EAD3"))
                self.material_sync_table.setItem(row, column, item)

    def trigger_material_sync(
        self,
        _checked=False,
        force_ids=None,
        adopt_existing=False,
    ):
        if self.material_sync_thread is not None and self.material_sync_thread.isRunning():
            if self.material_sync_status_label is not None:
                self.material_sync_status_label.setText("同步正在进行，请稍候。")
            return False
        settings = normalize_material_sync_settings(self.context.load_config())
        if not settings["folder_url"]:
            if self.material_sync_status_label is not None:
                self.material_sync_status_label.setText("请先在程序设置 → 库存插件中配置网盘文件夹。")
            return False
        self.material_sync_settings = settings
        thread = MaterialSyncThread(
            settings,
            self.material_sync_state_store,
            force_ids=force_ids,
            adopt_existing=adopt_existing,
            parent=self.context.parent_widget,
        )
        self.material_sync_thread = thread
        thread.progress.connect(self._material_sync_progress)
        thread.completed.connect(self._material_sync_completed)
        thread.failed.connect(self._material_sync_failed)
        thread.finished.connect(self._material_sync_finished)
        if self.material_sync_status_label is not None:
            self.material_sync_status_label.setText("正在检查网盘素材…")
        self.context.log("开始检查持续同步素材文件夹。")
        thread.start()
        return True

    def force_selected_material_sync(self):
        rows = self.material_sync_table.selectionModel().selectedRows() if self.material_sync_table else []
        file_ids = {
            str(self.material_sync_table.item(index.row(), 0).data(QtCore.Qt.UserRole) or "")
            for index in rows
            if self.material_sync_table.item(index.row(), 0) is not None
        }
        file_ids.discard("")
        if not file_ids:
            QtWidgets.QMessageBox.information(
                self.dialog,
                "强制下载",
                "请先选择需要重新下载的文件记录。",
            )
            return
        self.trigger_material_sync(force_ids=file_ids)

    def open_material_sync_directory(self):
        target = pathlib.Path(self.material_sync_settings["local_dir"])
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QtWidgets.QMessageBox.warning(self.dialog, "打开同步目录", str(error))
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target.resolve())))

    def _material_sync_progress(self, message):
        if self.material_sync_status_label is not None:
            self.material_sync_status_label.setText(str(message))
        self.context.log(f"网盘素材：{message}")

    def _material_sync_completed(self, result):
        count = int(result.get("downloaded_count", 0))
        errors = list(result.get("errors", []))
        message = f"检查完成，新下载或更新 {count} 个文件"
        if errors:
            message += f"，{len(errors)} 个失败"
        if self.material_sync_status_label is not None:
            self.material_sync_status_label.setText(message)
        self._refresh_material_sync_table(result.get("state"))
        if self.dialog is not None:
            self.dialog.refresh_materials()
        self.context.log(message + "。")
        if count:
            self.context.notify(
                "网盘素材已更新",
                f"已下载 {count} 个新增或更新的素材。",
            )
        if errors:
            self.context.log("网盘素材同步失败：" + "；".join(errors), logging.ERROR)

    def _material_sync_failed(self, message):
        if self.material_sync_status_label is not None:
            self.material_sync_status_label.setText(f"检查失败：{message}")
        self.context.log(f"网盘素材同步失败：{message}", logging.ERROR)

    def _material_sync_finished(self):
        thread = self.material_sync_thread
        self.material_sync_thread = None
        if thread is not None:
            thread.deleteLater()

    def enable_material_monitor(self, request):
        request = dict(request or {})
        folder_url = str(request.get("folder_url") or "").strip()
        local_dir = str(request.get("local_dir") or "").strip()
        try:
            if not parse_material_drive_link(folder_url).is_folder:
                raise ValueError("不是 Google Drive 文件夹链接")
        except Exception as error:
            QtWidgets.QMessageBox.warning(
                self.dialog,
                "无法开启网盘监视",
                str(error),
            )
            return False
        material_root = pathlib.Path(local_dir)
        try:
            child_directories = [
                path for path in material_root.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
        except OSError:
            child_directories = []
        if len(child_directories) == 1:
            local_dir = str(child_directories[0].resolve())
        previous = dict(self.material_sync_settings)
        self.material_sync_settings.update({
            "enabled": True,
            "folder_url": folder_url,
            "local_dir": local_dir,
            "material_id": str(request.get("material_id") or ""),
        })
        if not self.context.save_config():
            self.material_sync_settings = previous
            return False
        self._configure_material_sync_timer()
        if self.dialog is not None:
            self.dialog.set_material_sync_settings(self.material_sync_settings)
        self.context.log("已开启素材网盘持续监视。")
        return self.trigger_material_sync(adopt_existing=True)

    def stop_material_monitor(self):
        if not self.material_sync_settings.get("enabled"):
            return True
        previous = dict(self.material_sync_settings)
        self.material_sync_settings["enabled"] = False
        if not self.context.save_config():
            self.material_sync_settings = previous
            return False
        self._configure_material_sync_timer()
        if self.dialog is not None:
            self.dialog.set_material_sync_settings(self.material_sync_settings)
        self.context.log("已停止素材网盘持续监视。")
        return True

    def set_clipboard_monitor(self, enabled, save=True):
        previous = self.clipboard_monitor_enabled
        self.clipboard_monitor_enabled = bool(enabled)
        self._last_clipboard_text = None
        if save and not self.context.save_config():
            self.clipboard_monitor_enabled = previous
            self.context.update_command("clipboard_monitor", checked=previous)
            return False
        self.context.update_command(
            "clipboard_monitor",
            checked=self.clipboard_monitor_enabled,
        )
        state_text = "已开启" if self.clipboard_monitor_enabled else "已关闭"
        self.context.log(f"剪贴板 Google 链接监听{state_text}。")
        if self.clipboard_monitor_enabled:
            self.schedule_clipboard_inspection()
        return True

    def schedule_clipboard_inspection(self):
        if self.clipboard_monitor_enabled:
            QtCore.QTimer.singleShot(0, self.inspect_clipboard)

    def inspect_clipboard(self):
        if not self.clipboard_monitor_enabled:
            return 0
        clipboard = QtWidgets.QApplication.clipboard()
        if is_internal_clipboard_content(clipboard):
            return 0
        mime_data = clipboard.mimeData()
        clipboard_text = clipboard.text().strip()
        clipboard_value = clipboard_text
        if mime_data.hasHtml():
            clipboard_value = f"{clipboard_text}\0{mime_data.html()}"
        if clipboard_value == self._last_clipboard_text:
            return 0
        self._last_clipboard_text = clipboard_value
        links = google_drive_urls_from_mime_data(mime_data)
        if not links or self.is_dialog_visible():
            return 0
        added_count = self.open_manager(
            material_sources=links,
            source_label="剪贴板",
        )
        self.context.log(
            f"剪贴板识别到 {len(links)} 个 Google 链接，"
            f"已向素材来源新增 {added_count} 个。"
        )
        return added_count

    @staticmethod
    def _set_native_topmost(dialog, enabled):
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = (
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd = wintypes.HWND(int(dialog.winId()))
            insert_after = wintypes.HWND(-1 if enabled else -2)
            flags = 0x0001 | 0x0002
            flags |= 0x0040 if enabled else 0x0010
            changed = bool(user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags))
            if enabled:
                user32.SetForegroundWindow(hwnd)
            return changed
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def _bring_to_front(self, dialog):
        if dialog.isMinimized():
            dialog.showNormal()
        else:
            dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if not self._set_native_topmost(dialog, True):
            return
        timer = getattr(dialog, "_topmost_release_timer", None)
        if timer is None:
            timer = QtCore.QTimer(dialog)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._set_native_topmost(dialog, False))
            dialog._topmost_release_timer = timer
        timer.start(2500)

    def is_dialog_visible(self):
        return self.dialog is not None and self.dialog.isVisible()

    def open_manager(self, material_sources=None, source_label="剪贴板"):
        if self.store is None:
            self.store = InventoryStore()
        dialog = self.dialog
        if dialog is None:
            dialog = InventoryManagerDialog(
                self.store,
                material_sync_settings=self.material_sync_settings,
            )
            dialog.setWindowIcon(self.context.parent_widget.windowIcon())
            dialog.setModal(False)
            dialog.setWindowModality(QtCore.Qt.NonModal)
            dialog.changed.connect(self.refresh_status)
            dialog.log.connect(lambda message: self.context.log(f"素材：{message}"))
            dialog.material_monitor_requested.connect(
                self.enable_material_monitor
            )
            dialog.material_monitor_stopped.connect(
                self.stop_material_monitor
            )
            dialog.material_monitor_sync_requested.connect(
                self.trigger_material_sync
            )
            self.dialog = dialog
        else:
            dialog.set_material_sync_settings(self.material_sync_settings)
            dialog.refresh()
            dialog.refresh_materials()
            dialog.refresh_people()
        added_count = 0
        if material_sources:
            added_count = dialog.prepare_material_sources(
                material_sources,
                source_label=source_label,
            )
        self._bring_to_front(dialog)
        self.refresh_status()
        return added_count

    def refresh_status(self):
        if self.store is None:
            return
        try:
            summary = self.store.summary()
        except (OSError, ValueError) as error:
            message = str(error)
            self.context.update_command(
                "open",
                title="库存与素材管理器（异常）",
                tooltip=f"{message}\n全局快捷键：{self.global_hotkey}",
            )
            if message != self._inventory_error:
                self.context.log(f"库存记录异常：{message}", logging.ERROR)
                self.context.notify("库存记录异常", message, critical=True)
                self._inventory_error = message
            return

        self._inventory_error = ""
        alerts = [item for item in summary["items"] if item["status"] != STATUS_NORMAL]
        signature = tuple(sorted((item["id"], item["status"]) for item in alerts))
        worst = summary["worst"]
        if worst == STATUS_CRITICAL:
            suffix = f"高危 {summary['counts'][STATUS_CRITICAL]}"
        elif worst == STATUS_MODERATE:
            suffix = f"中度报警 {summary['counts'][STATUS_MODERATE]}"
        elif worst == STATUS_INITIAL:
            suffix = f"初步报警 {summary['counts'][STATUS_INITIAL]}"
        else:
            suffix = ""
        title = "库存与素材管理器" + (f"（{suffix}）" if suffix else "")
        self.context.update_command(
            "open",
            title=title,
            tooltip=(
                "黄色不足2天，橙色不足1天，红色已经耗尽\n"
                f"全局快捷键：{self.global_hotkey}"
            ),
        )
        if alerts and signature != self._alert_signature:
            names = "、".join(
                f"{item['name']}（{STATUS_LABELS[item['status']]}）"
                for item in alerts[:5]
            )
            if len(alerts) > 5:
                names += f"等 {len(alerts)} 项"
            self.context.notify(
                "库存报警",
                names,
                critical=worst == STATUS_CRITICAL,
            )
        self._alert_signature = signature

    def assign_images_to_tasks(self, rows=None):
        rows = self.context.selected_task_rows() if rows is None else list(rows)
        targets = self.context.task_targets(rows)
        if not targets:
            return
        try:
            groups = self.store.list_image_groups()
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.critical(
                self.context.parent_widget,
                "读取素材库失败",
                str(error),
            )
            self.context.log(f"图片分配：读取素材库失败：{error}", logging.ERROR)
            return
        if not groups or not any(group.get("images") for group in groups):
            QtWidgets.QMessageBox.information(
                self.context.parent_widget,
                "素材库中没有图片",
                "暂无可分配图片，请先添加素材。",
            )
            return

        dialog = MaterialGroupAssignmentDialog(groups, targets, self.context.parent_widget)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        assignments = dialog.assignments()
        distribution = dialog.distribution_summary()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            result = self.store.move_material_images(assignments)
        except (OSError, ValueError, KeyError) as error:
            self.context.log(f"图片分配失败：{error}", logging.ERROR)
            QtWidgets.QMessageBox.critical(
                self.context.parent_widget,
                "图片分配失败",
                f"{error}\n\n已尝试把本轮已经移动的图片放回素材库。",
            )
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        moved = result.get("moved", [])
        removed_materials = result.get("removed_materials", [])
        counts = {}
        for item in moved:
            label = item.get("task_label") or "未命名任务"
            counts[label] = counts.get(label, 0) + 1
            self.context.log(
                f"图片分配：{pathlib.Path(item['target']).name} -> {label}"
            )
        if self.dialog is not None:
            self.dialog.refresh_materials()
            self.dialog.refresh_people()
        summary_lines = [f"已移动 {len(moved)} 张图片，分配到 {len(counts)} 个任务。"]
        if removed_materials:
            summary_lines.append(f"已清理 {len(removed_materials)} 个空素材条目。")
        if distribution.get("missing_count"):
            first_missing = distribution.get("first_missing_task") or "未知任务"
            summary_lines.append(
                f"仍有 {distribution['missing_count']} 个任务缺图，从“{first_missing}”开始。"
            )
        elif distribution.get("remaining_count"):
            summary_lines.append(f"素材库剩余 {distribution['remaining_count']} 张图片。")
        self.context.log(
            f"图片分配完成，共移动 {len(moved)} 张图片到 {len(counts)} 个任务。"
        )
        QtWidgets.QMessageBox.information(
            self.context.parent_widget,
            "图片分配完成",
            "\n".join(summary_lines),
        )

    def can_close(self):
        thread = self.dialog.material_copy_thread if self.dialog is not None else None
        if thread is not None and thread.isRunning():
            self.open_manager()
            return (
                False,
                "素材正在复制或下载。可以隐藏管理窗口并继续使用主界面，"
                "但请等待处理完成后再退出主程序。",
            )
        if self.material_sync_thread is not None and self.material_sync_thread.isRunning():
            self.open_manager()
            return False, "网盘素材正在同步，请等待完成后再退出主程序。"
        return True, ""

    def stop(self):
        self.clipboard_monitor_enabled = False
        if self._clipboard_connected:
            try:
                QtWidgets.QApplication.clipboard().dataChanged.disconnect(
                    self.schedule_clipboard_inspection
                )
            except (TypeError, RuntimeError):
                pass
            self._clipboard_connected = False
        if self.status_timer is not None:
            self.status_timer.stop()
            self.status_timer.deleteLater()
            self.status_timer = None
        if self.material_sync_timer is not None:
            self.material_sync_timer.stop()
            self.material_sync_timer.deleteLater()
            self.material_sync_timer = None
        if self.hotkey_manager is not None:
            self.hotkey_manager.close()
            self.hotkey_manager.deleteLater()
            self.hotkey_manager = None
        if self.dialog is not None:
            self.dialog.hide()
            self.dialog.deleteLater()
            self.dialog = None
