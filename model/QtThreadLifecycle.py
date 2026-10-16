"""GUI-owned QThread completion: captured identity, native join, safe cleanup.

Use connect_thread_finished instead of reading a mutable worker field from an
unqualified finished callback. A completed/result signal is not a lifetime end.
"""
import logging
from PyQt6 import sip
from qt_compat import QtCore


def thread_is_active(thread):
    if thread is None:
        return False
    if isinstance(thread, QtCore.QThread):
        if sip.isdeleted(thread):
            return False
        if QtCore.QThread.currentThread() is thread:
            return thread.isRunning()
        # isRunning() may be false before native thread-local cleanup completes.
        return not thread.wait(0)
    return thread.isRunning()  # Host background handles and test doubles.


class _FinishedGate(QtCore.QObject):
    def __init__(self, thread, owner, attribute, callback, pass_thread, parent):
        super().__init__(parent)
        self.worker = thread
        self.owner = owner
        self.attribute = attribute
        self.callback = callback
        self.pass_thread = pass_thread
        self.delivered = False

    @QtCore.pyqtSlot()
    def deliver(self):
        if self.delivered:
            return
        worker = self.worker
        if sip.isdeleted(worker):
            self._release()
            return
        if not worker.wait(0):
            # Poll native cleanup without freezing the GUI or terminating it.
            QtCore.QTimer.singleShot(10, self.deliver)
            return
        self.delivered = True
        try:
            if self.attribute is None or getattr(self.owner, self.attribute, None) is worker:
                if self.pass_thread:
                    self.callback(worker)
                else:
                    self.callback()
        except Exception:
            # Exceptions escaping a PyQt slot can abort the whole process.
            logging.getLogger('assistant_tool').exception(
                '[线程生命周期] %s.%s 结束处理失败', type(self.owner).__name__, self.attribute)
        finally:
            if self.attribute is not None and getattr(self.owner, self.attribute, None) is worker:
                setattr(self.owner, self.attribute, None)
            if not sip.isdeleted(worker):
                worker.deleteLater()
            self._release()

    def _release(self):
        self.delivered = True
        self.owner = self.callback = self.worker = None
        self.deleteLater()


def connect_thread_finished(thread, owner, attribute, callback, *, pass_thread=False):
    app = QtCore.QCoreApplication.instance()
    if app is None or QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError('Qt 线程结束处理必须在主线程注册。')
    if not isinstance(thread, QtCore.QThread):
        raise TypeError('此接口只用于 QThread；主程序后台任务句柄保持其原有信号接口。')
    parent = owner if isinstance(owner, QtCore.QObject) else thread.parent() or app
    gate = _FinishedGate(thread, owner, attribute, callback, pass_thread, parent)
    thread.finished.connect(gate.deliver, QtCore.Qt.ConnectionType.QueuedConnection)
    return gate
