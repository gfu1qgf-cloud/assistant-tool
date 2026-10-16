"""Reap owned child processes asynchronously after a disposable UI releases them."""
from qt_compat import QtCore


class _ProcessReaper(QtCore.QObject):
    def __init__(self, process, grace_ms, kill):
        super().__init__(QtCore.QCoreApplication.instance())
        self.process = process
        process.setParent(self)
        process.finished.connect(self._finish)
        process.errorOccurred.connect(self._error)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._kill)
        self.timer.start(max(0, int(grace_ms)))
        if kill:
            process.kill()
        else:
            process.terminate()

    def _finish(self, *_args):
        self.timer.stop()
        self.deleteLater()

    def _error(self, _error):
        if self.process.state() == QtCore.QProcess.ProcessState.NotRunning:
            self._finish()

    def _kill(self):
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.process.kill()
        else:
            self._finish()


def retire_process(process, *, grace_ms=300, kill=False):
    """Detach old UI callbacks; retain ownership through native finished()."""
    try:
        process.disconnect()
    except TypeError:
        pass
    if process.state() == QtCore.QProcess.ProcessState.NotRunning:
        process.deleteLater()
        return
    _ProcessReaper(process, grace_ms, kill)
