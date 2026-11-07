from functools import partial
from uuid import uuid4

from qt_compat import QtCore
from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from model.AIProviderKeys import KEYS_CONFIG_KEY
from .service import run_operation, credential_identity


class ImageRemixPlugin(QtCore.QObject):
    plugin_id = 'image_remix'
    display_name = 'GPT 图片翻版'
    version = '0.1.0'
    required_api_version = 1

    def __init__(self):
        super().__init__()
        self.context = self.dialog = self.handle = None
        self.jobs = []
        self.message = '仅使用官方Batch；关闭窗口不取消云端任务，重启后继续检查。'
        self.stopping = False
        self.loaded = False
        self.poll = QtCore.QTimer(self)
        self.poll.setInterval(5 * 60 * 1000)
        self.poll.timeout.connect(self.check)
        self.watch = QtCore.QTimer(self)
        self.watch.setInterval(1000)
        self.watch.timeout.connect(self.reclaim)

    def register(self, context):
        self.context = context
        self.setParent(context.parent_widget)
        context.register_command(PluginCommand('open', 'GPT 图片翻版…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=23, tooltip='文字提示词批量生图，可选人物参考图；真实Batch与重启恢复'))
        from .settings import RemixSettingsPage
        context.register_settings_page(PluginSettingsPage('settings', self.display_name,
            lambda parent=None: RemixSettingsPage(context, parent), order=223))

    def start(self):
        self.stopping = False
        self.poll.start()
        QtCore.QTimer.singleShot(3000, self.check)

    def open_dialog(self):
        if self.dialog is None:
            from .ui import RemixDialog
            self.dialog = RemixDialog(self, self.context.parent_widget)
        self.dialog.show(); self.dialog.showNormal(); self.dialog.raise_(); self.dialog.activateWindow()
        self.check()
        return self.dialog

    def reclaim(self):
        handle = self.handle
        try:
            if handle is not None and callable(getattr(handle, 'isSettled', None)) and handle.isSettled():
                self.finished(handle)
        except (RuntimeError, KeyError):
            pass

    def busy(self):
        self.reclaim()
        return self.handle is not None

    def launch(self, action, **fields):
        if self.stopping or self.busy():
            return False
        config = self.context.load_config()
        pools = config.get(KEYS_CONFIG_KEY) or {}
        payload = {'root': str(self.context.storage.path), 'action': action,
                   'config': {KEYS_CONFIG_KEY: {'openai': pools.get('openai')}}, **fields}
        # Persist only a fingerprint in jobs. The host's encrypted key list is
        # consulted on each check; no plaintext credentials in plugin storage.
        handle = self.context.submit_background_task(title='图片翻版 · ' + {
            'create': '提交批次', 'check': '检查并收取', 'retry': '重试失败项',
            'cancel': '取消批次', 'delete': '删除记录', 'snapshot': '读取记录', 'bind': '关联原批次'}[action],
            runner=run_operation, payload=payload, kind='network', execution='parallel',
            resources=('image-remix-state',), retryable=False, dedup_key='image-remix-operation')
        from model.BackgroundTasks import BackgroundTaskHandle
        if not isinstance(handle, BackgroundTaskHandle):
            raise TypeError('图片翻版只接受宿主BackgroundTaskHandle，不接受插件自管QThread。')
        self.handle = handle
        handle.completed.connect(self.completed)
        handle.failed.connect(self.failed)
        handle.finished.connect(partial(self.finished, handle))
        self.watch.start()
        self.refresh()
        return True

    def submit(self, spec):
        if self.busy():
            return False
        config = self.context.load_config()
        try:
            credential = credential_identity(config)
        except (ValueError, RuntimeError, OSError) as error:
            raise ValueError('请在“程序设置 → GPT 图片翻版”添加或检查OpenAI Key并设为当前（与AI试用台共用）。') from error
        from .store import Store
        # Save the user's intent before joining the host queue. Even an exit
        # while queued cannot erase it. Image decoding stays in the worker.
        job = Store(self.context.storage.path).create(spec, credential, identifier=uuid4().hex,
                                                     defer_snapshot=True)
        self.jobs.insert(0, job)
        self.loaded = True
        return self.launch('create', spec=spec, credential=credential, id=job['id'])

    def check(self, *_args, manual=False):
        if self.stopping:
            return
        if self.loaded and not manual and not any(j['status'] not in ('done', 'partial', 'failed', 'cancelled', 'attention') for j in self.jobs):
            return
        try:
            self.launch('check', manual=manual)
        except Exception as error:
            self.failed(str(error))

    def completed(self, result):
        self.loaded = True
        self.jobs = result.get('jobs', [])
        messages = result.get('messages', [])
        self.message = '\n'.join(messages) if messages else '本地记录已更新。云端等待不占工作线程；每5分钟自动检查，也可手动检查。'
        if self.dialog is not None and not self.dialog.draft_loaded:
            self.dialog.load_draft(result.get('draft') or {})
        self.refresh()

    def failed(self, message):
        from model.SensitiveData import redact_sensitive_text
        self.message = redact_sensitive_text(message)
        self.refresh()

    def finished(self, handle):
        if self.handle is not handle:
            return
        # finished is emitted only after the host has released its worker.
        self.handle = None
        self.watch.stop()
        self.refresh()

    def refresh(self):
        if self.dialog is not None:
            self.dialog.refresh(self.jobs, self.message, self.handle is not None)

    def apply_settings(self, _config):
        return True

    def can_close(self):
        self.reclaim()
        if self.handle is not None:
            self.handle.requestInterruption()
            return False, '图片翻版正在保存／结束本次网络请求，请稍候再退出（读取超时最多60秒）；不会等待云端24小时。'
        return True, ''

    def stop(self):
        self.stopping = True
        self.poll.stop()
        allowed, _ = self.can_close()
        if not allowed:
            return False
        self.watch.stop()
        if self.dialog is not None:
            self.dialog.close()
        return True
