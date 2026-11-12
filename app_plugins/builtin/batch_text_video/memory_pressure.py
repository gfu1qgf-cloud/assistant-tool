"""Cheap system-wide headroom checks; no allocation probes or disk caches."""
import ctypes
import os
import threading
import time

MIB=1024**2
GIB=1024**3


def read_windows_status():
    if os.name!='nt':return None
    class Status(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),
                  *[(name,ctypes.c_ulonglong) for name in ('total_physical','free_physical','total_commit','free_commit','total_virtual','free_virtual','extended')]]
    value=Status();value.length=ctypes.sizeof(value)
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes=[ctypes.POINTER(Status)];api.GlobalMemoryStatusEx.restype=ctypes.c_int
    if not api.GlobalMemoryStatusEx(ctypes.byref(value)):return None
    return {name:int(getattr(value,name)) for name in ('total_physical','free_physical','total_commit','free_commit')}


def read_status():
    try:return read_windows_status()
    except (OSError,AttributeError):return None


def estimate(args=(),settings=None):
    amount=512*MIB
    if settings is not None:
        from .canvas_settings import output_size
        w,h=output_size(settings)
        amount=max(amount,w*h*4*32)
    for value in args:
        if hasattr(value,'width') and hasattr(value,'height'):
            amount=max(amount,value.width*value.height*4*(20+2*getattr(value,'overlap',0)))
    return amount


class Admission:
    def __init__(self):
        self.lock=threading.Lock();self.starts=[];self.backoff=0.

    def failed(self):
        with self.lock:self.backoff=max(self.backoff,time.monotonic()+2.)

    def request(self,amount):
        with self.lock:
            now=time.monotonic();status=read_status()
            if status is None:return now>=self.backoff,'等待内存释放：暂时无法读取系统内存余量，可停止等待。'
            self.starts=[(at,size) for at,size in self.starts if now-at<2.]
            reserve=max(256*MIB,min(2*GIB,status['total_physical']//16))
            available=min(status['free_physical'],status['free_commit'])
            held=sum(size for _,size in self.starts)
            ready=now>=self.backoff and available-held>=reserve+amount
            message=f"等待内存释放：可用物理内存{status['free_physical']/GIB:.1f}GB，提交余量{status['free_commit']/GIB:.1f}GB；已有任务继续运行，可停止等待。"
            if ready:self.starts.append((now,amount))
            return ready,message


ADMISSION=Admission()
