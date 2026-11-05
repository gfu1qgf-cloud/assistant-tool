"""GUI-owned scheduler; networking and file I/O run only in host workers."""
from qt_compat import QtCore
from model.OfflineMode import is_offline
from .delivery import run_receipts,RECEIPT_PROGRESS_PREFIX
from .tasks import spreadsheet_id

DEFAULT_POLL_MINUTES = 30


def poll_minutes(settings):
    if not isinstance(settings, dict): return DEFAULT_POLL_MINUTES
    value = settings.get('poll_minutes', DEFAULT_POLL_MINUTES)
    try:
        if isinstance(value, bool): return DEFAULT_POLL_MINUTES
        return max(1, min(1440, int(value)))
    except (ValueError, TypeError, OverflowError): return DEFAULT_POLL_MINUTES


class ReceiptMonitor(QtCore.QObject):
    def __init__(self, plugin):
        super().__init__(plugin)
        self.plugin = plugin
        self.handle = None
        self.running = False
        self.manual_notice = False
        self._continue_ids, self._continue_manual = [], False
        self.status = '自动收取将在程序启动后运行'
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(DEFAULT_POLL_MINUTES * 60000)
        self.timer.timeout.connect(self.tick)
        self.badge_timer=QtCore.QTimer(self)
        self.badge_timer.setSingleShot(True);self.badge_timer.setInterval(500)
        self.badge_timer.timeout.connect(self.refresh_progress_badges)

    def show_status(self, text):
        self.status = text
        dialog = self.plugin.dialog
        if dialog is not None:
            dialog.receipt_status.setText(text)
            if hasattr(dialog, 'image_page'): dialog.image_page.status.setText(text)

    def start(self):
        self.running = True
        self._continue_ids, self._continue_manual = [], False
        self.configure()

    def configure(self):
        self.timer.stop()
        if not self.running or self.handle is not None: return
        try: settings = self.plugin.store.settings()
        except Exception: settings = {}  # Retry unavailable storage slowly, not every second.
        if not isinstance(settings, dict): settings = {}
        if self._continue_ids and (settings.get('auto_download', True) or self._continue_manual):
            self.timer.start(2000)
        elif settings.get('auto_download', True): self.timer.start(poll_minutes(settings) * 60000)

    def stop(self):
        self.running = False; self.timer.stop()
        self.badge_timer.stop()
        self._continue_ids, self._continue_manual = [], False
        if self.handle is not None:
            self.handle.requestInterruption()
            return False
        return True

    def tick(self):
        if not self.running: return
        ids, manual = self._continue_ids, self._continue_manual
        self._continue_ids, self._continue_manual = [], False
        self.check(manual=manual, ids=ids or None)

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
        self.timer.stop()
        self._continue_ids, self._continue_manual = [], False
        try: return self._check(manual=manual, ids=ids, directories=directories, force=force)
        finally:
            if self.handle is None: self.configure()

    def _check(self, *, manual=False, ids=None, directories=None, force=False):
        if self.handle is not None:
            if manual: self.show_status('正在收取成品，进度可在主程序后台任务中查看。')
            return
        if self.plugin.handle is not None:
            self.show_status('当前填表操作尚未结束，请结束后再次手动检查。' if manual else '等待当前填表操作结束，下次巡查再检查。'); return
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
            if settings.get('image_url'):
                destinations.add(spreadsheet_id(settings['image_url']) + ':' + settings.get('image_sheet', '图片转动画'))
            if not destinations: self.show_status('请先设置并保存目标表格。'); return
            sheets = self.plugin.context.prepare_google_account('sheets')
            try: drive = self.download_authorization()
            except ValueError: drive = None  # Public single-file downloads need no login.
            self.manual_notice = bool(manual)
            self.handle = self.plugin.context.submit_background_task(title='AI Agent · 检查并收取成品',
                runner=run_receipts, payload={'root': str(self.plugin.context.storage.path),
                    'ui_generation': self.plugin.notices.generation,
                    'allow_incomplete': bool(manual),
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
        self.badge_timer.stop()
        self._continue_ids = list(dict.fromkeys(result.get('continue_ids') or [])) if self.running else []
        self._continue_manual = bool(self._continue_ids and self.manual_notice)
        self.show_status(result['message'])
        if result.get('task_badges') is not None:
            self.plugin.notices.apply_snapshot(result['task_badges'])
            if self.plugin.notices.pending and self.plugin.notices.handle is None: self.plugin.notices.refresh()
        if result.get('downloaded') or self.manual_notice:
            self.plugin.context.log(result['message'])
        if not self._continue_ids and not result.get('task_badges', {}).get('notify_files'): self.notify_manual(result['message'])
        if self.plugin.dialog: self.plugin.dialog.reload()

    def failed(self, message):
        try: automatic = self.running and self.plugin.store.settings().get('auto_download',True)
        except Exception: automatic = False
        hint = '下次巡查会再检查，也可手动重试。' if automatic else '可右键手动检查或重试下载。'
        self.show_status('成品收取失败：' + str(message) + '\n' + hint)
        self.notify_manual(self.status)
        self.plugin.notices.refresh()
    def notify_manual(self, message):
        dialog = self.plugin.dialog
        notify = getattr(self.plugin.context, 'notify', None)
        if self.manual_notice and notify and not (dialog and dialog.isVisible()):
            try: notify('AI Agent 成品', message)
            except Exception as error: self.plugin.context.log('成品检查通知失败：' + str(error))
    def progress(self, message, _percent):
        self.show_status(message)
        if message.startswith(RECEIPT_PROGRESS_PREFIX) and not self.badge_timer.isActive():
            self.badge_timer.start()
    def refresh_progress_badges(self):
        if self.handle is not None:self.plugin.notices.refresh(announce=False)
    def finished(self):
        if self.sender() is not self.handle: return
        self.badge_timer.stop()
        self.handle = None
        self.manual_notice = False
        if self.plugin.dialog: self.plugin.dialog.reload()
        self.configure()
