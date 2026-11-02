"""Temporary stores, synthetic credentials, mocked network, real Windows DPAPI."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model.AppDataStorage import ApplicationStorage
from model.GoogleAccounts import GoogleAccountManager, SCOPES, load_managed_service, validate_client
from model.GoogleAccountAuth import authorize
from model.BackgroundTasks import BackgroundTaskCanceled
from model import GoogleAccountAuth as auth

CLIENT = {'installed': {'client_id': 'fixture-client', 'client_secret': 'fixture-secret',
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token'}}
IDENTITY = {'sub': 'subject-one', 'email': 'fixture@example.test', 'email_verified': True}


def token(features=('sheets', 'drive')):
    return {'client_id': 'fixture-client', 'client_secret': 'fixture-secret', 'refresh_token': 'fixture-refresh',
            'token': 'fixture-access', 'expiry': '2099-01-01T00:00:00Z',
            'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': [SCOPES[key] for key in features]}


@unittest.skipUnless(os.name == 'nt', 'Production credential encryption uses Windows current-user DPAPI')
class GoogleAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = GoogleAccountManager(ApplicationStorage(self.root))
        self.client = self.manager.add_client(CLIENT, 'Fixture client')

    def login(self, identity=None, features=('sheets', 'drive')):
        return self.manager.save_login(self.client, identity if identity is not None else IDENTITY, token(features))

    def test_real_dpapi_roundtrip_no_plaintext_or_temp_leaks(self):
        self.login(); raw = self.manager.file.path.read_bytes()
        for secret in ('fixture-secret', 'fixture-refresh', 'fixture-access', 'fixture@example.test', 'subject-one'):
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(self.manager.snapshot()['accounts'][0]['email'], IDENTITY['email'])
        self.assertEqual([path.name for path in self.root.iterdir()], ['GoogleAccounts.json'])

    def test_snapshot_never_exposes_clients_or_tokens(self):
        self.login(); result = json.dumps(self.manager.snapshot())
        for secret in ('fixture-secret', 'fixture-refresh', 'fixture-access', 'token_uri'):
            self.assertNotIn(secret, result)

    def test_two_accounts_and_feature_bindings_are_independent(self):
        first = self.login()
        second = self.login({**IDENTITY, 'sub': 'two', 'email': 'second@example.test'}, ('docs',))
        self.manager.bind_many({'sheets': first, 'drive': first, 'docs': second})
        self.assertEqual(self.manager.bound_account('docs')[0], second)
        self.assertEqual(self.manager.bound_account('drive')[0], first)
        self.assertEqual(len(self.manager.snapshot()['accounts']), 2)

    def test_relogin_same_subject_keeps_id_not_guess_by_email(self):
        first = self.login()
        self.assertEqual(self.login({**IDENTITY, 'email': 'renamed@example.test'}), first)
        self.assertNotEqual(self.login({**IDENTITY, 'sub': 'different'}), first)

    def test_unverified_or_missing_identity_not_saved(self):
        before = self.manager.file.path.read_bytes()
        for identity in ({}, {**IDENTITY, 'email_verified': False}, {**IDENTITY, 'sub': ''}):
            with self.assertRaises(ValueError): self.login(identity)
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_bad_client_servers_and_types_rejected(self):
        for value in ({}, {'web': CLIENT['installed']}, {'installed': {**CLIENT['installed'], 'token_uri': 'https://evil.test'}},
                      {'installed': {**CLIENT['installed'], 'auth_uri': 'http://evil.test'}}):
            with self.assertRaises(ValueError): validate_client(value)

    def test_corrupt_vault_not_replaced_or_fallen_back(self):
        self.manager.file.path.write_text('broken', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '未覆盖'): self.manager.add_client(CLIENT, 'other')
        with self.assertRaises(ValueError): load_managed_service('drive', manager=self.manager)
        self.assertEqual(self.manager.file.path.read_text(), 'broken')

    def test_decrypt_failure_never_resets(self):
        before = self.manager.file.path.read_bytes()
        with patch('model.GoogleAccounts._dpapi', side_effect=RuntimeError('fixture-secret')):
            with self.assertRaises(ValueError) as caught: self.manager.snapshot()
        self.assertNotIn('fixture-secret', str(caught.exception))
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_encryption_failure_writes_nothing(self):
        before = self.manager.file.path.read_bytes()
        with patch('model.GoogleAccounts._dpapi', side_effect=RuntimeError('fail')):
            # Simulate encryption failing only, after a successful read.
            state = {'version': 1, 'clients': {}, 'accounts': {}, 'bindings': {}}
            with self.assertRaises(RuntimeError): self.manager._write(state)
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_storage_loss_never_falls_back_to_another_directory(self):
        guard = SimpleNamespace(check=Mock(side_effect=OSError('volume missing')))
        broken = GoogleAccountManager(ApplicationStorage(self.root, guard=guard))
        with self.assertRaises(OSError): broken.snapshot()

    def test_binding_insufficient_permission_is_atomic(self):
        key = self.login(features=('sheets',)); before = self.manager.file.path.read_bytes()
        with self.assertRaises(ValueError): self.manager.bind_many({'sheets': key, 'drive': key})
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_remove_clears_only_own_bindings(self):
        first = self.login(); second = self.login({**IDENTITY, 'sub': 'two'})
        self.manager.bind_many({'sheets': first, 'drive': second})
        self.manager.remove(first)
        with self.assertRaisesRegex(ValueError, '已停用'): self.manager.bound_account('sheets')
        self.assertEqual(self.manager.bound_account('drive')[0], second)

    def test_old_refresh_cannot_resurrect_removed_account(self):
        key = self.login(); self.manager.bind('sheets', key)
        revision = self.manager.bound_account('sheets')[1]['revision']
        self.manager.remove(key)
        with self.assertRaises(ValueError): self.manager.save_refreshed(key, revision, token())
        self.assertEqual(self.manager.snapshot()['accounts'], [])

    def test_old_refresh_cannot_overwrite_new_login(self):
        key = self.login(); self.manager.bind('sheets', key)
        old = self.manager.bound_account('sheets')[1]['revision']
        self.login()
        before = self.manager.file.path.read_bytes()
        with self.assertRaises(ValueError): self.manager.save_refreshed(key, old, token())
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_relogin_cannot_silently_remove_bound_permissions(self):
        key = self.login(); self.manager.bind('drive', key)
        with self.assertRaisesRegex(ValueError, '已绑定'): self.login(features=('sheets',))
        self.assertEqual(self.manager.bound_account('drive')[0], key)

    def test_wrong_token_server_or_client_not_saved(self):
        for updates in ({'token_uri': 'https://evil.test'}, {'client_id': 'other'}, {'refresh_token': ''}):
            with self.assertRaises(ValueError): self.manager.save_login(self.client, IDENTITY, {**token(), **updates})

    def test_unbound_service_stays_legacy_without_google_request(self):
        with patch('googleapiclient.discovery.build') as build:
            self.assertIsNone(load_managed_service('drive', manager=self.manager))
        build.assert_not_called()

    def test_managed_service_uses_correct_api_and_timeout_without_browser(self):
        key = self.login(); self.manager.bind('drive', key)
        with patch('googleapiclient.discovery.build', return_value='managed') as build, patch('webbrowser.open') as browser:
            self.assertEqual(load_managed_service('drive', manager=self.manager), 'managed')
        self.assertEqual(build.call_args.args, ('drive', 'v3'))
        self.assertFalse(build.call_args.kwargs['cache_discovery']); browser.assert_not_called()
        self.assertEqual(build.call_args.kwargs['http'].http.timeout, 30)

    def test_refresh_error_is_redacted_and_never_opens_browser(self):
        key = self.manager.save_login(self.client, IDENTITY, {**token(), 'expiry': '2000-01-01T00:00:00Z'})
        self.manager.bind('sheets', key)
        with patch('google.oauth2.credentials.Credentials.refresh', side_effect=RuntimeError('fixture-refresh')), patch('webbrowser.open') as browser:
            with self.assertRaises(RuntimeError) as caught: load_managed_service('sheets', manager=self.manager)
        self.assertNotIn('fixture-refresh', str(caught.exception)); browser.assert_not_called()

    def test_legacy_loaders_delegate_before_legacy_paths(self):
        from model import GoogleDriveHelper
        from model.GoogleSheetsHelper import load_sheets_service
        from model.GoogleDriveHelper import load_drive_service
        from app_plugins.builtin.document_formatter.google_auth import load_service
        with (patch('model.GoogleAccounts.load_managed_service', return_value='managed') as service,
              patch.object(GoogleDriveHelper, 'TOKEN_FILE', self.root/'missing-token'),
              patch.object(GoogleDriveHelper, 'CLIENT_SECRET_FILE', self.root/'missing-client')):
            self.assertEqual(load_sheets_service({'review_sheet_credentials_file': str(self.root/'missing-client'),
                                                  'review_sheet_token_file': str(self.root/'missing-token')}), 'managed')
            self.assertEqual(load_drive_service(), 'managed')
            self.assertEqual(load_service(self.root/'absent'), 'managed')
        self.assertEqual([call.args[0] for call in service.call_args_list], ['sheets', 'drive', 'docs'])

    def test_authorize_offline_or_canceled_does_not_open_browser(self):
        run = SimpleNamespace(check_canceled=Mock(side_effect=BackgroundTaskCanceled()), report=Mock())
        with patch('webbrowser.open') as browser:
            with self.assertRaises(BackgroundTaskCanceled): authorize({'client_id': self.client, 'features': ['sheets']}, run, manager=self.manager)
        browser.assert_not_called()
        with patch('model.GoogleAccountAuth.require_online', side_effect=PermissionError('offline')), patch('webbrowser.open') as browser:
            with self.assertRaises(PermissionError): authorize({}, run, manager=self.manager)
        browser.assert_not_called()

    def oauth(self, *, callback_state='fixture-state', granted=None, cancel=False, requested=None):
        import io
        flow = Mock()
        flow.authorization_url.return_value = ('https://accounts.google.com/fixture', 'fixture-state')
        value = token() if requested is None else {**token(), 'scopes': requested}
        flow.credentials.to_json.return_value = json.dumps(value)
        flow.credentials.granted_scopes = granted if granted is not None else (list(SCOPES.values()) if requested is None else requested)
        flow.credentials.token = 'fixture-access'
        class FakeServer:
            def __init__(self, address, handler): self.address, self.handler, self.server_port = address, handler, 12345
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def handle_request(self):
                callback = object.__new__(self.handler)
                callback.path = '/google-auth/fixture-path?code=fixture-code&state=' + callback_state
                callback.send_response, callback.send_header, callback.end_headers = Mock(), Mock(), Mock()
                callback.wfile = io.BytesIO(); callback.do_GET()
        run = SimpleNamespace(check_canceled=Mock(), report=Mock())
        if cancel: run.check_canceled.side_effect = [None, BackgroundTaskCanceled()]
        patches = [patch.object(auth, 'HTTPServer', FakeServer), patch.object(auth.secrets, 'token_urlsafe', return_value='fixture-path'),
                   patch.object(auth.webbrowser, 'open', return_value=True), patch.object(auth, '_identity', return_value=IDENTITY),
                   patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config', return_value=flow),
                   patch.object(auth.time, 'monotonic', side_effect=[0, 0, 181])]
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in patches]
            payload = {'client_id': self.client, 'features': ['sheets', 'drive']} if requested is None else {'client_id': self.client, 'scopes': requested}
            result = authorize(payload, run, manager=self.manager)
        return result, flow, mocks, run

    def test_oauth_pkce_verified_identity_saved_after_state_callback(self):
        result, flow, mocks, run = self.oauth()
        self.assertIn('account_id', result)
        self.assertTrue(mocks[4].call_args.kwargs['autogenerate_code_verifier'])
        self.assertEqual(flow.redirect_uri, 'http://127.0.0.1:12345/google-auth/fixture-path')
        self.assertEqual(flow.authorization_url.call_args.kwargs['prompt'], 'consent select_account')
        self.assertEqual(flow.fetch_token.call_args.kwargs['timeout'], 30)
        self.assertIn('openid', mocks[4].call_args.args[1])
        self.assertEqual(len(self.manager.snapshot()['accounts']), 1)

    def test_oauth_new_registered_scope_does_not_require_fixed_feature_name(self):
        scope = 'https://www.googleapis.com/auth/tasks'
        result, flow, mocks, run = self.oauth(requested=[scope])
        self.assertIn(scope, mocks[4].call_args.args[1])
        self.assertIn(scope, self.manager.account(result['account_id'])['credentials']['scopes'])

    def test_oauth_rejects_arbitrary_scope_server_without_browser(self):
        with patch('webbrowser.open') as browser:
            with self.assertRaisesRegex(ValueError, '权限声明无效'):
                authorize({'client_id': self.client, 'scopes': ['https://evil.test/auth/tasks']}, Mock(), manager=self.manager)
        browser.assert_not_called()

    def test_invalid_callback_state_eventually_times_out_without_saving(self):
        before = self.manager.file.path.read_bytes()
        with self.assertRaises(TimeoutError): self.oauth(callback_state='wrong-state')
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_partial_scope_grant_cannot_masquerade_as_full_grant(self):
        before = self.manager.file.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '全部权限'): self.oauth(granted=[SCOPES['sheets']])
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_cancel_oauth_preserves_existing_accounts(self):
        self.login(); before = self.manager.file.path.read_bytes()
        with self.assertRaises(BackgroundTaskCanceled): self.oauth(cancel=True)
        self.assertEqual(before, self.manager.file.path.read_bytes())

    def test_identity_request_uses_timeout_and_rejects_unverified_account(self):
        response = Mock(); response.json.return_value = {**IDENTITY, 'email_verified': False}
        with patch('requests.get', return_value=response) as get:
            with self.assertRaises(ValueError): auth._identity(SimpleNamespace(token='fixture-access'))
        self.assertEqual(get.call_args.kwargs['timeout'], (10, 30))

    def test_refresh_update_survives_reopening_manager(self):
        key = self.login(); self.manager.bind('sheets', key)
        revision = self.manager.bound_account('sheets')[1]['revision']
        self.manager.save_refreshed(key, revision, {**token(), 'token': 'new-fixture-token'})
        other = GoogleAccountManager(ApplicationStorage(self.root))
        self.assertEqual(other.bound_account('sheets')[1]['credentials']['token'], 'new-fixture-token')

    def test_concurrent_transactions_keep_both_accounts(self):
        import concurrent.futures
        def add(index):
            return self.login({**IDENTITY, 'sub': str(index), 'email': str(index) + '@example.test'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(add, range(12)))
        self.assertEqual(len(self.manager.snapshot()['accounts']), 12)

    def test_removed_binding_cannot_fall_back_to_legacy_account(self):
        key = self.login(); self.manager.bind('drive', key); self.manager.remove(key)
        with patch('googleapiclient.discovery.build') as build:
            with self.assertRaisesRegex(ValueError, '不会回退'): load_managed_service('drive', manager=self.manager)
        build.assert_not_called()

    def test_explicit_legacy_selection_is_distinct_from_disabled(self):
        from model.GoogleAccounts import DISABLED
        self.manager.bind('sheets', DISABLED)
        with self.assertRaises(ValueError): load_managed_service('sheets', manager=self.manager)
        self.manager.bind('sheets', '')
        self.assertIsNone(load_managed_service('sheets', manager=self.manager))

    def test_other_process_lock_fails_fast_without_lost_update(self):
        import subprocess
        import sys
        script = ('import sys; from model.GoogleAccounts import GoogleAccountManager; '
                  'from model.AppDataStorage import ApplicationStorage; '
                  'm=GoogleAccountManager(ApplicationStorage(sys.argv[1])); '
                  'm.bind("sheets", "")')
        before = self.manager.file.path.read_bytes()
        from model import AppDataStorage
        with self.manager._transaction():
            result = subprocess.run([sys.executable, '-c', script, str(self.root)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
                encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1',
                    'PYTHONPATH': str(Path(__file__).resolve().parents[1]) + os.pathsep
                                  + str(Path(AppDataStorage.__file__).resolve().parents[1])})
        self.assertNotEqual(result.returncode, 0)
        # Overlay root includes the new module; deployed check uses real root.
        self.assertIn('未等待或覆盖', result.stderr)
        self.assertEqual(before, self.manager.file.path.read_bytes())


if __name__ == '__main__': unittest.main()
