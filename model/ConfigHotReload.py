"""Safe config change detection and field-level merging, not full app restart."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from qt_compat import QtCore, QtWidgets


class _Missing:
    def __deepcopy__(self, memo):
        return self


MISSING = _Missing()


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
        self.path = Path(path)
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
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def _stamp(self):
        try:
            value = self.path.stat()
            return value.st_mtime_ns, value.st_size
        except OSError:
            return None

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def read(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("config.json 必须是对象")
        return value

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
        try:
            if self.retry_old and QtWidgets.QApplication.activeModalWidget() is None:
                previous = deepcopy(self.observed)
                previous.update(self.retry_old)
                retry = self.apply(previous, self.observed) or set()
                self.retry_old = {key: self.retry_old[key] for key in retry if key in self.retry_old}
                self.memory_base = deepcopy(self.snapshot())
                self._sync_applied_baseline(self.observed, self.memory_base)
            stamp = self._stamp()
            if not force and stamp == self.stamp and self.pending is None:
                return
            value = self.read()
            digest = self._digest(value)
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
        except Exception as error:
            text = f"[配置热更新] 未应用：{type(error).__name__}: {error}；现有运行配置保留。"
            if text != self.error_message:
                self.error_message = text
                self.log(text)

    def merge_for_save(self, local):
        external = self.read()
        self.save_expected_digest = self._digest(external)
        return merge_runtime_changes(self.memory_base, local, self.disk_base, external)

    def check_before_commit(self):
        if self._digest(self.read()) != getattr(self, 'save_expected_digest', None):
            raise ValueError('保存期间配置又被外部修改，未覆盖原文件，请重试。')

    def note_written(self):
        self.observed = self.read()
        self.disk_base = deepcopy(self.observed)
        self.memory_base = deepcopy(self.snapshot())
        self.digest, self.stamp = self._digest(self.observed), self._stamp()
        self.pending, self.pending_digest = None, None
