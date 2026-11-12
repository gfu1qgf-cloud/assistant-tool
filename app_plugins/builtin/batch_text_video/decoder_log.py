"""Drain decoder errors without pipe deadlocks, unbounded RAM or log files."""
from collections import deque
import threading
from .media_errors import memory_failure,describe,MediaMemoryError


class DecoderLog:
    def __init__(self,process):
        self.stream=process.stderr;self.parts=deque(maxlen=8)
        self.thread=threading.Thread(target=self._read,name='video-decoder-errors',daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while True:
                block=self.stream.read(4096)
                if not block:break
                self.parts.append(block)
        except (OSError,ValueError):pass

    def check(self,process,label):
        if not process.returncode:return
        self.thread.join(timeout=2)
        tail=b''.join(self.parts).decode('utf8','replace')[-2000:]
        error=RuntimeError(label+f'（退出码{process.returncode}）：'+tail)
        if memory_failure(error):raise MediaMemoryError(describe(error)) from error
        raise error

    def close(self):
        self.thread.join(timeout=2)
        if not self.thread.is_alive():self.stream.close()
