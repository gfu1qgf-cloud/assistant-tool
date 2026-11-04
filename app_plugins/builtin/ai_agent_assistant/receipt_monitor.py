"""GUI-owned scheduler; networking and file I/O run only in host workers."""
from qt_compat import QtCore
from model.OfflineMode import is_offline
from .delivery import run_receipts
from .tasks import spreadsheet_id


class ReceiptMonitor(QtCore.QObject):
    def __init__(self, plugin):
        super().__init__(plugin)
        self.plugin = plugin
        self.handle = None
        self.running = False
        self.manual_notice = False
        self.status = '自动收取将在程序启动后运行'
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self.tick)

    def show_status(self, text):
        self.status = text
        dialog = self.plugin.dialog
        if dialog is not None: dialog.receipt_status.setText(text)

    def start(self):
        self.running = True
        self.timer.start(1500)

    def stop(self):
        self.running = False; self.timer.stop()
        if self.handle is not None:
            self.handle.requestInterruption()
            return False
        return True

    def tick(self):
        self.timer.setInterval(60000)
        if not self.running: return
        self.check()

    def download_authorization(self):
        # A new purpose never borrows another plugin's account. On first use,
        # reuse THIS plugin's explicitly selected Sheets account only if it
        # already has the host's Drive scope; explicit disabled/other selection wins.
        from model.GoogleAccounts import GoogleAccountManager, token_scopes
        manager = GoogleAccountManager()
        bound = manager.bound_account('ai_agent_assistant.downloads')
        if bound is None:
            selected = manager.bound_account('ai_agent_assistant.sheets')
            if selected and 'https://www.googleapis.com/auth/drive' in token_scopes(selected[1]['credentials']):
                manager.bind('ai_agent_assistant.downloads', selected[0])
        return self.plugin.context.prepare_google_account('downloads')

    def check(self, *, manual=False, ids=None, directories=None, force=False):
        if self.handle is not None:
            if manual: self.show_status('正在收取成品，进度可在主程序后台任务中查看。')
            return
        if self.plugin.handle is not None:
            self.show_status('等待当前填表操作结束，下次自动收取。'); return
        if is_offline(): self.show_status('离线模式：成品收取已暂停。'); return
        try:
            store = self.plugin.store
            if not store.file.exists(): self.show_status('尚无本地 Agent 任务。'); return
            settings = store.settings()
            if not manual and not settings.get('auto_download', True):
                self.show_status('自动收取已关闭；右键可立即检查。'); return
            jobs = store.jobs(limit=100000)
            if not jobs: self.show_status('尚无本地 Agent 任务。'); return
            destinations = {job['destination'] for job in jobs if job['destination']}
            if settings.get('url') and settings.get('sheet'):
                destinations.add(spreadsheet_id(settings['url']) + ':' + settings['sheet'])
            if not destinations: self.show_status('请先设置并保存目标表格。'); return
            sheets = self.plugin.context.prepare_google_account('sheets')
            try: drive = self.download_authorization()
            except ValueError: drive = None  # Public single-file downloads need no login.
            self.manual_notice = bool(manual)
            self.handle = self.plugin.context.submit_background_task(title='AI Agent · 检查并收取成品',
                runner=run_receipts, payload={'root': str(self.plugin.context.storage.path),
                    'sheets_authorization': sheets, 'drive_authorization': drive,
                    'ids': list(ids) if ids is not None else None,
                    'directories': list(directories) if directories is not None else None, 'force': bool(force)},
                kind='network', execution='parallel', retryable=False,
                resources=['agent-receipts:' + str(store.root.path).casefold(),
                           *['agent-sheet:' + value.split(':', 1)[0] for value in sorted(destinations)]])
            self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed)
            self.handle.progressChanged.connect(self.progress); self.handle.finished.connect(self.finished)
            self.show_status('后台检查成品中；可关闭助手窗口。')
        except Exception as error:
            self.show_status('成品收取待处理：' + str(error))

    def completed(self, result):
        self.show_status(result['message'])
        if result.get('task_badges') is not None: self.plugin.notices.apply_snapshot(result['task_badges'])
        if result.get('downloaded') or self.manual_notice:
            self.plugin.context.log(result['message'])
        if not result.get('task_badges', {}).get('notify_files'): self.notify_manual(result['message'])
        if self.plugin.dialog: self.plugin.dialog.reload()

    def failed(self, message):
        self.show_status('成品收取稍后自动重试：' + str(message))
        self.notify_manual(self.status)
        self.plugin.notices.refresh()
    def notify_manual(self, message):
        dialog = self.plugin.dialog
        notify = getattr(self.plugin.context, 'notify', None)
        if self.manual_notice and notify and not (dialog and dialog.isVisible()):
            try: notify('AI Agent 成品', message)
            except Exception as error: self.plugin.context.log('成品检查通知失败：' + str(error))
    def progress(self, message, _percent): self.show_status(message)
    def finished(self):
        if self.sender() is not self.handle: return
        self.handle = None
        self.manual_notice = False
        if self.plugin.dialog: self.plugin.dialog.reload()
