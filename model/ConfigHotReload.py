from model.AppDataStorage import data_io, data_file, data_directory
"""Safe config change detection and field-level merging, not full app restart."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import queue
import threading

from qt_compat import QtCore, QtWidgets


class _Missing:
    def __deepcopy__(self, memo):
        return self


MISSING = _Missing()


def _config_stamp(path):
    try:
        value=path.stat()
        return value.st_mtime_ns,value.st_size
    except OSError:return None


def _read_config(path):
    managed=data_file(path)
    if not managed.exists():return {}
    value=json.loads(managed.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict):raise ValueError('config.json 必须是对象')
    from .PrivateDataPaths import private_config
    return private_config(value)


def _probe_config(path,results,generation,known_stamp,pending):
    # No QObject, callbacks or private values are written to disk by this
    # daemon. A removed window can leave a slow removable-drive query safely.
    try:
        stamp=_config_stamp(path)
        value=_read_config(path) if pending or stamp != known_stamp else None
        digest=ConfigHotReload._digest(value) if value is not None else None
        results.put((generation,stamp,value,digest,None))
    except Exception as error:
        results.put((generation,None,None,None,error))


def merge_runtime_changes(memory_base, local, disk_base, external):
    """Apply only genuinely edited in-memory fields to the current disk file.

    Disjoint changes merge; simultaneous changes of the same leaf are conflicts.
    Unchanged stale controls never overwrite an externally edited configuration.
    """
    conflicts = []

    def merge(before, current, old_disk, new_disk, path):
        if current == before:
            return deepcopy(new_disk)
        if isinstance(before, dict) and isinstance(current, dict) and (isinstance(new_disk, dict) or new_disk is MISSING):
            result = deepcopy(new_disk) if isinstance(new_disk, dict) else {}
            old = old_disk if isinstance(old_disk, dict) else {}
            for key in set(before) | set(current):
                value = merge(before.get(key, MISSING), current.get(key, MISSING),
                              old.get(key, MISSING), result.get(key, MISSING), path+[str(key)])
                if value is MISSING:
                    result.pop(key, None)
                else:
                    result[key] = value
            return result
        if new_disk == old_disk or new_disk == current:
            return deepcopy(current)
        conflicts.append(".".join(path))
        return deepcopy(new_disk)

    merged = merge(memory_base, local, disk_base, external, [])
    return merged, sorted(conflicts)


class ConfigHotReload(QtCore.QObject):
    def __init__(self, path, snapshot, apply, log, parent=None):
        super().__init__(parent)
        self.path = data_file(path).path
        self.snapshot = snapshot
        self.apply = apply
        self.log = log
        self.observed = self.read()
        self.disk_base = deepcopy(self.observed)
        self.memory_base = deepcopy(snapshot())
        self.digest = self._digest(self.observed)
        self.pending = None
        self.pending_digest = None
        self.error_message = None
        self.stamp = self._stamp()
        self.retry_old = {}
        self.generation=0
        self.worker=None
        self.results=queue.SimpleQueue()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.request_poll)
        self.timer.start()

    def _stamp(self):
        return _config_stamp(self.path)

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def read(self):
        return _read_config(self.path)

    def _sync_applied_baseline(self, external, runtime):
        def sync(old, disk, memory):
            result = deepcopy(old) if isinstance(old, dict) else {}
            if not isinstance(disk, dict) or not isinstance(memory, dict):
                return result
            for key, value in disk.items():
                current = memory.get(key, MISSING)
                if current == value:
                    result[key] = deepcopy(value)
                elif isinstance(value, dict) and isinstance(current, dict):
                    result[key] = sync(result.get(key, {}), value, current)
            for key in set(result) - set(disk):
                if key not in memory:
                    result.pop(key, None)
            return result
        self.disk_base = sync(self.disk_base, external, runtime)

    def poll(self, force=False):
        """Explicit/manual synchronous check; timer uses request_poll instead."""
        self.generation+=1
        try:
            self.retry_deferred()
            stamp = self._stamp()
            if not force and stamp == self.stamp and self.pending is None:
                return
            value = self.read()
            digest = self._digest(value)
            self.consume(stamp,value,digest,force=force)
        except Exception as error:self.report_error(error)

    def retry_deferred(self):
        if self.retry_old and QtWidgets.QApplication.activeModalWidget() is None:
            previous=deepcopy(self.observed);previous.update(self.retry_old)
            retry=self.apply(previous,self.observed) or set()
            self.retry_old={key:self.retry_old[key] for key in retry if key in self.retry_old}
            self.memory_base=deepcopy(self.snapshot())
            self._sync_applied_baseline(self.observed,self.memory_base)

    def request_poll(self):
        try:
            self.retry_deferred()
            try:generation,stamp,value,digest,error=self.results.get_nowait()
            except queue.Empty:
                if self.worker is not None and self.worker.is_alive():return
                self.worker=threading.Thread(target=_probe_config,
                    args=(self.path,self.results,self.generation,self.stamp,self.pending is not None),
                    name='vc-config-watch',daemon=True)
                self.worker.start();self.timer.setInterval(100)
                return
            self.worker=None;self.timer.setInterval(1000)
            # An internal save/manual check may have superseded this read.
            if generation != self.generation:return
            if error is not None:raise error
            if value is not None:self.consume(stamp,value,digest)
        except Exception as error:self.report_error(error)

    def consume(self,stamp,value,digest,*,force=False):
        if digest == self.digest:
            self.stamp, self.pending, self.pending_digest = stamp, None, None
            if force:
                self.log('[配置热更新] 已检查配置文件，没有新的未处理变更。')
            return
        # Wait one poll to avoid partial writes and defer while any modal dialog is open.
        if not force and digest != self.pending_digest:
            self.pending, self.pending_digest = value, digest
            return
        if QtWidgets.QApplication.activeModalWidget() is not None:
            self.pending, self.pending_digest = value, digest
            return
        if not value.get("config_hot_reload", {}).get("enabled", True) and not force:
            self.pending = None
            self.stamp = stamp
            return
        old = deepcopy(self.observed)
        retry = self.apply(old, value) or set()
        self.retry_old = {key: old.get(key) for key in retry}
        # Snapshot after applying safe fields. Deferred fields retain their old runtime values.
        self.observed, self.digest, self.stamp = deepcopy(value), digest, stamp
        self.memory_base = deepcopy(self.snapshot())
        self._sync_applied_baseline(value, self.memory_base)
        # For the next local edit, retain the external conflict baseline where not applied.
        self.pending, self.pending_digest, self.error_message = None, None, None

    def report_error(self,error):
        text=f'[配置热更新] 未应用：{type(error).__name__}: {error}；现有运行配置保留。'
        if text != self.error_message:
            self.error_message=text;self.log(text)

    def merge_for_save(self, local):
        external = self.read()
        self.save_expected_digest = self._digest(external)
        return merge_runtime_changes(self.memory_base, local, self.disk_base, external)

    def check_before_commit(self):
        if self._digest(self.read()) != getattr(self, 'save_expected_digest', None):
            raise ValueError('保存期间配置又被外部修改，未覆盖原文件，请重试。')

    def note_written(self):
        self.generation+=1
        self.observed = self.read()
        self.disk_base = deepcopy(self.observed)
        self.memory_base = deepcopy(self.snapshot())
        self.digest, self.stamp = self._digest(self.observed), self._stamp()
        self.pending, self.pending_digest = None, None
