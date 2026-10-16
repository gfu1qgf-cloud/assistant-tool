"""Serialize only native speech-model work, never whole media pipelines."""

import threading
from collections import deque
from contextlib import contextmanager


class _InferenceGate:
    """FIFO between threads, reentrant within one native operation.

    A timeout retry on an ordinary RLock can repeatedly lose to the next clip
    from the current batch. Keep each waiter's ticket until acquired/canceled.
    """
    def __init__(self):
        self.condition = threading.Condition()
        self.waiters = deque()
        self.owner = None
        self.depth = 0

    def acquire(self, check, progress):
        check()
        thread = threading.get_ident()
        ticket = object()
        with self.condition:
            if self.owner == thread:
                self.depth += 1
                return
            self.waiters.append(ticket)
            if self.owner is None and self.waiters[0] is ticket:
                self.waiters.popleft()
                self.owner, self.depth = thread, 1
                return
        try:
            if progress:
                progress("等待字幕模型空闲（其他识别／字幕对齐正在使用）")
            while True:
                check()
                with self.condition:
                    if self.owner is None and self.waiters[0] is ticket:
                        self.waiters.popleft()
                        self.owner, self.depth = thread, 1
                        return
                    self.condition.wait(timeout=.1)
        except BaseException:
            with self.condition:
                self.waiters.remove(ticket)
                self.condition.notify_all()
            raise

    def release(self):
        with self.condition:
            if self.owner != threading.get_ident():
                raise RuntimeError("字幕模型锁只能由持有它的线程释放。")
            self.depth -= 1
            if not self.depth:
                self.owner = None
                self.condition.notify_all()


_INFERENCE_GATE = _InferenceGate()


@contextmanager
def whisper_inference(*, progress=None, cancelled=None):
    """Cooperative, process-wide guard for the shared Whisper instance.

    faster-whisper returns lazy segments: callers must consume/close the
    iterator inside this scope. Media I/O, cache access and SRT formatting
    belong outside it. Never terminate a thread waiting for native inference.
    """
    def check():
        if cancelled and cancelled():
            raise InterruptedError("已取消字幕模型处理。")

    _INFERENCE_GATE.acquire(check, progress)
    try:
        check()
        if progress:
            progress("字幕模型已就绪，继续识别／对齐")
        yield
        check()
    finally:
        _INFERENCE_GATE.release()
