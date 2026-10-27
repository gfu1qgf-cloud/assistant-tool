"""Process-wide named RLocks. Names come from trusted node registrations."""
from contextlib import contextmanager
import threading


_MUTEXES = {}
_REGISTRY_LOCK = threading.Lock()


def validate_mutex_name(name):
    if (not isinstance(name, str) or len(name) > 120 or name != name.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in name)):
        raise ValueError('节点互斥名称必须是最多120字的固定字符串，不能含首尾空白或控制字符。')
    return name


def named_mutex(name):
    """Stable identity for the life of this process; never evict held locks."""
    validate_mutex_name(name)
    if not name:
        raise ValueError('空名称表示不加锁，不能取得命名互斥锁。')
    with _REGISTRY_LOCK:
        lock = _MUTEXES.get(name)
        if lock is None:
            lock = _MUTEXES[name] = threading.RLock()
        return lock


@contextmanager
def node_mutex(name, context):
    """Cancel-aware acquisition; only the owning worker releases the lock."""
    if not name:
        yield
        return
    context.check_canceled()
    lock = named_mutex(name)
    acquired = lock.acquire(blocking=False)
    try:
        if not acquired:
            context.report('等待互斥锁：' + name, None)
            while not acquired:
                context.check_canceled()
                acquired = lock.acquire(timeout=.1)
            context.check_canceled()
            context.report('已获得互斥锁：' + name, None)
        context.check_canceled()
        yield
    finally:
        if acquired:
            lock.release()
