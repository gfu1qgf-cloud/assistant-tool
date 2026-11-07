"""GUI-only handle ownership: exact callbacks plus terminal-state recovery."""
from functools import partial
from qt_compat import QtCore


def settled(handle):
    if handle is None: return False
    try:
        check = getattr(handle, 'isSettled', None)
        if callable(check): return check() is True
        # Compatibility with older hosts. Never guess an unknown/running job
        # finished, nor release it while result callbacks are being delivered.
        running = getattr(handle, 'isRunning', None)
        manager = getattr(handle, 'manager', None)
        idle = getattr(manager, 'is_settled', None)
        return callable(running) and running() is False and callable(idle) and idle() is True
    except (RuntimeError, KeyError):
        return False


def reclaim(owner, *, closing=False):
    current = owner.handle
    if not settled(current): return False
    owner.finished(current, recovered=True, closing=closing)
    return owner.handle is not current


def watch_finished(owner, handle):
    if isinstance(handle, QtCore.QThread):
        raise TypeError('Agent生命周期只接受主程序QObject任务句柄，不接受原生QThread。')
    # Capture the handle rather than relying on QObject.sender() identity.
    handle.finished.connect(partial(owner.finished, handle))
    timer = getattr(owner, '_handle_watch', None)
    if timer is None:
        timer = owner._handle_watch = QtCore.QTimer(owner)
        timer.setInterval(1000)
        def check():
            reclaim(owner)
            if owner.handle is None: timer.stop()
        timer.timeout.connect(check)
    timer.start()
    # The timer also covers a task that finished during a re-entrant submit.


def stop_watch(owner):
    timer = getattr(owner, '_handle_watch', None)
    if timer is not None and owner.handle is None: timer.stop()
