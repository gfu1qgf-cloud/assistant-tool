"""Owned process lifetime, not a permissions sandbox."""
import os


class ProcessJob:
    def __init__(self, process, memory_mb=512):
        self.handle, self.kernel = None, None
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('PerProcessUserTimeLimit', c.c_longlong), ('PerJobUserTimeLimit', c.c_longlong),
                ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', c.c_size_t), ('MaximumWorkingSetSize', c.c_size_t),
                ('ActiveProcessLimit', w.DWORD), ('Affinity', c.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class Counters(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class Extended(c.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', Counters), ('ProcessMemoryLimit', c.c_size_t),
                ('JobMemoryLimit', c.c_size_t), ('PeakProcessMemoryUsed', c.c_size_t), ('PeakJobMemoryUsed', c.c_size_t)]
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]; kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]; kernel.SetInformationJobObject.restype = w.BOOL
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]; kernel.AssignProcessToJobObject.restype = w.BOOL
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; kernel.OpenProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]; kernel.CloseHandle.restype = w.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle: raise c.WinError(c.get_last_error())
        child = None
        try:
            limits = Extended(); limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
            limits.JobMemoryLimit = memory_mb * 1024 * 1024
            if not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            child = kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)  # SET_QUOTA | TERMINATE
            if not child or not kernel.AssignProcessToJobObject(handle, child):
                raise c.WinError(c.get_last_error())
            self.handle, self.kernel = handle, kernel
        except Exception:
            kernel.CloseHandle(handle)
            raise
        finally:
            if child: kernel.CloseHandle(child)

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            self.kernel.CloseHandle(handle)
