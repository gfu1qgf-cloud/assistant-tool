"""Check native pointer ownership, shutdown ordering and real Windows COM."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import unittest
from unittest.mock import patch

from model.FlowParameterGuard import _FlowUiaController, FlowParameterGuardThread


class ComState:
    COINIT_MULTITHREADED = 0

    def __init__(self):
        self.active = False
        self.events = []

    def CoInitializeEx(self, flags):
        self.active = True
        self.events.append(("initialize", flags, threading.get_ident()))

    def CoUninitialize(self):
        self.events.append(("uninitialize", threading.get_ident()))
        self.active = False


class OwnedPointer:
    def __init__(self, name, state):
        self.name = name
        self.state = state
        self.owner = threading.get_ident()

    def __del__(self):
        self.state.events.append(("release", self.name, self.state.active,
                                  threading.get_ident() == self.owner))


def fake_load(state, fail=False):
    def load(controller):
        controller.UIA = object()
        for name in ("automation", "true_condition", "radio_condition",
                     "button_condition", "hyperlink_condition"):
            setattr(controller, name, OwnedPointer(name, state))
        if fail:
            raise RuntimeError("partial initialization")
    return load


class FlowComLifecycleTests(unittest.TestCase):
    def test_release_is_before_uninitialize_on_stop(self):
        state = ComState()
        guard = FlowParameterGuardThread({"poll_seconds": 30})
        guard._comtypes = state
        guard._wait = lambda: None
        with patch.object(_FlowUiaController, "_load_uia", fake_load(state)), \
             patch.object(_FlowUiaController, "scan", return_value=[]), \
             patch.object(guard, "isInterruptionRequested", side_effect=[False, True]):
            guard.run()
        releases = [event for event in state.events if event[0] == "release"]
        self.assertEqual(len(releases), 5)
        self.assertTrue(all(event[2:] == (True, True) for event in releases))
        self.assertEqual(state.events[0][0:2], ("initialize", 0))
        self.assertEqual(state.events[-1][0], "uninitialize")
        self.assertEqual(releases[-1][1], "automation")

    def test_partial_creation_failure_releases_before_uninitialize(self):
        state = ComState()
        guard = FlowParameterGuardThread({})
        guard._comtypes = state
        errors = []
        guard.error.connect(errors.append)
        with patch.object(_FlowUiaController, "_load_uia", fake_load(state, fail=True)):
            guard.run()
        releases = [event for event in state.events if event[0] == "release"]
        self.assertEqual(len(releases), 5)
        self.assertTrue(all(event[2:] == (True, True) for event in releases))
        self.assertEqual(state.events[-1][0], "uninitialize")
        self.assertIn("partial initialization", errors[0])

    def test_scan_failure_also_releases_before_uninitialize(self):
        state = ComState()
        guard = FlowParameterGuardThread({})
        guard._comtypes = state
        guard._wait = lambda: None
        with patch.object(_FlowUiaController, "_load_uia", fake_load(state)), \
             patch.object(_FlowUiaController, "scan", side_effect=RuntimeError("stale element")), \
             patch.object(guard, "isInterruptionRequested", side_effect=[False, True]):
            guard.run()
        self.assertTrue(all(event[2:] == (True, True) for event in state.events if event[0] == "release"))
        self.assertEqual(state.events[-1][0], "uninitialize")

    def test_failed_com_initialization_does_not_uninitialize(self):
        state = ComState()
        guard = FlowParameterGuardThread({})
        guard._comtypes = state
        with patch.object(state, "CoInitializeEx", side_effect=RuntimeError("initialization failed")), \
             patch.object(_FlowUiaController, "_load_uia") as loader:
            guard.run()
        loader.assert_not_called()
        self.assertFalse(state.events)

    def test_missing_library_reports_without_starting_or_uninitializing(self):
        guard = FlowParameterGuardThread({})
        guard._comtypes = None
        guard._com_import_error = "missing library"
        errors = []
        guard.error.connect(errors.append)
        with patch.object(_FlowUiaController, "_load_uia") as loader:
            guard.run()
        loader.assert_not_called()
        self.assertIn("missing library", errors[0])

    def test_close_is_idempotent_and_cross_thread_access_rejected(self):
        state = ComState()
        state.active = True
        with patch.object(_FlowUiaController, "_load_uia", fake_load(state)):
            controller = _FlowUiaController({})
        errors = []
        def other_thread():
            for operation in (controller.scan, controller.close):
                try:
                    operation()
                except RuntimeError as error:
                    errors.append(str(error))
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 2)
        self.assertFalse(state.events)
        controller.close()
        controller.close()
        self.assertEqual(len(state.events), 5)

    @unittest.skipUnless(os.name == "nt", "Windows native UI Automation requires Windows")
    def test_real_mta_guard_repeated_stop_and_process_exit(self):
        # A native access violation would kill an in-process test runner. Only
        # window enumeration is isolated: COM and UIA construction are real.
        script = textwrap.dedent("""
            import ctypes
            from unittest.mock import patch
            from qt_compat import QtCore
            from model.FlowParameterGuard import FlowParameterGuardThread
            app = QtCore.QCoreApplication([])
            errors = []
            class ProbeGuard(FlowParameterGuardThread):
                def _run_controller(self):
                    apt, qualifier = ctypes.c_int(), ctypes.c_int()
                    result = ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(apt), ctypes.byref(qualifier))
                    if result != 0 or apt.value != 1:
                        raise RuntimeError('UIA worker is not MTA')
                    super()._run_controller()
            with patch('model.FlowParameterGuard._enum_flow_windows', return_value=[]):
                for index in range(40):
                    guard = ProbeGuard({'poll_seconds': 30})
                    loop = QtCore.QEventLoop()
                    timer = QtCore.QTimer()
                    timer.setSingleShot(True)
                    timer.timeout.connect(lambda: (errors.append('timeout'), guard.stop(), loop.quit()))
                    guard.status.connect(lambda _message: guard.stop())
                    guard.error.connect(errors.append)
                    guard.finished.connect(loop.quit)
                    timer.start(15000)
                    guard.start()
                    loop.exec()
                    timer.stop()
                    assert guard.wait(5000), 'guard native thread did not finish'
                    assert not errors, errors
                    guard.deleteLater()
                    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            print('native-mta-exit-ok:40', flush=True)
        """)
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, f"Native exit {result.returncode}:\n{result.stdout}\n{result.stderr}")
        self.assertIn("native-mta-exit-ok:40", result.stdout)


if __name__ == "__main__":
    unittest.main()
