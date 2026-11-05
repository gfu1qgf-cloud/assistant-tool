"""GUI projection and commands; private SQLite is read only in host workers."""
from pathlib import Path

from qt_compat import QtCore, QtGui
from app_plugins.api import TaskBadge
from .notice_data import directory_key, run_notice_snapshot


class ReceiptNotices(QtCore.QObject):
    def __init__(self, plugin):
        super().__init__(plugin)
        self.plugin, self.handle, self.items = plugin, None, {}
        self.filtered = self.pending = False
        self.pending_announce = False
        self.generation = 0
    def refresh(self, *, seen=None, unseen=None, announce=True):
        context = self.plugin.context
        if not hasattr(context, 'set_task_badges'): return
        if self.handle is not None:
            if seen or unseen: raise ValueError('成品标记正在刷新，请稍后再修改查看标记。')
            self.pending = True; self.pending_announce |= bool(announce); return
        if not self.plugin.store.file.exists(): self.publish(); return
        try:
            self.handle = context.submit_background_task(title='AI Agent · 更新本地成品标记',
                runner=run_notice_snapshot, payload={'root': str(context.storage.path), 'seen': list(seen or []),
                    'unseen': list(unseen or []), 'ui_generation': self.generation, 'announce': bool(announce),
                    'transient': not announce and not bool(seen or unseen)},
                kind='network', execution='parallel', resources=['agent-notices:' + str(context.storage.path).casefold()], retryable=False)
        except Exception as error:
            self.failed(error); return
        self.handle.completed.connect(self.apply_snapshot); self.handle.failed.connect(self.failed); self.handle.finished.connect(self.finished)
    def apply_snapshot(self, snapshot):
        if 'ui_generation' in snapshot and snapshot['ui_generation'] != self.generation:
            self.pending = True; self.pending_announce = True; return
        if not snapshot.get('transient'):self.generation += 1
        self.items = {directory_key(item['directory']): item for item in snapshot.get('items', [])}
        try: self.publish()
        except Exception as error: self.failed(error)
        if snapshot.get('notify_files'):
            notify = getattr(self.plugin.context, 'notify', None)
            if notify:
                try: notify('Agent 新成品', f'{snapshot["notify_tasks"]} 个任务新增 {snapshot["notify_files"]} 个视频，已放入原任务的 result 目录。')
                except Exception as error: self.failed(error)
    def publish(self):
        publish = getattr(self.plugin.context, 'set_task_badges', None)
        if publish:
            publish({item['directory']: TaskBadge(item['text'], item['tone'], item['tooltip'], bool(item['unread']))
                     for item in self.items.values()})
    def selected(self, rows):
        getter = getattr(self.plugin.context, 'task_badge_keys', None)
        keys = getter(rows) if getter else []
        return [self.items[key] for key in dict.fromkeys(keys) if key in self.items]
    def mark_seen(self, rows):
        versions = [version for item in self.selected(rows) for version in item['versions']]
        if versions: self.refresh(seen=versions)
    def mark_unseen(self, rows):
        versions = [version for item in self.selected(rows) for version in item.get('seen_versions', [])]
        if versions: self.refresh(unseen=versions)
    def open_result(self, rows):
        items = self.selected(rows)
        if len(items) != 1: raise ValueError('请只选择一条有成品标记的任务。')
        directory = Path(items[0]['directory'])/'result'
        if not directory.is_dir(): raise ValueError('原任务成品目录不可用。')
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))): raise ValueError('无法打开原任务成品目录。')
    def set_filter(self, _rows, checked):
        self.filtered = bool(checked)
        self.plugin.context.set_task_badge_filter(self.filtered)
        updater = getattr(self.plugin.context, 'update_command', None)
        if updater: updater('received_filter', checked=self.filtered)
    def failed(self, message): self.plugin.context.log('成品标记刷新失败，原标记保留：' + str(message))
    def finished(self):
        if self.sender() is not self.handle: return
        self.handle = None
        if self.pending:
            announce=self.pending_announce
            self.pending=self.pending_announce=False
            self.refresh(announce=announce)
    def stop(self):
        self.pending = self.pending_announce = False
        if self.handle is not None: self.handle.requestInterruption(); return False
        publisher = getattr(self.plugin.context, 'set_task_badges', None)
        if publisher: publisher({})
        filterer = getattr(self.plugin.context, 'set_task_badge_filter', None)
        if filterer: filterer(False)
        return True
