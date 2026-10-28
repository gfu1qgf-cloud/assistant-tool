"""One isolated unittest module; no real account state or unmocked network."""
import faulthandler
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import traceback
import unittest


def run_module(root, module):
    root = Path(root).resolve()
    if module not in {p.stem for p in (root / 'tests').glob('test_*.py')}:
        raise ValueError('Unknown test module')
    sys.path[:0] = [str(root), str(root / 'tests')]
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    def no_network(*args, **kwargs):
        raise AssertionError('Unmocked network access is forbidden in release tests')
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    callbacks = []
    def callback_error(kind, value, trace):
        callbacks.append(str(value))
        traceback.print_exception(kind, value, trace, file=sys.stderr)
    sys.excepthook = callback_error
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        # Developer PATHs may contain unrelated OpenSSL builds (e.g. Poppler).
        # Keep this interpreter's SSL/crypto pair together, with normal binaries.
        dlls = Path(sys.base_prefix)/'DLLs'
        if dlls.is_dir():
            dll_scope = os.add_dll_directory(str(dlls))
            if all((dlls/name).is_file() for name in ('libcrypto-3-x64.dll','libssl-3-x64.dll')):
                crypto = ctypes.WinDLL(str(dlls/'libcrypto-3-x64.dll'))
                ssl = ctypes.WinDLL(str(dlls/'libssl-3-x64.dll'))
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='release-test-', ignore_cleanup_errors=True) as temporary:
        isolated = Path(temporary)
        for name, child in {'HOME':'home', 'USERPROFILE':'home', 'APPDATA':'roaming',
                'LOCALAPPDATA':'local', 'CODEX_HOME':'codex', 'HF_HOME':'models'}.items():
            target = isolated / child
            target.mkdir(exist_ok=True)
            os.environ[name] = str(target)
        os.environ.update(LZX_TEST_SOURCE_ROOT=str(root), LZX_TEST_BASE_ROOT=str(root))
        os.chdir(isolated)
        try:
            import app_paths
            app_paths.APP_ROOT = isolated
            app_paths.DATA_ROOT = isolated
            from qt_compat import QtCore, QtWidgets
            from PyQt6 import sip
            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            faulthandler.dump_traceback_later(120, repeat=True)
            suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=module+'.py')
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            # Real close/queued destruction, not an exit-code override or forced termination.
            widgets = list(app.topLevelWidgets())
            for widget in widgets:
                if not sip.isdeleted(widget):
                    widget.close()
            deadline = time.monotonic() + 10
            while True:
                app.processEvents()
                threads = [thread for widget in widgets if not sip.isdeleted(widget)
                    for thread in widget.findChildren(QtCore.QThread)
                    if not sip.isdeleted(thread) and not thread.wait(0)]
                if not threads or time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            if threads:
                callbacks.append('Test-owned QThreads did not finish')
            for widget in widgets:
                if not sip.isdeleted(widget):
                    widget.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
            faulthandler.cancel_dump_traceback_later()
            print(f'RELEASE_TEST_RESULT: run={result.testsRun}, failures={len(result.failures)}, '
                f'errors={len(result.errors)}, skipped={len(result.skipped)}, callbacks={len(callbacks)}', flush=True)
            if callbacks:
                print('QT_CALLBACK_ERRORS:', repr(callbacks), flush=True)
            return 0 if result.testsRun and result.wasSuccessful() and not callbacks else 1
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    raise SystemExit(run_module(Path(__file__).resolve().parents[1], sys.argv[1]))
