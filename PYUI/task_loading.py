"""GUI adapter for generation-checked task loads; no filesystem waits on GUI."""
import logging
from pathlib import Path
import queue
import threading
import time

from qt_compat import QtCore, QtWidgets
from model.TaskLoading import TaskLoadEngine, TaskLoadRequest, TaskLoadCancelled, TaskLoadError
from model.OdsHelper import format_task_table_report


def _run(engine, request, cancelled, results, generation):
    try:
        value = engine.load(request, cancelled=cancelled.is_set,
            progress=lambda stage: results.put(('progress', generation, stage)))
        results.put(('completed', generation, value))
    except TaskLoadCancelled:
        pass
    except (Exception, SystemExit) as error:
        results.put(('failed', generation, error))


class TaskLoadController(QtCore.QObject):
    def __init__(self, host, *, engine=None, timeout=45, max_workers=2, **engine_options):
        super().__init__(host)
        self.host = host
        self.engine = engine or TaskLoadEngine(**engine_options)
        self.timeout, self.max_workers = timeout, max_workers
        self.results = queue.SimpleQueue()
        self.workers = {}
        self.generation = 0
        self.current = None
        self.stage = ''
        self.closed = False
        self.button_text = host.load_btn.text()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)

    @property
    def busy(self):
        return self.current is not None

    def _button(self, loading):
        self.host.load_btn.setText('取消加载' if loading else self.button_text)

    def cancel(self, message='已取消加载，原列表保留。'):
        self.generation += 1
        for worker, event in self.workers.values():
            event.set()
        was_busy = self.busy
        self.current = None
        if not self.closed:
            self._button(False)
            if was_busy:
                self.host.task_status_label.setText(message)

    def request(self, root, day, config_path):
        if self.closed:
            return False
        request = TaskLoadRequest(str(root).strip(), str(day), str(config_path))
        if self.current and request == self.current['request']:
            self.cancel()
            return False
        self.cancel()
        self.workers = {key: value for key, value in self.workers.items() if value[0].is_alive()}
        if len(self.workers) >= self.max_workers:
            self.host.task_status_label.setText('旧读取仍在等待磁盘响应；未重复启动，原列表保留。')
            self.host.appendLog('[任务加载] 磁盘读取尚未返回；已限制后台读取数量，界面可继续使用。')
            return False
        generation = self.generation
        event = threading.Event()
        worker = threading.Thread(target=_run, args=(self.engine, request, event, self.results, generation),
                                  name='task-table-read', daemon=True)
        self.workers[generation] = worker, event
        self.current = {'request': request, 'generation': generation, 'started': time.monotonic(), 'warned': False}
        self.stage = '准备读取'
        self._button(True)
        self.host.task_status_label.setText('正在后台加载任务，原列表保留…')
        self.timer.start()
        worker.start()
        return True

    def _matches(self):
        if not self.current:
            return False
        request = self.current['request']
        return (self.host.task_path_edit.text().strip() == request.root
                and self.host.dateEdit.date().toString('MMdd') == request.day)

    def poll(self):
        if self.closed:
            return
        try:
            self._poll()
        except Exception as error:
            logging.getLogger('assistant_tool').exception('[任务加载] 界面更新失败')
            self.cancel('加载结果未能应用，原列表保留。')
            self.host.appendLog('[任务加载] 界面更新失败：{}'.format(error), level=logging.ERROR)

    def _poll(self):
        while not self.results.empty():
            kind, generation, value = self.results.get()
            if not self.current or generation != self.current['generation']:
                continue
            if not self._matches():
                self.cancel('路径或日期已改变，旧加载结果已丢弃。')
                continue
            if kind == 'progress':
                self.stage = value
            elif kind == 'completed':
                self._complete(value)
            elif kind == 'failed':
                self.current = None
                self._button(False)
                self.host.task_status_label.setText('加载失败，原列表保留。')
                self.host.appendLog('[任务加载] {}'.format(value), level=logging.ERROR)
                if isinstance(value, TaskLoadError) and value.warning:
                    QtWidgets.QMessageBox.warning(self.host, value.title, str(value))
                else:
                    self.host.Critical(str(value))
        self.workers = {key: value for key, value in self.workers.items() if value[0].is_alive()}
        if self.current:
            elapsed = time.monotonic() - self.current['started']
            if elapsed >= self.timeout:
                phase = self.stage
                self.cancel('加载超时，原列表保留；可检查磁盘后重试。')
                self.host.appendLog('[任务加载] 超过 {} 秒，已取消本次结果；停在“{}”。'
                    '未等待或强杀磁盘线程，界面可继续操作。'.format(self.timeout, phase), level=logging.WARNING)
            else:
                self.host.task_status_label.setText('后台加载 · {} · {}秒（可取消）'.format(self.stage, int(elapsed)))
                if elapsed >= 8 and not self.current['warned']:
                    self.current['warned'] = True
                    self.host.appendLog('[任务加载] 读取较慢，当前阶段：{}；可取消，原列表保留。'.format(self.stage))
        if not self.workers and not self.current:
            self.timer.stop()

    def _complete(self, result):
        host = self.host
        old_tasks = host.task_list
        old_project = host.loaded_project_dir
        previous_languages = dict(getattr(host, '_task_language_by_row', {}))
        # Prepare selection before replacing model rows. No file IO here.
        same_project = old_project is not None and str(old_project) == str(result.project)
        selected = set()
        if same_project:
            for index in host.task_table_widget.selectionModel().selectedRows():
                task = old_tasks[index.row()]
                selected.add((task.task_type, task.task_id, task.source_row))
        scroll = host.task_table_widget.verticalScrollBar().value() if same_project else 0
        try:
            host.task_list = result.tasks
            host.refreshTaskWidget(languages=result.languages)
        except Exception:
            host.task_list = old_tasks
            host.refreshTaskWidget(languages=[previous_languages.get(row, 'unknown') for row in range(len(old_tasks))])
            raise
        host.loaded_project_dir = result.project
        if selected:
            flags = QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows
            model = host.task_table_widget.model()
            selection = host.task_table_widget.selectionModel()
            for row, task in enumerate(result.tasks):
                if (task.task_type, task.task_id, task.source_row) in selected:
                    selection.select(model.index(row, 0), flags)
        host.task_table_widget.verticalScrollBar().setValue(scroll)
        self.current = None
        self._button(False)
        host.updateGoogleSheetDownloadTarget()
        host.task_status_label.setText('已加载 {} 个任务'.format(len(result.tasks)))
        host.appendLog(format_task_table_report(result.report))
        host.appendLog('[任务加载] 完成：{} 条；读表 {:.2f}s{}，语言 {:.2f}s（缓存命中 {} 条），总计 {:.2f}s。'.format(
            len(result.tasks), result.timings['table'], '（解析缓存）' if result.table_cached else '',
            result.timings['language'], result.language_hits, result.timings['total']))
        # Filesystem enumeration/download preparation must not run inline here.
        try:
            if host.file_explorer_tree_view.isVisible():
                host.file_explorer_tree_view.load_directory(result.project, validated=True)
            host.startTaskReferenceDownloads(result.project)
        except Exception as error:
            host.appendLog('[任务加载] 任务已加载，附加目录/参考文件操作失败：{}'.format(error), level=logging.WARNING)

    def shutdown(self):
        if self.closed:
            return
        self.cancel()
        self.closed = True
        self.timer.stop()
        self.host = None
