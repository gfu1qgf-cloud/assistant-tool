"""Cancellable task parsing and bounded, memory-only content caches.

No QObject or window is used here. A slow filesystem read can outlive its
request; callers discard its generation rather than waiting on the GUI thread.
"""
import copy
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import threading
import time

from model.AppDataStorage import data_directory
from model.ConfigHotReload import _read_config
from model.OdsHelper import ReadTaskOds2
from model.TaskTableSchema import load_task_table_schema


class TaskLoadCancelled(InterruptedError):
    pass


class TaskLoadError(ValueError):
    def __init__(self, message, *, title='任务表格读取失败', warning=False):
        super().__init__(message)
        self.title, self.warning = title, warning


@dataclass(frozen=True)
class TaskLoadRequest:
    root: str
    day: str
    config_path: str


@dataclass
class TaskLoadResult:
    project: Path
    tasks: list
    report: dict
    languages: list
    timings: dict
    table_cached: bool
    language_hits: int


def detect_language(text):
    # Do not import SubtitleHelper/Whisper just to classify text in this worker.
    from langdetect import detect, LangDetectException
    try:
        return detect(text) or 'unknown'
    except LangDetectException:
        return 'unknown'


class TaskLoadEngine:
    def __init__(self, *, reader=None, schema_loader=None, config_reader=None,
                 detector=None, missing_message=None, language_limit=4096, table_limit=2):
        self.reader = reader or ReadTaskOds2
        self.schema_loader = schema_loader or load_task_table_schema
        self.config_reader = config_reader or _read_config
        self.detector = detector or detect_language
        self.missing_message = missing_message
        self.language_limit, self.table_limit = language_limit, table_limit
        self._languages, self._tables = OrderedDict(), OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _check(cancelled):
        if cancelled():
            raise TaskLoadCancelled('任务加载已取消')

    @staticmethod
    def _text_key(text):
        return hashlib.sha256(str(text or '').encode('utf-8')).digest()

    def cached_language(self, text):
        key = self._text_key(text)
        with self._lock:
            value = self._languages.get(key)
            if value is not None:
                self._languages.move_to_end(key)
            return value

    def language(self, text, cancelled=lambda: False):
        self._check(cancelled)
        key = self._text_key(text)
        with self._lock:
            found = self._languages.get(key)
            if found is not None:
                self._languages.move_to_end(key)
                return found, True
        value = self.detector(str(text or '')) or 'unknown'
        self._check(cancelled)
        with self._lock:
            self._languages[key] = value
            self._languages.move_to_end(key)
            while len(self._languages) > self.language_limit:
                self._languages.popitem(last=False)
        return value, False

    def load(self, request, *, cancelled=lambda: False, progress=lambda stage: None):
        timings = {}
        began = time.perf_counter()
        self._check(cancelled)
        progress('读取配置')
        config = self.config_reader(request.config_path)
        self._check(cancelled)
        schema = self.schema_loader(path=request.config_path, migrate_legacy=False)
        timings['config'] = time.perf_counter() - began
        progress('检查任务目录')
        root = Path(request.root)
        if not request.root.strip() or not root.is_dir():
            raise TaskLoadError('任务根目录不存在或不是文件夹：\n{}\n\n请点击任务路径右侧的“…”选择包含每日日期文件夹的根目录。'
                                .format(request.root or '（未填写）'))
        name = str(config.get('task_table_file_name') or 'tasks.ods').strip()
        if name in {'', '.', '..'} or Path(name).name != name:
            raise TaskLoadError('任务表格文件名无效；只能填写文件名，不能填写目录或完整路径。')
        project = (root / request.day).resolve()
        path = project / name
        if not path.is_file():
            message = (self.missing_message(project, name) if self.missing_message else
                       '没有找到任务登记表：{}。请检查任务根目录、目标日期和文件名。'.format(path))
            raise TaskLoadError(message, title='没有找到任务登记表', warning=True)
        self._check(cancelled)
        progress('读取登记表')
        phase = time.perf_counter()
        before = path.stat()
        chunks = []
        with path.open('rb') as stream:
            while True:
                self._check(cancelled)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                chunks.append(block)
        payload = b''.join(chunks)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise TaskLoadError('读取期间登记表发生变化，请保存表格后重新加载；原列表保留。')
        key = (str(path).casefold(), hashlib.sha256(payload).digest(),
               hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()).digest())
        with self._lock:
            cached = self._tables.get(key)
            if cached is not None:
                self._tables.move_to_end(key)
        if cached is None:
            progress('解析登记表')
            try:
                tasks, report = self.reader(path, schema=schema, return_report=True,
                                            source_bytes=payload, cancelled=cancelled)
            except InterruptedError as error:
                raise TaskLoadCancelled(str(error)) from None
            except Exception as error:
                raise TaskLoadError('任务表格读取失败：{}\n登记表路径：{}\n\n'
                    '请打开“程序设置 → 任务表格”检查工作表、表头行和字段别名。'.format(error, path)) from None
            self._check(cancelled)
            # Cache a private copy, never objects later mutated by a plugin.
            if len(payload) <= 16 * 1024 * 1024 and len(tasks) <= 20000:
                entry = copy.deepcopy((tasks, report))
                with self._lock:
                    self._tables[key] = entry
                    while len(self._tables) > self.table_limit:
                        self._tables.popitem(last=False)
        else:
            tasks, report = copy.deepcopy(cached)
        timings['table'] = time.perf_counter() - phase
        self._check(cancelled)
        phase = time.perf_counter()
        languages, hits = [], 0
        for index, task in enumerate(tasks):
            self._check(cancelled)
            if index % 50 == 0:
                progress('检测语言 {}/{}'.format(index, len(tasks)))
            try:
                value, hit = self.language(task.task_audio_text or '', cancelled)
            except TaskLoadCancelled:
                raise
            except Exception:
                value, hit = 'unknown', False
            languages.append(value)
            hits += int(hit)
        timings['language'] = time.perf_counter() - phase
        self._check(cancelled)
        # Config/field mappings edited during parsing must not be applied late.
        progress('核对加载范围')
        latest = self.config_reader(request.config_path)
        if any(config.get(key) != latest.get(key) for key in ('task_table_file_name', 'task_table_schema')):
            raise TaskLoadError('读取期间任务表格配置已改变，请重新加载；未应用旧结果。')
        self._check(cancelled)
        # Preserve the old empty-result-directory behavior, off the GUI thread.
        data_directory(project / 'result').ensure(parents=True, exist_ok=True)
        self._check(cancelled)
        timings['total'] = time.perf_counter() - began
        return TaskLoadResult(project, tasks, report, languages, timings, cached is not None, hits)
