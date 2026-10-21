"""Activate one identified Chrome HWND; never guess from a shared process ID."""
import os
import re
import time

from qt_compat import QtCore
from .browser_options import snapshot
from .timing import request_timeout

UI_ACTIONS = {'get_points', 'prepare', 'upload_images', 'run_tasks', 'retry_download'}


def native_activate(marker):
    if os.name != 'nt': return 'unsupported'
    import ctypes
    from ctypes import wintypes as w
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    signatures = {
        'EnumWindows': ([callback_type, w.LPARAM], w.BOOL),
        'GetWindowTextW': ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetClassNameW': ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetWindowThreadProcessId': ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        'IsWindowVisible': ([w.HWND], w.BOOL), 'IsIconic': ([w.HWND], w.BOOL),
        'ShowWindowAsync': ([w.HWND, ctypes.c_int], w.BOOL),
        'BringWindowToTop': ([w.HWND], w.BOOL), 'SetForegroundWindow': ([w.HWND], w.BOOL),
        'GetForegroundWindow': ([], w.HWND),
    }
    for name, (args, result) in signatures.items():
        function = getattr(user, name); function.argtypes = args; function.restype = result
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]; kernel.CloseHandle.restype = w.BOOL
    matches = []

    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd): return True
        title = ctypes.create_unicode_buffer(2048); user.GetWindowTextW(hwnd, title, len(title))
        if not title.value.startswith(marker): return True
        name = ctypes.create_unicode_buffer(128); user.GetClassNameW(hwnd, name, len(name))
        if name.value != 'Chrome_WidgetWin_1': return True
        pid = w.DWORD(); user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel.OpenProcess(0x1000, False, pid.value)
        if not process: return True
        try:
            executable = ctypes.create_unicode_buffer(32768); size = w.DWORD(len(executable))
            if (kernel.QueryFullProcessImageNameW(process, 0, executable, ctypes.byref(size)) and
                    os.path.basename(executable.value).casefold() == 'chrome.exe'):
                matches.append(hwnd)
        finally: kernel.CloseHandle(process)
        return True

    if not user.EnumWindows(visit, 0): return 'error'
    if len(matches) != 1: return 'not_found' if not matches else 'ambiguous'
    hwnd = matches[0]
    if user.IsIconic(hwnd): user.ShowWindowAsync(hwnd, 9)  # SW_RESTORE, keep normal/maximized geometry otherwise.
    user.BringWindowToTop(hwnd)
    user.SetForegroundWindow(hwnd)
    return 'focused' if user.GetForegroundWindow() == hwnd else 'denied'


def receive(service, client, message):
    request_id = message.get('request_id'); pending = service.pending.get(request_id)
    token = message.get('token')
    if (not pending or pending['client'] != client['id'] or pending['action'] not in UI_ACTIONS or
            not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token) or
            token != pending.get('foreground_token') or pending.get('retired') or
            time.time() - pending['at'] >= request_timeout(service.preferences, pending['action'], client)): return
    if message.get('type') == 'foreground_cancel':
        pending['foreground_cancelled'] = True
        client['foreground'] = {'state': 'timeout', 'method': 'windows', 'at': time.time(), 'action': pending['action']}
        service.publish()
        return
    if pending.get('foreground_started') or pending.get('foreground_cancelled'): return
    pending['foreground_started'] = True

    def attempt(index=0):
        if (service.pending.get(request_id) is not pending or pending.get('foreground_cancelled') or pending.get('retired') or
                service.clients.get(client.get('socket')) is not client):
            return
        if message.get('unavailable') is True:
            state = 'error'
        elif not snapshot(service.preferences)['focus_before_action']:
            state = 'disabled'
        else:
            try: state = native_activate('[AssistantFlowFocus ' + token + ']')
            except (OSError, AttributeError, ValueError): state = 'error'
        # Chrome's caption can lag its tab title briefly. Never block the queue
        # thread, and never retry a generation command to recover focus.
        if state == 'not_found' and index < 4:
            QtCore.QTimer.singleShot(100, lambda: attempt(index+1)); return
        client['foreground'] = {'state': state, 'method': 'windows', 'at': time.time(), 'action': pending['action']}
        service.send(client, {'type': 'foreground_result', 'request_id': request_id, 'token': token, 'state': state})
        service.publish()
    attempt()


def description(client, preferences):
    if not snapshot(preferences)['focus_before_action']: return '前台激活：已关闭'
    if 'native_foreground_v1' not in client.get('capabilities', []):
        return '前台激活：当前扩展未加载 Windows 激活支持（扩展 ' + str(client.get('extension_version') or '未知版本') + '）'
    info = client.get('foreground', {})
    text = {'focused': 'Windows 已确认窗口在前台', 'denied': 'Windows 未允许切到前台',
            'not_found': '未找到本次操作对应窗口', 'ambiguous': '窗口标识不唯一，未切换',
            'unsupported': '当前系统不支持 Windows 激活', 'error': 'Windows 激活调用失败',
            'timeout': 'Windows 激活回复超时，未确认成功', 'disabled': '已关闭'}.get(info.get('state'), '等待首次网页操作确认')
    return ('上次前台激活：' if info else '前台激活：') + text
