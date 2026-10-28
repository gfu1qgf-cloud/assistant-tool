"""Bounded, read-ahead music work; reservation/commit order stays unchanged.

Only audio/file work runs here, never Qt fonts, alignment models or node code.
One batch reuses its first external cache directory, including across task folders.
The process-wide budget is acquired *after* the per-cache-key build lock so
duplicate readers cannot occupy every processing slot while waiting for one file.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError, wait
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import threading


_CURRENT = ContextVar('text_video_music_preparation', default=None)
_PROCESSING = threading.BoundedSemaphore(4)


def music_key(path, settings):
    """Keep existing persisted playheads compatible with the previous cache key."""
    from .store import file_identity
    path = Path(path)
    info = path.stat()
    signature = json.dumps([file_identity(path), info.st_size, info.st_mtime_ns,
                            settings['skip_silence'], settings['silence_db'], settings['silence_seconds']])
    return hashlib.sha256(signature.encode('utf-8')).hexdigest()


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        from .engine import Cancelled
        raise Cancelled('已取消音乐准备。')


@contextmanager
def processing_slot(cancel=None):
    acquired = False
    try:
        while not acquired:
            _check_cancel(cancel)
            acquired = _PROCESSING.acquire(timeout=.1)
        _check_cancel(cancel)
        yield
    finally:
        if acquired:
            _PROCESSING.release()


@contextmanager
def music_session(session):
    token = _CURRENT.set(session)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_music_session():
    return _CURRENT.get()


class _Cancellation:
    def __init__(self, parent):
        self.parent, self.local = parent, threading.Event()

    def is_set(self):
        return self.local.is_set() or bool(self.parent and self.parent.is_set())


class MusicPreparationSession:
    def __init__(self, cancel, workers):
        self.cancel = _Cancellation(cancel)
        self.limit = max(1, min(4, int(workers)))
        self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix='TextVideoMusic')
        self.lock = threading.Lock()
        self.entries = {}
        self.cache = None
        self.leases = {}
        self.cache_directories = set()
        from .media_cache import MediaCacheSession
        self.media_cache = MediaCacheSession()

    def __enter__(self):
        return self

    def _submit(self, path, settings, cache, ffmpeg, ffprobe):
        _check_cancel(self.cancel)
        key = (music_key(path, settings), str(ffmpeg), str(ffprobe))
        with self.lock:
            if self.cache is None:
                self.cache = Path(cache)
            if key in self.entries:
                return self.entries[key]
            # Bound queued work as well as active workers; do not enqueue a
            # whole library when only a handful of videos need to be exported.
            if sum(not future.done() for future in self.entries.values()) >= self.limit:
                return None
            options = {name: settings[name] for name in ('skip_silence', 'silence_db', 'silence_seconds')}
            future = self.executor.submit(self._prepare, path, options,
                                          self.cache, ffmpeg, ffprobe)
            self.entries[key] = future
            return future

    def _prepare(self, path, settings, cache, ffmpeg, ffprobe):
        from .engine import clean_music
        from .store import file_identity
        from model.MediaCacheLeases import media_cache_lease
        expected = music_key(path, settings)
        # Protect the generated path before building, then overlap this lease
        # with the session lease. No eviction gap between warm-up and rendering.
        with media_cache_lease(Path(cache) / (expected + '.flac')):
            result = clean_music(path, settings, cache, ffmpeg, ffprobe, self.cancel)
            _check_cancel(self.cancel)
            if result[2] != expected:
                raise ValueError('音乐准备期间原文件发生变化：' + Path(path).name)
            if file_identity(result[0]) != file_identity(path):
                with self.lock:
                    self._hold(result[0])
                    self.cache_directories.add(Path(result[0]).parent)
        return result

    def _hold(self, path):
        """Caller holds self.lock; transfer this lease to the export promptly."""
        from .store import file_identity
        from model.MediaCacheLeases import media_cache_lease
        identity = file_identity(path)
        if identity not in self.leases:
            lease = media_cache_lease(path)
            lease.__enter__()
            self.leases[identity] = lease

    def release(self, path):
        from .store import file_identity
        with self.lock:
            lease = self.leases.pop(file_identity(path), None)
        if lease is not None:
            lease.__exit__(None, None, None)

    def _forget(self, future):
        with self.lock:
            for key, entry in list(self.entries.items()):
                if entry is future:
                    del self.entries[key]

    def prime(self, state, cache, ffmpeg, ffprobe):
        if not state['settings'].get('music_enabled', True):
            return
        from .custom_pools import music_source
        items, next_id, _pool = music_source(state)
        if not items:
            return
        start = next((i for i, item in enumerate(items) if item['id'] == next_id), 0)
        for offset in range(min(self.limit, len(items))):
            try:
                self._submit(items[(start + offset) % len(items)]['path'],
                             state['settings'], cache, ffmpeg, ffprobe)
            except (OSError, ValueError):
                # Report unusable files only if/when selection reaches them.
                # Speculative preparation must not change the queue or log spam.
                continue

    def get(self, path, settings, cache, ffmpeg, ffprobe, cancel, report):
        notified = False
        while True:
            _check_cancel(cancel)
            _check_cancel(self.cancel)
            expected = music_key(path, settings)
            future = self._submit(path, settings, cache, ffmpeg, ffprobe)
            if future is None:
                with self.lock:
                    pending = [item for item in self.entries.values() if not item.done()]
                wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                continue
            try:
                value = future.result(timeout=.1)
            except TimeoutError:
                if not future.done():
                    if not notified:
                        report('等待音乐预处理：' + Path(path).name + '（后台并行准备中）')
                        notified = True
                    continue
                try:
                    value = future.result()
                except Exception:
                    self._forget(future)
                    raise  # Real processing timeout, not a completed short poll.
            except Exception:
                self._forget(future)
                raise  # A temporary source failure must not poison this batch.
            _check_cancel(cancel)
            if value[2] == expected == music_key(path, settings):
                from .store import file_identity
                if file_identity(value[0]) != file_identity(path):
                    with self.lock:
                        self._hold(value[0])
                    if not Path(value[0]).is_file() or Path(value[0]).stat().st_size == 0:
                        # A previous export released this ready cache and the
                        # size-limited pruner evicted it. Rebuild, don't return
                        # a stale successful Future pointing at a missing file.
                        self.release(value[0])
                        with self.lock:
                            self.entries.pop((expected, str(ffmpeg), str(ffprobe)), None)
                        continue
                return value
            # A changed source is a different cache key; never reuse old audio.

    def __exit__(self, *_args):
        self.cancel.local.set()
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            try:
                for lease in list(self.leases.values()):
                    lease.__exit__(None, None, None)
                self.leases.clear()
                from model.MediaCacheLeases import trim_generated_cache
                for directory in self.cache_directories:
                    trim_generated_cache(directory, '.flac', 1024**3)
            finally:
                # Encoding contexts and music workers have released their files.
                self.media_cache.close()
