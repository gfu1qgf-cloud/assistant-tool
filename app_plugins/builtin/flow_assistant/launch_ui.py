"""GUI side of the worker's shared UI lease, used only by Flow launches."""
import os
import time

from qt_compat import QtCore
from .browser_options import CONFIG_KEY, normalize, snapshot
from .timing import value
from .setup_support import project_identity


def chrome_window_state():
    """Read native window appearance; never activate or close any window."""
    if os.name != 'nt': return None
    import ctypes
    from ctypes import wintypes as w
    try:
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        for name, args, result in (
            ('EnumWindows', [callback,w.LPARAM], w.BOOL),
            ('IsWindowVisible', [w.HWND], w.BOOL),
            ('GetClassNameW', [w.HWND,w.LPWSTR,ctypes.c_int], ctypes.c_int),
            ('GetWindowThreadProcessId', [w.HWND,ctypes.POINTER(w.DWORD)], w.DWORD),
            ('GetForegroundWindow', [], w.HWND)):
            function=getattr(user,name);function.argtypes=args;function.restype=result
        kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
        kernel.QueryFullProcessImageNameW.restype=w.BOOL
        kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
        windows=[]
        @callback
        def visit(hwnd, _):
            if not user.IsWindowVisible(hwnd): return True
            name=ctypes.create_unicode_buffer(128);user.GetClassNameW(hwnd,name,len(name))
            if name.value!='Chrome_WidgetWin_1': return True
            pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            process=kernel.OpenProcess(0x1000,False,pid.value)
            if process:
                try:
                    executable=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(executable))
                    if (kernel.QueryFullProcessImageNameW(process,0,executable,ctypes.byref(size)) and
                            os.path.basename(executable.value).casefold()=='chrome.exe'):
                        windows.append(int(hwnd))
                finally: kernel.CloseHandle(process)
            return True
        if not user.EnumWindows(visit,0): return None
        return (tuple(sorted(windows)),int(user.GetForegroundWindow() or 0))
    except (OSError,AttributeError,ValueError): return None


class LaunchUiGate(QtCore.QObject):
    def __init__(self, plugin):
        super().__init__(plugin)
        self.plugin=plugin;self.active=None
        self.timer=QtCore.QTimer(self);self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll)

    def enabled(self):
        preferences=self.plugin.snapshot.get('preferences')
        if isinstance(preferences,dict): return snapshot(preferences)['ui_mutex']
        load=getattr(self.plugin.context,'load_config',None)
        config=load() if callable(load) else {}
        return normalize(config.get(CONFIG_KEY))['ui_mutex'] if isinstance(config,dict) and CONFIG_KEY in config else False

    def track(self, entry):
        if not self.enabled() or entry.get('ui_tracked'): return
        entry['ui_tracked']=True
        entry.setdefault('ui_wait_started',time.time())
        self.plugin.command.emit({'action':'ui_launch_wait','token':entry['ticket'],
            'profile':entry['profile'],'waiting':True})

    def untrack(self, entry):
        if entry.pop('ui_tracked',False):
            self.plugin.command.emit({'action':'ui_launch_wait','token':entry['ticket'],'waiting':False})

    def request(self, entry):
        maximum=value(self.plugin.snapshot.get('preferences',{}),'browser_startup_seconds')
        if time.time()-entry.get('ui_wait_started',time.time())>=maximum:
            error=f'启动浏览器等待UI互斥超过 {maximum:g} 秒，已跳过此配置；任务保留，继续其他账号。'
            self.untrack(entry);self.plugin._launch_report(entry,False,error)
            return False,error
        thread=self.plugin.thread
        if self.plugin.service is None or thread is None or not thread.isRunning():
            error='UI互斥服务尚未就绪，未启动浏览器；请等主程序连接服务就绪后重试。'
            self.untrack(entry);self.plugin._launch_report(entry,False,error)
            return False,error
        self.active=entry;self.track(entry)
        self.plugin.command.emit({'action':'ui_native_request','token':entry['ticket'],'profile':entry['profile']})
        if self.plugin.dialog:
            self.plugin.dialog.note.setText('FLOW 启动浏览器正在等待 UI 互斥；当前传图／页面操作完成后继续。')
        return True,None

    @QtCore.pyqtSlot(object)
    def result(self, result):
        entry=self.active;token=result.get('token')
        if not entry or entry['ticket']!=token: return
        state=result.get('state')
        if state in {'granted','disabled'} and not entry.get('reported'):
            if self.plugin.stopping or state=='granted' and result.get('expires_at',0)<=time.time():
                self.plugin._launch_report(entry,False,'已取消待启动浏览器');self.finish();return
            entry['lease']=state=='granted'
            entry['last_window_state']=chrome_window_state() if entry['lease'] else None
            entry['started_wall']=time.time();entry['started_at']=entry['changed_at']=time.monotonic()
            ok,_=self.plugin._launch_now(entry)
            self.untrack(entry)
            if not ok or not entry['lease']: self.finish();return
            self.timer.start()
        elif state not in {'granted'}:
            if not entry.get('reported'):
                error='启动浏览器的UI互斥等待超时或已取消；已跳过此配置，任务保留原队列。'
                self.plugin._launch_report(entry,False,error)
                if self.plugin.dialog:self.plugin.dialog.note.setText(error)
            self.finish()

    def poll(self):
        entry=self.active
        if not entry or not entry.get('opened'): self.finish();return
        if entry.get('tab_reuse_pending'): return
        now=time.monotonic();observed=chrome_window_state()
        if observed!=entry.get('last_window_state'):
            entry['last_window_state']=observed;entry['changed_at']=now
        # Another manually opened Chrome is outside Flow's lock. Its new HWND
        # alone cannot prove that this requested profile has finished opening.
        targets={project_identity(url) for url in entry['targets']} - {None}
        if any(c.get('profile')==entry['profile'] and (c.get('page_loaded') or c.get('page_ready')) and
               c.get('page_at',0)>=entry['started_wall'] and project_identity(c.get('project')) in targets
               for c in self.plugin.snapshot.get('clients',[])):
            entry['surface_seen']=True
        maximum=min(30,max(5,value(self.plugin.snapshot.get('preferences',{}),'browser_startup_seconds')))
        if entry.get('surface_seen') and now-entry['changed_at']>=1 and now-entry['started_at']>=1:
            self.finish()
        elif now-entry['started_at']>=maximum:
            self.plugin.context.log('FLOW 浏览器启动窗口未在时限内确认稳定；已释放UI锁，保留原启动占位，不重复开窗。')
            self.finish()

    def finish(self):
        self.timer.stop();entry,self.active=self.active,None
        if entry:
            self.untrack(entry)
            self.plugin.command.emit({'action':'ui_native_release','token':entry['ticket']})
        if not self.plugin.stopping and self.plugin.pending_browser_launches and not self.plugin.launch_timer.isActive():
            self.plugin.launch_timer.start(0)

    def stop(self):
        if self.active and not self.active.get('reported'):
            self.plugin._launch_report(self.active,False,'已取消待启动浏览器')
        self.finish()
