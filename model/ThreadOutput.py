"""Capture a worker's prints without stealing stdout from concurrent jobs."""

import sys
import threading
from contextlib import contextmanager


_lock = threading.RLock()
_writers = {}
_streams = {}


class _ThreadStream:
    def __init__(self, fallback):
        self.fallback = fallback

    def write(self, text):
        writer = _writers.get(threading.get_ident())
        target = writer or self.fallback
        return target.write(text) if target is not None else len(text)

    def flush(self):
        writer = _writers.get(threading.get_ident())
        target = writer or self.fallback
        if target is not None:
            return target.flush()

    def __getattr__(self, name):
        return getattr(self.fallback, name)


@contextmanager
def capture_thread_output(writer):
    identity = threading.get_ident()
    with _lock:
        previous = _writers.get(identity)
        if not _writers:
            for name in ("stdout", "stderr"):
                original = getattr(sys, name)
                stream = _ThreadStream(original)
                _streams[name] = (original, stream)
                setattr(sys, name, stream)
        _writers[identity] = writer
    try:
        yield
    finally:
        try:
            writer.flush()
        finally:
            with _lock:
                if previous is None:
                    _writers.pop(identity, None)
                else:
                    _writers[identity] = previous
                if not _writers:
                    for name, (original, stream) in _streams.items():
                        if getattr(sys, name) is stream:
                            setattr(sys, name, original)
                    _streams.clear()
