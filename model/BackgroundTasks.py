"""Host-owned cooperative jobs. Only the GUI thread mutates the queue/UI state."""

from model.AppDataStorage import data_directory, data_file, data_io, application_storage

import copy
import dataclasses
import json
import logging
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path
from uuid import uuid4

from qt_compat import QtCore
from model.SensitiveData import redact_sensitive_text
from model.BackgroundTaskSettings import MAX_PARALLEL


ACTIVE = {"queued", "running", "canceling"}
TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}


class BackgroundTaskCanceled(Exception):
    pass


class BackgroundTaskResultError(RuntimeError):
    """A failed/partial job still has usable results that must reach its owner."""
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def directory_resource(path):
    """Use the same canonical resource key for every writer of a directory."""
    return "directory:" + str(Path(path).expanduser().resolve()).casefold()


def directory_tree_resource(path):
    """For recursive writers; ordinary render output locks do not cover children."""
    return "directory-tree:" + str(Path(path).expanduser().resolve()).casefold()


def resources_conflict(left, right):
    """Directory writers also conflict with writers of descendant directories."""
    if set(left) & set(right):
        return True
    def directories(keys):
        return [(key.split(":", 1)[1].replace("\\", "/").rstrip("/") + "/",
                 key.startswith("directory-tree:")) for key in keys
                if key.startswith(("directory:", "directory-tree:"))]
    return any((x == y or recursive_x and y.startswith(x) or recursive_y and x.startswith(y))
               for x, recursive_x in directories(left) for y, recursive_y in directories(right))


class TaskCancellationToken:
    """Read-only Event interface for existing media engines; no Qt objects."""
    def __init__(self, context):
        self.context = context

    def is_set(self):
        return self.context.is_canceled()


class TaskRunContext:
    def __init__(self, canceled, report, log, item_report=None):
        self._canceled = canceled
        self._report = report
        self._log = log
        self._item_report = item_report

    def is_canceled(self):
        return self._canceled.is_set()

    def check_canceled(self):
        if self.is_canceled():
            raise BackgroundTaskCanceled("操作已取消。")
        from model.AppDataStorage import application_storage
        storage = application_storage()
        if storage.protected: storage.check()

    def report(self, message, percent=None):
        self.check_canceled()
        if percent is not None:
            # Validate on the worker side: a bad plugin progress value must not
            # raise out of a Qt GUI slot and terminate the whole application.
            percent = max(0, min(100, int(percent)))
        self._report(str(message), percent)

    def log(self, message):
        self._log(str(message))

    def report_item(self, identifier, percent):
        if self._item_report is not None:
            self._item_report(str(identifier), max(0, min(100, int(percent))))


class BackgroundTaskHandle(QtCore.QObject):
    """Observable job handle, not a thread owned by a plugin window."""
    progressChanged = QtCore.pyqtSignal(str, object)
    itemProgress = QtCore.pyqtSignal(str, int)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()

    def __init__(self, manager, task_id):
        super().__init__(manager)
        self.manager = manager
        self.task_id = task_id
        self._result_handler = None
        self._result_pending = False

    def set_result_handler(self, callback, *, pending=False):
        self.manager._assert_main_thread()
        self._result_handler = callback
        self._result_pending = bool(callback and pending)
        self.manager.changed.emit(self.task_id)

    def has_result_handler(self):
        return self._result_handler is not None

    def has_pending_result(self):
        return self._result_pending and self.has_result_handler()

    def show_result(self):
        self.manager._assert_main_thread()
        if self._result_handler is None:
            return False
        # Returning False means the owner deferred opening (e.g. another
        # review is open). Keep its pending card instead of hiding the result.
        if self._result_handler() is not False:
            self._result_pending = False
            self.manager.changed.emit(self.task_id)
        return True

    def isRunning(self):
        return self.manager.get(self.task_id)["status"] in ACTIVE

    def requestInterruption(self):
        self.manager.cancel(self.task_id)


