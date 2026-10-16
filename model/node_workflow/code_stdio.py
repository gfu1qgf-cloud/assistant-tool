"""Inherited pipes for a PyInstaller windowed executable (sys.std* may be None)."""
import os
import sys


def binary_channel(name):
    stream = getattr(sys, name, None)
    if stream is not None and hasattr(stream, 'buffer'):
        return stream.buffer, False
    if os.name != 'nt':
        raise RuntimeError('Python节点缺少标准通信管道。')
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [w.DWORD]; kernel.GetStdHandle.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
    kernel.DuplicateHandle.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]; kernel.CloseHandle.restype = w.BOOL
    code = {'stdin': -10, 'stdout': -11, 'stderr': -12}[name]
    handle = kernel.GetStdHandle(code & 0xffffffff)
    if not handle or handle == c.c_void_p(-1).value:
        raise RuntimeError('Python节点未继承有效通信管道。')
    process, duplicate = kernel.GetCurrentProcess(), w.HANDLE()
    if not kernel.DuplicateHandle(process, handle, process, c.byref(duplicate), 0, False, 2):
        raise c.WinError(c.get_last_error())
    reading = name == 'stdin'
    try:
        fd = msvcrt.open_osfhandle(int(duplicate.value), os.O_BINARY | (os.O_RDONLY if reading else os.O_WRONLY))
    except Exception:
        kernel.CloseHandle(duplicate); raise
    # Buffered read loops across short Windows pipe reads until EOF/limit.
    return os.fdopen(fd, 'rb' if reading else 'wb'), True
