"""Own disposable media until every renderer/player using it has finished.

Each session gets one unique directory, reused across task output folders.
Cleanup never scans shared caches, user
sources, saved mattes or outputs, so independent batches cannot delete inputs
belonging to another renderer.
"""
from contextlib import ExitStack
from pathlib import Path
import threading

from model.AppDataStorage import data_directory, data_io


class MediaCacheSession:
    def __init__(self):
        self._scope = ExitStack()
        self._directory = None
        self._lock = threading.Lock()

    def directory(self, preferred, *, keep=True):
        root = Path(preferred)
        if keep:
            return root
        with self._lock:
            if self._directory is None:
                from model.MediaStorage import require_media_output
                require_media_output(root)
                data_directory(root).ensure(parents=True, exist_ok=True)
                folder = self._scope.enter_context(data_io.TemporaryDirectory(
                    prefix='temporary-', dir=str(root), ignore_cleanup_errors=True))
                self._directory = Path(folder)
            return self._directory

    def close(self):
        with self._lock:
            self._scope.close()
            self._directory = None
