"""Cancelable Windows byte pipe. Only a bounded OS buffer, never a media file."""
import ctypes
from ctypes import wintypes as W
import os
import uuid


class MemoryPipe:
    def __init__(self, cancel):
        if os.name != 'nt': raise RuntimeError('内存抠像管道需要Windows。')
        self.cancel = cancel
        self.name = r'\\.\pipe\text-video-matte-' + uuid.uuid4().hex
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                        ('Offset', W.DWORD), ('OffsetHigh', W.DWORD), ('hEvent', W.HANDLE)]
        self.Overlapped = Overlapped
        signatures = {
            'CreateNamedPipeW': ([W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.LPVOID], W.HANDLE),
            'CreateEventW': ([W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            'ConnectNamedPipe': ([W.HANDLE, ctypes.POINTER(Overlapped)], W.BOOL),
            'WriteFile': ([W.HANDLE, W.LPCVOID, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.POINTER(Overlapped)], W.BOOL),
            'GetOverlappedResult': ([W.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(W.DWORD), W.BOOL], W.BOOL),
            'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'CancelIoEx': ([W.HANDLE, ctypes.POINTER(Overlapped)], W.BOOL),
            'CloseHandle': ([W.HANDLE], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name); function.argtypes = args; function.restype = result
        self.handle = self.api.CreateNamedPipeW(self.name, 0x40000002, 0, 1, 65536, 65536, 0, None)
        if self.handle == W.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())

    def _operation(self, start, *, connect=False):
        from .matting_models import check_cancel
        check_cancel(self.cancel)
        event = self.api.CreateEventW(None, True, False, None)
        if not event: raise ctypes.WinError(ctypes.get_last_error())
        operation = self.Overlapped(); operation.hEvent = event
        count = W.DWORD()
        pending = False
        try:
            if start(operation, count): return count.value
            code = ctypes.get_last_error()
            if connect and code == 535: return 0  # Client won the connect race.
            if code in (109, 232): raise BrokenPipeError('合成器已关闭抠像输入。')
            if code != 997: raise ctypes.WinError(code)
            pending = True
            while self.api.WaitForSingleObject(event, 100) == 258:
                check_cancel(self.cancel)
            if not self.api.GetOverlappedResult(self.handle, ctypes.byref(operation), ctypes.byref(count), False):
                code = ctypes.get_last_error()
                if code in (109, 232): raise BrokenPipeError('合成器已关闭抠像输入。')
                raise ctypes.WinError(code)
            pending = False
            return count.value
        finally:
            if pending:
                # Reap before releasing the OVERLAPPED structure or its buffer.
                self.api.CancelIoEx(self.handle, ctypes.byref(operation))
                self.api.GetOverlappedResult(self.handle, ctypes.byref(operation), ctypes.byref(count), True)
            self.api.CloseHandle(event)

    def connect(self):
        self._operation(lambda op, count: self.api.ConnectNamedPipe(self.handle, ctypes.byref(op)), connect=True)

    def write(self, data):
        offset = 0
        # Keep each in-flight allocation small even for 4K input frames.
        while offset < len(data):
            chunk = data[offset:offset+65536]
            size = self._operation(lambda op, count: self.api.WriteFile(
                self.handle, chunk, len(chunk), ctypes.byref(count), ctypes.byref(op)))
            if not size: raise BrokenPipeError('抠像管道未写入数据。')
            offset += size

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle); self.handle = None
