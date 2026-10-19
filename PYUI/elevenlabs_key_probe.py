"""Cancelable read-only inspection; Qt's pool owns native thread lifetime."""
import threading
from uuid import uuid4
from qt_compat import QtCore

_ACTIVE = {}


class _Signals(QtCore.QObject):
    result = QtCore.pyqtSignal(str, int, object)
    finished = QtCore.pyqtSignal(str)


def _release(identifier):
    _ACTIVE.pop(identifier, None)


class _Probe(QtCore.QRunnable):
    def __init__(self, rows):
        super().__init__()
        self.identifier = uuid4().hex
        self.rows = rows
        self.cancel = threading.Event()
        self.signals = _Signals()
        self.signals.finished.connect(_release)

    def run(self):
        from model.ApiKeyHelper import api_key_id
        from model.ElevenLabsPermissions import inspect_key
        try:
            for key, epoch in self.rows:
                if self.cancel.is_set():
                    break
                try:
                    result = inspect_key(key)
                except Exception:
                    # Never put credentials or raw API exception bodies in UI/logs.
                    result = {'permission_probe_status': 'network_error'}
                if not self.cancel.is_set():
                    self.signals.result.emit(api_key_id(key), epoch, result)
        finally:
            self.signals.finished.emit(self.identifier)


class KeyProbeController(QtCore.QObject):
    result = QtCore.pyqtSignal(str, object)
    busyChanged = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pending = {}
        self.epochs = {}
        self.worker = None
        self.closing = False

    @property
    def busy(self):
        return self.worker is not None or bool(self.pending)

    def enqueue(self, keys):
        from model.ApiKeyHelper import api_key_id, normalize_api_keys
        if self.closing:
            return
        running = {api_key_id(key): epoch for key, epoch in self.worker.rows} if self.worker else {}
        for key in normalize_api_keys(keys):
            identifier = api_key_id(key)
            epoch = self.epochs.get(identifier, 0)
            if running.get(identifier) != epoch:
                self.pending[identifier] = (key, epoch)
        if self.worker is None and self.pending:
            self.busyChanged.emit(True)
            self._start()

    def invalidate(self, identifiers):
        for identifier in identifiers:
            self.epochs[identifier] = self.epochs.get(identifier, 0) + 1
            self.pending.pop(identifier, None)

    def _start(self):
        worker = _Probe(list(self.pending.values()))
        self.pending.clear()
        self.worker = worker
        _ACTIVE[worker.identifier] = worker
        worker.signals.result.connect(self._result)
        worker.signals.finished.connect(self._finished)
        QtCore.QThreadPool.globalInstance().start(worker)

    def _result(self, identifier, epoch, updates):
        if not self.closing and self.epochs.get(identifier, 0) == epoch:
            self.result.emit(identifier, updates)

    def _finished(self, identifier):
        if self.worker is None or self.worker.identifier != identifier:
            return
        self.worker = None
        if self.pending and not self.closing:
            self._start()
        else:
            self.busyChanged.emit(False)

    def stop(self):
        self.closing = True
        self.pending.clear()
        if self.worker is not None:
            self.worker.cancel.set()
