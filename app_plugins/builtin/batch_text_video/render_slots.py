"""A cancelable FIFO budget for final encodes across all projects."""
from collections import deque
from contextlib import contextmanager
import threading
from model.MediaEncoding import MAX_EXPORT_WORKERS

_LOCK = threading.Lock()
_GATES = {}


class _Gate:
    def __init__(self, limit):
        self.condition = threading.Condition()
        self.limit = limit
        self.active = 0
        self.waiters = deque()


def configure(scope, limit, *, replace=True):
    if not scope:
        return None
    limit = max(1, min(MAX_EXPORT_WORKERS, int(limit)))
    with _LOCK:
        gate = _GATES.setdefault(scope, _Gate(limit))
    if replace:
        with gate.condition:
            gate.limit = limit
            gate.condition.notify_all()
    return gate


@contextmanager
def render_slot(scope, limit, cancel=None, report=None, *, memory_bytes=0, progress=None):
    gate = configure(scope, limit, replace=False)
    if gate is None and not memory_bytes:
        yield
        return
    if gate is None:gate=_Gate(1)
    ticket = object()
    with gate.condition:
        gate.waiters.append(ticket)
    acquired = False
    notified = ''
    try:
        while not acquired:
            if cancel is not None and cancel.is_set():
                from .engine import Cancelled
                raise Cancelled('已取消等待全局渲染名额。')
            with gate.condition:
                if gate.waiters[0] is ticket and gate.active < gate.limit:
                    from .memory_pressure import ADMISSION
                    ready,detail=ADMISSION.request(memory_bytes) if memory_bytes else (True,'')
                    if ready:
                        gate.waiters.popleft()
                        gate.active += 1
                        acquired = True
                        gate.condition.notify_all()
                    else:
                        if notified!='memory':
                            if report:report(detail)
                            from .render_progress import stage
                            stage(progress,'等待内存释放')
                            notified='memory'
                        gate.condition.wait(.2)
                else:
                    if not notified and report:
                        report('等待全局渲染名额；其他项目正在编码。')
                        notified = 'slot'
                    gate.condition.wait(.1)
        yield
    finally:
        with gate.condition:
            if acquired:
                gate.active -= 1
            else:
                gate.waiters.remove(ticket)
            gate.condition.notify_all()
