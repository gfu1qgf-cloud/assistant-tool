"""Sheet monitor plugin: retains original settings, baseline and pending queue."""
from qt_compat import QtCore, QMessageBox
from app_plugins.api import MAIN_MENU, PluginCommand, GoogleAuthorization
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
from model.GoogleSheetMonitor import GoogleSheetMonitorThread, normalize_monitor_settings, reset_monitor_baseline
from model.OfflineMode import is_offline
from .ui import GoogleSheetMonitorDialog

CONFIG_KEY = 'google_sheet_monitor'


class SheetMonitorPlugin(QtCore.QObject):
    plugin_id = 'sheet_monitor'
    display_name = '表格监视器'
    version = '1.0'
    required_api_version = 2

    def __init__(self):
        super().__init__()
        self.context = None
        self.google_sheet_monitor_settings = normalize_monitor_settings({})
        self.google_sheet_monitor_thread = None
        self.google_sheet_monitor_status = '未启动'
        self.google_sheet_monitor_pending = self.google_sheet_monitor_unread = 0

    def register(self, context):
        self.context = context
        self.setParent(context.parent_widget)
        self.google_sheet_monitor_settings = normalize_monitor_settings(context.load_config().get(CONFIG_KEY))
        context.register_google_authorization(GoogleAuthorization('sheets', '表格监视器', 'sheets', 'v4',
            ('https://www.googleapis.com/auth/spreadsheets',), order=40, allow_legacy=True,
            legacy_bindings=('core.sheet_monitor',)))
        context.register_command(PluginCommand('open', '表格监视器…', lambda _rows: self.openGoogleSheetMonitor(),
            frozenset({MAIN_MENU}), order=41))
        context.register_command(PluginCommand('restart', '重新连接表格监视器', lambda _rows: self.restartGoogleSheetMonitor(),
            frozenset({MAIN_MENU}), order=42, submenu='表格监视器'))

    @property
    def loaded_project_dir(self): return self.context.current_project_directory()

    def load_config(self): return self.context.load_config()
    def saveCurrentConfig(self): return self.context.save_config()
    def appendLog(self, message): self.context.log(message)
    def showDesktopNotification(self, title, message, critical=False): self.context.notify(title, message, critical)

    def start(self): self.startGoogleSheetMonitor()
    def stop(self): return self.stopGoogleSheetMonitor()
    def can_close(self): return True, ''

    def apply_settings(self, config):
        settings = normalize_monitor_settings(config.get(CONFIG_KEY))
        if settings == self.google_sheet_monitor_settings: return True
        if not self.stopGoogleSheetMonitor(): return False
        self.google_sheet_monitor_settings = settings
        self.startGoogleSheetMonitor()
        return True

    def update_config(self, config):
        config[CONFIG_KEY] = dict(self.google_sheet_monitor_settings)

    def on_project_changed(self, project, *, validated=False):
        thread = self.google_sheet_monitor_thread
        if thread is not None: thread.set_project_dir(project, validated=validated)

    def startGoogleSheetMonitor(self):
        if is_offline():
            self.google_sheet_monitor_status = '离线暂停'
            self.updateGoogleSheetMonitorButton()
            return
        if not self.google_sheet_monitor_settings.get("enabled"):
            self.google_sheet_monitor_status = "未启用"
            self.updateGoogleSheetMonitorButton()
            return
        if self.google_sheet_monitor_thread is not None:
            return
        thread = GoogleSheetMonitorThread(
            self.google_sheet_monitor_settings,
            api_config=self.load_config(),
            parent=self,
        )
        thread.status.connect(self.onGoogleSheetMonitorStatus)
        thread.detected.connect(self.onGoogleSheetLinksDetected)
        thread.download_result.connect(self.onGoogleSheetDownloadResult)
        thread.queue_changed.connect(self.onGoogleSheetQueueChanged)
        thread.log.connect(self.appendLog)
        connect_thread_finished(thread, self, 'google_sheet_monitor_thread', self.onGoogleSheetMonitorFinished, pass_thread=True)
        self.google_sheet_monitor_thread = thread
        thread.set_project_dir(self.loaded_project_dir)
        self.google_sheet_monitor_status = "正在启动…"
        self.updateGoogleSheetMonitorButton()
        thread.start()

    def stopGoogleSheetMonitor(self, wait_ms=5000):
        thread = self.google_sheet_monitor_thread
        if thread is None:
            return True
        if thread_is_active(thread):
            thread.stop()
            if not thread.wait(wait_ms):
                return False
        if self.google_sheet_monitor_thread is thread:
            self.google_sheet_monitor_thread = None
        thread.deleteLater()
        return True

    def restartGoogleSheetMonitor(self):
        if not self.stopGoogleSheetMonitor():
            QMessageBox.warning(self.context.parent_widget, "表格监视器", "后台检查仍在停止，请稍后再试。")
            return
        self.google_sheet_monitor_status = "未启用"
        self.startGoogleSheetMonitor()

    def onGoogleSheetMonitorFinished(self, thread=None):
        thread = thread if thread is not None else self.sender()
        if self.google_sheet_monitor_thread is not thread:
            return
        if self.google_sheet_monitor_thread is thread:
            self.google_sheet_monitor_thread = None

    def onGoogleSheetMonitorStatus(self, status):
        self.google_sheet_monitor_status = str(status)
        self.updateGoogleSheetMonitorButton()

    def onGoogleSheetQueueChanged(self, count):
        self.google_sheet_monitor_pending = max(0, int(count))
        self.updateGoogleSheetMonitorButton()

    def onGoogleSheetLinksDetected(self, payload):
        count = int(payload.get("count", 0))
        self.google_sheet_monitor_unread += count
        if self.loaded_project_dir is None:
            message = "发现 {} 个新链接；尚未加载项目，已保存到待下载队列。".format(count)
        else:
            message = "发现 {} 个新链接，正在下载到当前项目。".format(count)
        self.appendLog(message)
        self.showDesktopNotification("Google 表格发现新链接", message)
        self.updateGoogleSheetMonitorButton()

    def onGoogleSheetDownloadResult(self, result):
        self.google_sheet_monitor_pending = int(result.get("pending", self.google_sheet_monitor_pending))
        if result.get("ok"):
            path = result.get("path", "")
            self.appendLog("表格新文件已下载：{}".format(path))
            if self.google_sheet_monitor_pending == 0:
                self.showDesktopNotification("Google 表格下载完成", "新链接文件已经放入当前项目目录。")
        else:
            error = result.get("error", "未知错误")
            self.appendLog("表格链接下载失败，已延后重试：{}".format(error))
            self.showDesktopNotification(
                "Google 表格下载失败",
                "链接仍保留在队列中，稍后自动重试：{}".format(error),
                critical=True,
            )
        self.updateGoogleSheetMonitorButton()

    def updateGoogleSheetMonitorButton(self):
        label = self.google_sheet_monitor_status
        if self.google_sheet_monitor_pending: label = f"待下载 {self.google_sheet_monitor_pending}"
        self.context.update_command('open', title=f"表格监视器…（{label}）", tooltip=self.google_sheet_monitor_status)

    def openGoogleSheetMonitor(self):
        self.google_sheet_monitor_unread = 0
        self.updateGoogleSheetMonitorButton()
        result = GoogleSheetMonitorDialog.get_settings(
            self.google_sheet_monitor_settings,
            self.google_sheet_monitor_status,
            self.context.parent_widget,
        )
        if result is None:
            return
        settings, reset_baseline = result
        previous_settings = self.google_sheet_monitor_settings
        if not self.stopGoogleSheetMonitor():
            QMessageBox.warning(self.context.parent_widget, "表格监视器", "后台检查仍在停止，请稍后再保存。")
            return
        self.google_sheet_monitor_settings = settings
        if reset_baseline:
            try:
                reset_monitor_baseline()
            except OSError as error:
                self.google_sheet_monitor_settings = previous_settings
                self.startGoogleSheetMonitor()
                QMessageBox.critical(self.context.parent_widget, "表格监视器", "无法重置监视记录：{}".format(error))
                return
        from model.AppDataStorage import application_storage
        storage = application_storage()
        # On volume loss exit must not save defaults or restart services just
        # because saving is impossible. Other lifecycle checks remain intact.
        if (not storage.protected or storage.guard.available) and not self.saveCurrentConfig():
            self.google_sheet_monitor_settings = previous_settings
            self.startGoogleSheetMonitor()
            return
        self.startGoogleSheetMonitor()
