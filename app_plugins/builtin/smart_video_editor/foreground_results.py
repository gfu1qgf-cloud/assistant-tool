"""Qt-main-thread foreground presentation, serialized after worker completion."""
import logging
from qt_compat import QtCore, QtWidgets


class ForegroundResults(QtCore.QObject):
    def __init__(self, parent, accepting, blocked):
        super().__init__(parent)
        self.accepting, self.blocked = accepting, blocked
        self.queue, self.showing = [], False
        self.stopped = False
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._drain)

    def enqueue(self, key, callback):
        if self.stopped or not self.accepting():
            return
        if not any(item[0] == key for item in self.queue): self.queue.append((key, callback))
        self.timer.start(0)

    def resume(self):
        self.stopped = False

    def stop(self):
        self.stopped = True
        self.timer.stop()
        self.queue.clear()  # Pending host result handlers remain available.

    def show(self, key, callback):
        if QtCore.QThread.currentThread() != self.thread():
            raise RuntimeError("视频结果只能在 Qt 主线程显示。")
        if self.stopped or not self.accepting() or self.showing or self.blocked() or QtWidgets.QApplication.activeModalWidget():
            return False
        self.queue[:] = [item for item in self.queue if item[0] != key]
        self.showing = True
        try:
            return callback()
        finally:
            self.showing = False
            if self.queue: self.timer.start(0)

    def _drain(self):
        if not self.accepting():
            # The application's exit gate can be reopened after an aborted
            # close. Suppress queued windows, but do not disable this plugin.
            self.queue.clear(); return
        if not self.queue: return
        if self.showing or self.blocked() or QtWidgets.QApplication.activeModalWidget():
            self.timer.start(200); return
        key, callback = self.queue[0]
        try:
            callback()  # Host handle clears pending only after successful opening.
        except Exception:
            logging.getLogger(__name__).exception("自动打开视频结果失败；保留后台结果入口")
        # Never repeatedly reopen a failed/canceled presentation.
        self.queue[:] = [item for item in self.queue if item[0] != key]
        if self.queue: self.timer.start(0)
