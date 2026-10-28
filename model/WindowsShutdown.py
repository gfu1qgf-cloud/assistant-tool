"""Local Windows power-off boundary. No elevation, forced close or OS timer.

check_shutdown_permission only enables/restores an existing token privilege.
Only request_windows_shutdown can call ExitWindowsEx, after host cleanup.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import logging
import os
from types import SimpleNamespace


class _LUID(ctypes.Structure):
    _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Luid', _LUID), ('Attributes', wintypes.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [('PrivilegeCount', wintypes.DWORD),
                ('Privileges', _LUID_AND_ATTRIBUTES * 1)]


def _load_api():
    if os.name != 'nt':
        raise OSError('自动关机目前只支持Windows。')
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    security = ctypes.WinDLL('advapi32.dll', use_last_error=True)
    user = ctypes.WinDLL('user32.dll', use_last_error=True)
    api = SimpleNamespace(
        current_process=kernel.GetCurrentProcess, close_handle=kernel.CloseHandle,
        open_token=security.OpenProcessToken,
        lookup_privilege=security.LookupPrivilegeValueW,
        adjust_privileges=security.AdjustTokenPrivileges,
        exit_windows=user.ExitWindowsEx,
        get_error=ctypes.get_last_error, set_error=ctypes.set_last_error)
    api.current_process.argtypes = []
    api.current_process.restype = wintypes.HANDLE
    api.close_handle.argtypes = [wintypes.HANDLE]
    api.close_handle.restype = wintypes.BOOL
    api.open_token.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                              ctypes.POINTER(wintypes.HANDLE)]
    api.open_token.restype = wintypes.BOOL
    api.lookup_privilege.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    ctypes.POINTER(_LUID)]
    api.lookup_privilege.restype = wintypes.BOOL
    api.adjust_privileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
        ctypes.POINTER(_TOKEN_PRIVILEGES), wintypes.DWORD,
        ctypes.POINTER(_TOKEN_PRIVILEGES), ctypes.POINTER(wintypes.DWORD)]
    api.adjust_privileges.restype = wintypes.BOOL
    api.exit_windows.argtypes = [wintypes.UINT, wintypes.DWORD]
    api.exit_windows.restype = wintypes.BOOL
    return api


def _error(stage, code):
    code = int(code)
    detail = ctypes.FormatError(code).strip() if code else '系统没有返回具体错误码'
    if code in (5, 1300, 1314):
        return PermissionError(code, f'{stage}失败（Windows代码{code}）：{detail}。'
            '当前进程没有可用的“关闭系统”权限；请检查启动方式或联系管理员核对用户权限策略。'
            '程序不会自动提权或修改系统策略。')
    return OSError(code, f'{stage}失败（Windows代码{code}）：{detail}')


@contextmanager
def _shutdown_privilege(api):
    token = wintypes.HANDLE()
    # TOKEN_QUERY | TOKEN_ADJUST_PRIVILEGES. Enable only this existing right.
    if not api.open_token(api.current_process(), 0x0028, ctypes.byref(token)):
        raise _error('读取关机权限', api.get_error())
    previous = _TOKEN_PRIVILEGES()
    changed = False
    failed = False
    try:
        desired = _TOKEN_PRIVILEGES()
        desired.PrivilegeCount = 1
        if not api.lookup_privilege(None, 'SeShutdownPrivilege',
                                    ctypes.byref(desired.Privileges[0].Luid)):
            raise _error('查询关机权限', api.get_error())
        desired.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
        returned = wintypes.DWORD()
        api.set_error(0)
        ok = api.adjust_privileges(token, False, ctypes.byref(desired),
            ctypes.sizeof(previous), ctypes.byref(previous), ctypes.byref(returned))
        code = api.get_error()  # TRUE can still mean ERROR_NOT_ALL_ASSIGNED.
        changed = bool(previous.PrivilegeCount)
        if not ok or code:
            raise _error('启用关机权限', code)
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if changed:
                api.set_error(0)
                ok = api.adjust_privileges(token, False, ctypes.byref(previous),
                                            0, None, None)
                code = api.get_error()
                if not ok or code:
                    error = _error('恢复关机权限', code)
                    if not failed:
                        raise error
                    logging.getLogger('assistant_tool').warning('%s', error)
        finally:
            api.close_handle(token)


def check_shutdown_permission():
    """Preflight only: never calls a shutdown API or starts a helper process."""
    with _shutdown_privilege(_load_api()):
        pass


def request_windows_shutdown():
    """Request non-forced power-off; Windows/other applications can still veto."""
    api = _load_api()
    with _shutdown_privilege(api):
        # EWX_POWEROFF only. Neither FORCE nor FORCEIFHUNG, no logout/reboot.
        # SHTDN_REASON_FLAG_PLANNED | SHTDN_REASON_MAJOR_APPLICATION.
        if not api.exit_windows(0x00000008, 0x80040000):
            raise _error('Windows接受关机请求', api.get_error())
