"""Process-wide resources for prepared work beyond each encoder window."""
import shutil
import threading
from pathlib import Path
from . import memory_pressure

MIB=1024**2
GIB=1024**3
MAX_AHEAD=8


class Ticket:
    def __init__(self,budget,identity):self.budget,self.identity=budget,identity
    def release(self):
        with self.budget.lock:self.budget.held.pop(self.identity,None)


class PreparationBudget:
    def __init__(self):
        self.lock=threading.Lock();self.held={}

    def request(self,amount,anchor):
        amount=max(64*MIB,int(amount))
        status=memory_pressure.read_status()
        if status is None:return None,'预准备暂不能读取内存余量，维持按需准备。'
        reserve=max(256*MIB,min(2*GIB,status['total_physical']//16))
        available=max(0,min(status['free_physical'],status['free_commit'])-reserve)
        try:
            path=Path(anchor)
            for _ in range(32):
                if path.is_dir():break
                if path.parent==path:break
                path=path.parent
            disk_free=shutil.disk_usage(path).free
        except (OSError,ValueError):return None,'预准备暂不能读取缓存盘余量，维持按需准备。'
        # Leave room for active encoders and final outputs. No extra disk cache
        # is created here: queued work owns the existing preparation contexts.
        budget=min(4*GIB,available//3,max(0,disk_free-2*GIB)//2)
        with self.lock:
            if len(self.held)>=MAX_AHEAD or sum(self.held.values())+amount>budget:
                return None,'预准备缓冲或资源余量不足；已有编码继续，释放后再准备。'
            identity=object();self.held[identity]=amount
        return Ticket(self,identity),''


def release(entry):
    ticket=entry.get('ahead_ticket')
    if ticket is not None:ticket.release()


BUFFER=PreparationBudget()
