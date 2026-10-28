"""All power-off boundaries use a fake API. Never actually shut down Windows."""
import ctypes
import unittest
from unittest.mock import Mock, patch
from model import WindowsShutdown as shutdown


class FakeAPI:
    def __init__(self, *, changed=True, missing=False):
        self.error = 0
        self.changed, self.missing = changed, missing
        self.get_error = lambda: self.error
        self.set_error = lambda value: setattr(self, 'error', value)
        self.current_process = Mock(return_value=-1)
        self.open_token = Mock(side_effect=self._open)
        self.lookup_privilege = Mock(side_effect=self._lookup)
        self.adjust_privileges = Mock(side_effect=self._adjust)
        self.close_handle = Mock(return_value=True)
        self.exit_windows = Mock(return_value=True)

    def _open(self, _process, _access, token):
        token._obj.value = 12345
        return True

    def _lookup(self, machine, name, luid):
        assert machine is None and name == 'SeShutdownPrivilege'
        luid._obj.LowPart = 19
        return True

    def _adjust(self, _token, all_disabled, state, size, previous, returned):
        assert all_disabled is False
        if previous is not None:
            assert state._obj.PrivilegeCount == 1
            assert state._obj.Privileges[0].Attributes == 2
            assert size == ctypes.sizeof(shutdown._TOKEN_PRIVILEGES)
            previous._obj.PrivilegeCount = int(self.changed and not self.missing)
            previous._obj.Privileges[0].Attributes = 0
            self.error = 1300 if self.missing else 0
        else:
            assert state._obj.PrivilegeCount == 1
            assert state._obj.Privileges[0].Attributes == 0
        return True


class WindowsShutdownTests(unittest.TestCase):
    def run_with(self, api, function):
        with patch.object(shutdown, '_load_api', return_value=api):
            return function()

    def test_preflight_enables_then_restores_without_shutdown(self):
        api = FakeAPI()
        self.run_with(api, shutdown.check_shutdown_permission)
        self.assertEqual(api.adjust_privileges.call_count, 2)
        api.exit_windows.assert_not_called()
        api.open_token.assert_called_once()
        self.assertEqual(api.open_token.call_args.args[1], 0x28)
        api.close_handle.assert_called_once()

    def test_request_uses_only_poweroff_and_planned_application_reason(self):
        api = FakeAPI()
        self.run_with(api, shutdown.request_windows_shutdown)
        api.exit_windows.assert_called_once_with(8, 0x80040000)
        self.assertEqual(api.adjust_privileges.call_count, 2)
        api.close_handle.assert_called_once()

    def test_successful_adjust_but_not_all_assigned_must_fail(self):
        for function in (shutdown.check_shutdown_permission, shutdown.request_windows_shutdown):
            api = FakeAPI(missing=True)
            with self.assertRaisesRegex(PermissionError, '1300'):
                self.run_with(api, function)
            api.exit_windows.assert_not_called()
            api.close_handle.assert_called_once()

    def test_already_enabled_privilege_is_not_disabled(self):
        api = FakeAPI(changed=False)
        self.run_with(api, shutdown.check_shutdown_permission)
        self.assertEqual(api.adjust_privileges.call_count, 1)
        api.close_handle.assert_called_once()

    def test_api_failure_preserves_actual_error_and_restores_privilege(self):
        api = FakeAPI()
        def denied(*_):
            api.error = 1115  # shutdown already in progress, NOT permission denied.
            return False
        api.exit_windows.side_effect = denied
        with self.assertRaisesRegex(OSError, '1115') as caught:
            self.run_with(api, shutdown.request_windows_shutdown)
        self.assertNotIsInstance(caught.exception, PermissionError)
        self.assertEqual(api.adjust_privileges.call_count, 2)
        api.close_handle.assert_called_once()

    def test_open_token_denied_never_calls_shutdown_or_closes_invalid_handle(self):
        api = FakeAPI()
        api.open_token.side_effect = None
        api.open_token.return_value = False
        api.error = 5
        with self.assertRaises(PermissionError):
            self.run_with(api, shutdown.request_windows_shutdown)
        api.exit_windows.assert_not_called()
        api.close_handle.assert_not_called()

    def test_lookup_failure_closes_token_without_shutdown(self):
        api = FakeAPI()
        api.lookup_privilege.side_effect = None
        api.lookup_privilege.return_value = False
        api.error = 1313
        with self.assertRaisesRegex(OSError, '1313'):
            self.run_with(api, shutdown.request_windows_shutdown)
        api.exit_windows.assert_not_called()
        api.close_handle.assert_called_once()

    def test_restore_failure_is_not_silently_ignored(self):
        api = FakeAPI()
        original = api._adjust
        def adjust(*args):
            if args[4] is None:
                api.error = 5
                return False
            return original(*args)
        api.adjust_privileges.side_effect = adjust
        with self.assertRaisesRegex(PermissionError, '恢复关机权限'):
            self.run_with(api, shutdown.check_shutdown_permission)
        api.exit_windows.assert_not_called()
        api.close_handle.assert_called_once()

    def test_other_platform_is_rejected_before_loading_windows_dlls(self):
        with patch.object(shutdown.os, 'name', 'posix'):
            with self.assertRaisesRegex(OSError, '只支持Windows'):
                shutdown.check_shutdown_permission()


if __name__ == '__main__':
    unittest.main()
