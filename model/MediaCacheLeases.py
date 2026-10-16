"""Prevent generated-cache pruning while an export still owns its inputs."""
from collections import Counter
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import re

_LOCK = threading.RLock()
_IN_USE = Counter()
_BUILD_LOCKS = {}


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


@contextmanager
def media_cache_lease(path):
    key = _key(path)
    with _LOCK:
        _IN_USE[key] += 1
    try:
        yield
    finally:
        with _LOCK:
            _IN_USE[key] -= 1
            if not _IN_USE[key]:
                del _IN_USE[key]


def media_cache_in_use(path):
    with _LOCK:
        return bool(_IN_USE[_key(path)])


@contextmanager
def media_cache_build(path, cancel=None):
    """Serialize just one cache key while independent media keeps preparing."""
    key = _key(path)
    with _LOCK:
        entry = _BUILD_LOCKS.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    acquired = False
    try:
        while not acquired:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('已取消等待媒体缓存。')
            acquired = entry[0].acquire(timeout=.1)
        yield
    finally:
        if acquired:
            entry[0].release()
        with _LOCK:
            entry[1] -= 1
            if not entry[1]:
                del _BUILD_LOCKS[key]


def trim_generated_cache(directory, suffix, limit):
    """Only SHA256-named generated media; never remove leased/source files."""
    from model.AppDataStorage import data_file
    if suffix not in {'.flac', '.mp4'} or limit <= 0:
        raise ValueError('生成缓存清理参数无效。')
    with _LOCK:
        entries = []
        for path in Path(directory).glob('*'+suffix):
            if not re.fullmatch(r'[0-9a-f]{64}', path.stem):
                continue
            try:
                stat = path.stat()
                entries.append((stat.st_mtime, stat.st_size, path))
            except FileNotFoundError:
                continue
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= limit:
                break
            if _IN_USE[_key(path)] or _key(path) in _BUILD_LOCKS:
                continue
            try:
                data_file(path).remove(missing_ok=True)
                if suffix == '.flac':
                    data_file(path.with_suffix('.json')).remove(missing_ok=True)
                total -= size
            except OSError:
                pass  # Another process may still own a disposable cache; source/results stay untouched.
