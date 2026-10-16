"""Session-only opt-in shutdown after the host queue has safely settled."""
import ctypes
import logging
import math
import os
from pathlib import Path
import subprocess
import time

from qt_compat import QtCore
from model.BackgroundTasks import ACTIVE


class CompletionShutdown(QtCore.QObject):
    changed = QtCore.pyqtSignal()

    def __init__(self, manager, close_application, parent=None, *, ready_check=None,
                 clock=time.monotonic, countdown=60):
        super().__init__(parent)
        self.manager, self.close_application = manager, close_application
        self.ready_check = ready_check or (lambda: '')
        self.clock, self.countdown = clock, int(countdown)
        if not 10 <= self.countdown <= 600:
            raise ValueError('关机倒计时须为10–600秒。')
        self.armed = self.requested = self.finishing = False
        self.allow_failed = False
        self.deadline = None
        self.status = '未启用；仅本次启动有效。'
        self._known, self._watched = set(), {}
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        manager.changed.connect(self.refresh)
        manager.idle.connect(self.refresh)

    @property
    def remaining(self):
        return max(0, math.ceil(self.deadline-self.clock())) if self.deadline is not None else 0

    def _status(self, message):
        if self.status != message:
            self.status = message
            self.changed.emit()

    def arm(self, *, allow_failed=False):
        if self.requested or self.finishing:
            raise RuntimeError('程序已进入关机退出流程。')
        if type(allow_failed) is not bool:
            raise TypeError('关机完成条件必须明确选择。')
        states = self.manager.completion_states()
        self._known = set(states)
        self._watched = {key: status for key, status in states.items() if status in ACTIVE}
        self.allow_failed, self.armed, self.deadline = allow_failed, True, None
        self.timer.start()
        self.changed.emit()
        self.refresh()

    def cancel(self, message='已取消自动关机；后台任务继续执行。'):
        if self.requested:
            return
        self.timer.stop()
        self.armed, self.deadline = False, None
        self._watched.clear()
        self.status = message
        self.changed.emit()

    def refresh(self, *_args):
        if not self.armed or self.finishing:
            return
        try:
            self._evaluate()
        except Exception as error:
            logging.getLogger('assistant_tool').exception('自动关机检查失败，已取消关机')
            self.cancel(f'自动关机已取消：{type(error).__name__}: {error}')

    def _evaluate(self):
        added = False
        for key, status in self.manager.completion_states().items():
            if key not in self._known:
                self._known.add(key)
                self._watched[key] = status
                added = True
            elif key in self._watched:
                self._watched[key] = status
        if added:
            self.deadline = None
        if not self.allow_failed and any(status not in ACTIVE | {'succeeded'}
                                          for status in self._watched.values()):
            self.cancel('已有任务失败或取消，自动关机已取消；请核对结果后重新开启。')
            return
        if not self._watched:
            self._status('已开启，等待提交后台任务；空队列不会直接关机。')
            return
        if not self.manager.is_settled():
            self.deadline = None
            self._status('已开启，等待全部后台任务及结果回调收尾。')
            return
        if any(status in ACTIVE for status in self._watched.values()):
            self.deadline = None
            self._status('正在等待任务状态确认，暂不关机。')
            return
        reason = self.ready_check()
        if reason:
            self.deadline = None
            self._status('等待安全退出：' + str(reason))
            return
        if self.deadline is None:
            self.deadline = self.clock() + self.countdown
        seconds = self.remaining
        self._status(f'全部后台任务已结束，{seconds}秒后保存并退出程序，然后关机。')
        if seconds == 0 and self.armed and self.deadline is not None and self.remaining == 0:
            self._finish()

    def _finish(self):
        self.finishing, self.armed, self.deadline = True, False, None
        self.timer.stop()
        self._status('正在保存记录并安全退出；未强制结束任何程序。')
        accepting = self.manager.is_accepting()
        self.manager.stop_accepting()
        try:
            if not self.manager.is_settled():
                raise RuntimeError('又有任务开始执行，请重新开启自动关机。')
            if self.manager.flush_history() is False:
                raise OSError('后台任务记录保存失败，未执行关机。')
            if not self.close_application():
                raise RuntimeError('程序未能安全退出，未执行关机；请查看提示。')
            # main() executes the OS action only AFTER plugins, Qt, log files
            # and the private VC storage session have been cleanly released.
            self.requested = True
        except Exception as error:
            if accepting:
                self.manager.resume_accepting()
            self.cancel(str(error))
        finally:
            self.finishing = False
            self.changed.emit()


def request_windows_shutdown():
    """Never call in tests: mock subprocess.run. No shell, /f, or timed /t.

    Microsoft documents that /t > 0 implies /f, so the cancelable countdown
    belongs to our UI. /s /t 0 lets other applications protect unsaved work.
    https://learn.microsoft.com/windows-server/administration/windows-commands/shutdown
    """
    if os.name != 'nt':
        raise OSError('自动关机目前只支持Windows。')
    system = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system))
    if not size or size >= len(system):
        raise OSError('无法确定Windows系统目录，未执行关机。')
    executable = Path(system.value) / 'shutdown.exe'
    result = subprocess.run([str(executable), '/s', '/t', '0'], shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError(f'Windows未接受关机请求（代码{result.returncode}）；可能缺少权限或已有系统关机任务。')


def report_shutdown_failure(error):
    # QApplication is already destroyed; do not recreate Qt or private logs.
    message = '后台任务已结束，辅助工具已安全退出，但自动关机失败。\n' + str(error)
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, message, '自动关机失败', 0x10)