class _TaskWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str, str, object)
    itemProgress = QtCore.pyqtSignal(str, str, int)
    outcome = QtCore.pyqtSignal(str, object)
    message = QtCore.pyqtSignal(str, str)

    def __init__(self, task_id, runner, payload, canceled, parent):
        super().__init__(parent)
        self.task_id = task_id
        self.runner = runner
        self.payload = payload
        self.canceled = canceled

    def run(self):
        context = TaskRunContext(
            self.canceled,
            lambda text, percent: self.progress.emit(self.task_id, text, percent),
            lambda text: self.message.emit(self.task_id, text),
            lambda identifier, percent: self.itemProgress.emit(self.task_id, identifier, percent),
        )
        try:
            context.check_canceled()
            result = self.runner(self.payload, context)
            if isinstance(result, dict) and "paths" in result:
                paths = result["paths"]
                if not isinstance(paths, (list, tuple)):
                    raise TypeError("后台任务结果 paths 必须为路径列表。")
                result = {**result, "paths": [str(path) for path in paths]}
            outcome = {"status": "canceled" if context.is_canceled() else "succeeded", "result": result}
        except BackgroundTaskCanceled:
            outcome = {"status": "canceled", "error": "操作已取消。"}
        except BackgroundTaskResultError as error:
            outcome = {"status": "canceled" if context.is_canceled() else "failed", "result": error.result,
                       "error": redact_sensitive_text(str(error)),
                       "traceback": redact_sensitive_text(traceback.format_exc())}
        except Exception as error:
            outcome = {
                "status": "canceled" if context.is_canceled() else "failed",
                "error": redact_sensitive_text(f"{type(error).__name__}: {error}"),
                "traceback": redact_sensitive_text(traceback.format_exc()),
            }
        self.outcome.emit(self.task_id, outcome)


class BackgroundTaskManager(QtCore.QObject):
    changed = QtCore.pyqtSignal(str)
    idle = QtCore.pyqtSignal()

    def __init__(self, parent=None, history_path=None, logger=None, max_parallel=3,
                 kind_limits=None):
        super().__init__(parent)
        self.max_parallel = max(1, min(MAX_PARALLEL, int(max_parallel)))
        self.kind_limits = {"network": 2, "media": 2, "cpu": 1, "model": 1}
        self.kind_limits.update(kind_limits or {})
        self._logger = logger
        self._history_path = Path(history_path) if history_path else None
        self._records = {}
        self._specs = {}
        self._handles = {}
        self._workers = {}
        self._outcomes = {}
        self._notifying = set()
        self._queue = []
        self._closing = False
        self._canceling_all = False
        self._temporary = data_io.TemporaryDirectory(prefix="lzx_background_")
        self.work_root = Path(self._temporary.name)
        self._persist_timer = QtCore.QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(500)
        self._persist_timer.timeout.connect(self.flush_history)
        self._load_history()

    def _assert_main_thread(self):
        if QtCore.QThread.currentThread() != self.thread():
            raise RuntimeError("后台任务的提交、取消和查询必须在主线程进行；运行函数只能使用 TaskRunContext。")

    def _log(self, message, level=logging.INFO):
        try:
            if self._logger:
                self._logger(message, level)
                return
        except Exception:
            logging.getLogger("assistant_tool").exception("后台任务日志回调失败")
        logging.getLogger("assistant_tool").log(level, message)

    def resume_accepting(self):
        self._assert_main_thread()
        self._closing = False

    def is_accepting(self):
        return not self._closing

    def apply_limits(self, settings):
        """Only future admissions change; running workers/payloads stay intact."""
        self._assert_main_thread()
        from .BackgroundTaskSettings import manager_options
        options=manager_options(settings)
        kinds={**self.kind_limits,**options['kind_limits']}
        if self.max_parallel != options['max_parallel'] or kinds != self.kind_limits:
            self.max_parallel=options['max_parallel'];self.kind_limits=kinds
            self.changed.emit('')
            if not self._closing:
                QtCore.QTimer.singleShot(0,self._dispatch)
        return options

    def submit(self, *, owner, title, runner, payload=None, kind="cpu",
               execution="parallel", resources=(), dedup_key="", retryable=False):
        self._assert_main_thread()
        from model.AppDataStorage import application_storage
        storage = application_storage()
        if storage.protected: storage.check()
        if self._closing:
            raise RuntimeError("程序正在退出，不能提交新后台任务。")
        storage=application_storage()
        if storage.protected:
            from model.ProtectedStorage import StorageCapacityError
            try:
                storage.check(write=True)
            except StorageCapacityError as error:
                # Plugin submit buttons already surface RuntimeError to users;
                # do not leak a new disk exception through a Qt callback.
                raise RuntimeError(str(error)) from error
        if execution not in {"parallel", "exclusive"}:
            raise ValueError("execution 必须为 parallel 或 exclusive。")
        if not callable(runner) or not str(title).strip():
            raise ValueError("后台任务需要名称和运行函数。")
        for task_id, spec in self._specs.items():
            if dedup_key and spec["owner"] == owner and spec["dedup_key"] == dedup_key \
                    and self._records[task_id]["status"] in ACTIVE:
                return self._handles[task_id]
        # Never send live widgets/model/config objects as payload. The runner
        # receives an isolated copy of the submission-time inputs.
        self._reject_qobjects(payload)
        snapshot = copy.deepcopy(payload)
        task_id = uuid4().hex
        spec = {"owner": str(owner), "title": str(title), "runner": runner,
                "payload": snapshot, "kind": str(kind), "execution": execution,
                "resources": frozenset(str(value) for value in resources if str(value)),
                "dedup_key": str(dedup_key), "retryable": bool(retryable)}
        self._specs[task_id] = spec
        self._records[task_id] = {
            "id": task_id, "owner": str(owner), "title": str(title),
            "kind": str(kind), "execution": execution, "status": "queued",
            "stage": "排队中", "percent": 0, "created_at": time.time(),
            "finished_at": None, "paths": [], "error": "", "traceback": "",
        }
        handle = BackgroundTaskHandle(self, task_id)
        self._handles[task_id] = handle
        self._queue.append(task_id)
        self._touch(task_id)
        # A queued dispatch lets the caller connect result signals first.
        QtCore.QTimer.singleShot(0, self._dispatch)
        return handle

    @classmethod
    def _reject_qobjects(cls, value):
        if isinstance(value, QtCore.QObject):
            raise TypeError("不能将 Qt 对象作为后台任务参数。")
        if isinstance(value, dict):
            for child in (*value.keys(), *value.values()):
                cls._reject_qobjects(child)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for child in value:
                cls._reject_qobjects(child)
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            for field in dataclasses.fields(value):
                cls._reject_qobjects(getattr(value, field.name))
        elif not isinstance(value, (str, int, float, bool, bytes, Path, type(None))):
            raise TypeError("后台任务参数只接受普通数据、路径和数据类，不能传入共享模型或运行时对象。")

    def get(self, task_id):
        self._assert_main_thread()
        return copy.deepcopy(self._records[task_id])

    def records(self):
        self._assert_main_thread()
        return [copy.deepcopy(row) for row in sorted(
            (row for row in self._records.values() if not row.get("discarded")),
            key=lambda row: row["created_at"], reverse=True)]

    def is_busy(self):
        return bool(self._queue or self._workers)

    def is_settled(self):
        """Also wait for result callbacks that may submit follow-up work."""
        return not self.is_busy() and not self._notifying

    def completion_states(self):
        """Lightweight main-thread status view, without copied logs/payloads."""
        self._assert_main_thread()
        return {key: row['status'] for key, row in self._records.items() if not row.get('discarded')}

    def waiting_reason(self, task_id):
        spec = self._specs.get(task_id)
        if not spec:
            return "排队中"
        if spec["execution"] == "exclusive":
            return "等待其他后台任务结束（独占执行）"
        if any(self._specs[other]["execution"] == "exclusive" for other in self._workers):
            return "等待独占任务结束"
        occupied = set().union(*(self._specs[other]["resources"] for other in self._workers))
        if resources_conflict(occupied, spec["resources"]):
            return "等待共用资源 / 同一输出目录空闲（避免覆盖或缓存冲突）"
        same_kind = sum(self._specs[other]["kind"] == spec["kind"] for other in self._workers)
        kind_limit = self.kind_limits.get(spec["kind"], 1)
        if same_kind >= kind_limit:
            label = {"media": "媒体处理", "network": "联网", "cpu": "计算", "model": "模型"}.get(spec["kind"], spec["kind"])
            return f"等待{label}并发名额（{same_kind}/{kind_limit}）；不是资源互斥"
        if len(self._workers) >= self.max_parallel:
            return f"等待后台并发名额（{len(self._workers)}/{self.max_parallel}）；不是资源互斥"
        return "排队中，等待并发名额"

    @QtCore.pyqtSlot()
    def _dispatch(self):
        if self._canceling_all:
            return
        if any(self._specs[key]["execution"] == "exclusive" for key in self._workers):
            return
        for task_id in list(self._queue):
            if len(self._workers) >= self.max_parallel:
                break
            spec = self._specs[task_id]
            # Do not let later parallel tasks starve an earlier exclusive job.
            if spec["execution"] == "exclusive":
                if self._workers:
                    break
                self._start(task_id)
                break
            occupied = set().union(*(self._specs[key]["resources"] for key in self._workers))
            same_kind = sum(self._specs[key]["kind"] == spec["kind"] for key in self._workers)
            if resources_conflict(occupied, spec["resources"]) or same_kind >= self.kind_limits.get(spec["kind"], 1):
                continue
            self._start(task_id)

    def _start(self, task_id):
        spec = self._specs[task_id]
        self._queue.remove(task_id)
        worker = _TaskWorker(task_id, spec["runner"], copy.deepcopy(spec["payload"]), threading.Event(), self)
        self._workers[task_id] = worker
        row = self._records[task_id]
        row.update(status="running", stage="正在启动", started_at=time.time(), percent=None)
        worker.progress.connect(self._on_progress)
        worker.itemProgress.connect(self._on_item_progress)
        worker.outcome.connect(self._on_outcome)
        worker.message.connect(self._on_message)
        worker.finished.connect(self._on_finished)
        self._touch(task_id)
        worker.start()

    @QtCore.pyqtSlot(str, str, object)
    def _on_progress(self, task_id, stage, percent):
        row = self._records[task_id]
        if row["status"] != "running":
            return
        row["stage"] = redact_sensitive_text(stage)
        row["percent"] = None if percent is None else max(0, min(99, int(percent)))
        self._handles[task_id].progressChanged.emit(row["stage"], row["percent"])
        self.changed.emit(task_id)

    @QtCore.pyqtSlot(str, str, int)
    def _on_item_progress(self, task_id, identifier, percent):
        if self._records[task_id]['status'] == 'running':
            self._handles[task_id].itemProgress.emit(identifier, percent)

    @QtCore.pyqtSlot(str, object)
    def _on_outcome(self, task_id, outcome):
        self._outcomes[task_id] = outcome

    @QtCore.pyqtSlot(str, str)
    def _on_message(self, task_id, message):
        row = self._records[task_id]
        self._log(redact_sensitive_text(f"[后台任务/{row['owner']}] {row['title']}：{message}"))

    @QtCore.pyqtSlot()
    def _on_finished(self, worker=None):
        worker = worker or self.sender()
        if not worker.wait(0):
            QtCore.QTimer.singleShot(10, lambda current=worker: self._on_finished(current))
            return
        task_id = worker.task_id
        self._notifying.add(task_id)
        outcome = self._outcomes.pop(task_id, {"status": "failed", "error": "工作线程没有返回结果。"})
        # A cancellation can reach the GUI after the worker emitted its result
        # but before the queued finished slot runs. Respect that late request.
        if worker.canceled.is_set():
            outcome["status"] = "canceled"
        row = self._records[task_id]
        row.update(status=outcome["status"], finished_at=time.time(),
                   error=outcome.get("error", ""), traceback=outcome.get("traceback", ""))
        result = outcome.get("result")
        if isinstance(result, dict):
            row["paths"] = [str(path) for path in result.get("paths", [])]
        row["stage"] = {"succeeded": "已完成", "failed": "失败", "canceled": "已取消"}.get(row["status"], "结束")
        if row["status"] == "succeeded":
            row["percent"] = 100
        handle = self._handles[task_id]
        self._workers.pop(task_id)
        worker.deleteLater()
        self._touch(task_id)
        try:
            if row["status"] == "succeeded":
                handle.completed.emit(result)
                self._log(f"[后台任务/{row['owner']}] {row['title']}：完成。")
            else:
                if result is not None:
                    handle.completed.emit(result)
                handle.failed.emit(row["error"] or row["stage"])
                self._log(f"[后台任务/{row['owner']}] {row['title']}：{row['error']}\n{row['traceback']}",
                          logging.ERROR if row["status"] == "failed" else logging.INFO)
            handle.finished.emit()
            if not self._specs[task_id]["retryable"]:
                self._specs[task_id]["payload"] = None
        finally:
            self._notifying.discard(task_id)
        self.changed.emit(task_id)
        self._dispatch()
        if not self.is_busy():
            self.flush_history()
            self.idle.emit()

    def cancel(self, task_id):
        self._assert_main_thread()
        row = self._records[task_id]
        if row["status"] == "queued":
            self._notifying.add(task_id)
            self._queue.remove(task_id)
            row.update(status="canceled", stage="已取消", error="操作已取消。", finished_at=time.time())
            self._touch(task_id)
            try:
                self._handles[task_id].failed.emit("操作已取消。")
                self._handles[task_id].finished.emit()
                if not self._specs[task_id]["retryable"]:
                    self._specs[task_id]["payload"] = None
            finally:
                self._notifying.discard(task_id)
            self.changed.emit(task_id)
            self._dispatch()
            if not self.is_busy():
                self.idle.emit()
        elif row["status"] == "running":
            row.update(status="canceling", stage="正在安全取消，等待当前步骤结束")
            self._workers[task_id].canceled.set()
            self._touch(task_id)

    def cancel_all(self):
        self.cancel_many(list(self._queue) + list(self._workers))

    def cancel_many(self, task_ids):
        self._assert_main_thread()
        previous = self._canceling_all
        self._canceling_all = True
        try:
            for task_id in task_ids:
                self.cancel(task_id)
        finally:
            self._canceling_all = previous
        self._dispatch()

    def stop_accepting(self):
        self._assert_main_thread()
        self._closing = True

    def retry(self, task_id):
        self._assert_main_thread()
        spec = self._specs.get(task_id)
        if not spec or not spec["retryable"] or self._records[task_id]["status"] not in {"failed", "canceled"}:
            raise ValueError("这个任务不支持直接重试，请回到插件重新提交。")
        return self.submit(**spec)

    def can_retry(self, task_id):
        spec = self._specs.get(task_id)
        return bool(spec and spec["retryable"] and self._records[task_id]["status"] in {"failed", "canceled"})

    def can_remove(self, task_id):
        self._assert_main_thread()
        row = self._records.get(task_id)
        return bool(row and not row.get("discarded") and row["status"] in TERMINAL
                    and task_id not in self._workers and task_id not in self._notifying)

    def remove(self, task_id):
        return bool(self.remove_many([task_id]))

    def remove_many(self, task_ids):
        """Forget finished records, never cancel work or delete result files.

        Existing plugins may retain handles and query their terminal status.
        Keep their in-memory snapshot, but omit it from the UI and disk history.
        """
        self._assert_main_thread()
        selected = [task_id for task_id in dict.fromkeys(task_ids) if self.can_remove(task_id)]
        if not selected:
            return 0
        for task_id in selected:
            self._records[task_id]["discarded"] = True
        if not self.flush_history():
            for task_id in selected:
                self._records[task_id].pop("discarded", None)
            raise OSError("无法保存后台任务历史，未删除记录。请检查磁盘空间或文件权限后重试。")
        for task_id in selected:
            self._specs.pop(task_id, None)
            handle = self._handles.pop(task_id, None)
            if handle is not None:
                # Do not destroy QObjects retained by plugins or pending queued
                # callbacks. Large retry payload/result closures can be released.
                handle._result_handler = None
                handle._result_pending = False
            self._outcomes.pop(task_id, None)
            self.changed.emit(task_id)
        return len(selected)

    def _touch(self, task_id):
        self.changed.emit(task_id)
        if self._history_path:
            self._persist_timer.start()

    def _load_history(self):
        if not self._history_path or not self._history_path.is_file():
            return
        try:
            rows = json.loads(data_file(self._history_path).read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("任务记录必须为列表")
            cutoff = time.time() - 7 * 86400
            for row in rows[-300:]:
                if not isinstance(row, dict) or row.get("discarded") or not row.get("id") or float(row.get("finished_at") or row.get("created_at", 0)) < cutoff:
                    continue
                if row.get("status") in ACTIVE:
                    row.update(status="interrupted", stage="上次程序退出时未完成，请回插件重新提交", finished_at=time.time())
                if row.get("status") not in TERMINAL:
                    continue
                defaults = {"owner": "unknown", "title": "后台任务", "kind": "cpu", "execution": "parallel",
                            "stage": "已结束", "percent": 0, "paths": [], "error": "", "traceback": "", "finished_at": None}
                normalized = {**defaults, **row}
                for key in ("id", "owner", "title", "kind", "execution", "stage", "error", "traceback"):
                    normalized[key] = str(normalized[key])
                normalized["created_at"] = float(normalized["created_at"])
                if normalized["finished_at"] is not None:
                    normalized["finished_at"] = float(normalized["finished_at"])
                if normalized["percent"] is not None:
                    normalized["percent"] = max(0, min(100, int(normalized["percent"])))
                if not isinstance(normalized["paths"], list):
                    raise ValueError("后台任务结果路径必须为列表")
                normalized["paths"] = [str(path) for path in normalized["paths"]]
                self._records[normalized["id"]] = normalized
        except (OSError, ValueError, TypeError):
            logging.getLogger("assistant_tool").exception("后台任务历史读取失败")
            backup = self._history_path.with_name(self._history_path.name + ".invalid-" + uuid4().hex)
            try:
                data_io.replace(self._history_path, backup)
            except OSError:
                # Do not overwrite a damaged record if preserving it failed.
                self._history_path = None
                logging.getLogger("assistant_tool").exception("无法备份损坏任务记录；本次禁用历史写入")

    @QtCore.pyqtSlot()
    def flush_history(self):
        if not self._history_path:
            return True
        cutoff = time.time() - 7 * 86400
        rows = [row for row in self._records.values() if not row.get("discarded") and (row["status"] in ACTIVE
                or float(row.get("finished_at") or row["created_at"]) >= cutoff)]
        rows = sorted(rows, key=lambda row: row["created_at"])[-300:]
        temporary = self._history_path.with_name(self._history_path.name + ".tmp")
        try:
            data_directory(self._history_path.parent).ensure(parents=True, exist_ok=True)
            # Metadata only: never persist runner, payload, cookies or keys.
            safe_rows = []
            for row in rows:
                safe = {key: redact_sensitive_text(value) if isinstance(value, str) else value
                        for key, value in row.items()}
                safe["paths"] = [redact_sensitive_text(str(path)) for path in row.get("paths", [])]
                safe["traceback"] = safe.get("traceback", "")[:20000]
                safe_rows.append(safe)
            data_file(temporary).write_text_direct(json.dumps(safe_rows, ensure_ascii=False, indent=2), encoding="utf-8")
            data_io.replace(temporary, self._history_path)
            return True
        except (OSError, TypeError):
            logging.getLogger("assistant_tool").exception("后台任务历史保存失败")
            return False

    def shutdown(self):
        self._assert_main_thread()
        self._closing = True
        if self.is_busy():
            self.cancel_all()
            return False
        self._persist_timer.stop()
        self.flush_history()
        self._temporary.cleanup()
        return True
