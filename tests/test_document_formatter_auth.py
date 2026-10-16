"""No real OAuth, browser or network requests are performed by these tests."""
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_plugins.builtin.document_formatter import google_auth
from model.BackgroundTasks import BackgroundTaskCanceled

CLIENT = {'installed': {'client_id': 'fixture-client', 'client_secret': 'fixture-secret',
                       'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                       'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': ['http://localhost']}}


class FakeServer:
    def __init__(self, address, handler): self.address, self.handler, self.server_port = address, handler, 12345
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def handle_request(self):
        callback = object.__new__(self.handler)
        callback.path = '/?code=fixture-code&state=fixture-state'
        callback.send_response, callback.send_header, callback.end_headers = Mock(), Mock(), Mock()
        callback.wfile = io.BytesIO(); callback.do_GET()


class DocsAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.client = self.root/'client-original.json'
        self.client.write_text(json.dumps(CLIENT), encoding='utf-8'); self.private = self.root/'private'
        self.unrelated = self.root/'GoogleSheetsToken.json'; self.unrelated.write_text('other-service-unchanged')
        google_auth.configure_client(self.client, self.private)
        self.run = SimpleNamespace(check_canceled=lambda: None, report=Mock())

    def test_configure_client_keeps_original_and_other_service_files(self):
        self.assertEqual(json.loads(self.client.read_text()), CLIENT)
        self.assertEqual(json.loads((self.private/'google_client.json').read_text()), CLIENT)
        self.assertEqual(self.unrelated.read_text(), 'other-service-unchanged')
        self.assertEqual(google_auth.SCOPES, ['https://www.googleapis.com/auth/documents'])

    def test_unknown_auth_server_web_client_and_service_account_rejected(self):
        values = [{}, {'web': CLIENT['installed']}, {**CLIENT, 'installed': {**CLIENT['installed'], 'token_uri': 'https://evil.example/token'}},
                  {**CLIENT, 'installed': {**CLIENT['installed'], 'auth_uri': 'https://evil.example/auth'}}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError): google_auth.client_config(value)

    def test_authorize_uses_cancelable_loopback_and_private_token_only(self):
        flow = Mock(); flow.authorization_url.return_value = ('https://accounts.google.com/fixture', 'fixture-state')
        flow.credentials.to_json.return_value = json.dumps({'token': 'fixture-token', 'token_uri': 'https://oauth2.googleapis.com/token'})
        with (patch.object(google_auth, 'HTTPServer', FakeServer), patch.object(google_auth.webbrowser, 'open', return_value=True) as browser,
              patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config', return_value=flow)):
            result = google_auth.authorize(self.private, self.run)
        self.assertEqual(result['kind'], 'auth'); self.assertEqual(flow.redirect_uri, 'http://127.0.0.1:12345/')
        flow.fetch_token.assert_called_once_with(authorization_response='https://127.0.0.1:12345/?code=fixture-code&state=fixture-state', timeout=45)
        self.assertEqual(json.loads((self.private/'google_token.json').read_text())['token'], 'fixture-token')
        self.assertEqual(self.unrelated.read_text(), 'other-service-unchanged')
        browser.assert_called_once()

    def test_canceled_login_preserves_existing_docs_token(self):
        token = self.private/'google_token.json'; token.write_text('original-docs-token')
        flow = Mock(); flow.authorization_url.return_value = ('https://accounts.google.com/fixture', 'fixture-state')
        run = SimpleNamespace(check_canceled=Mock(side_effect=BackgroundTaskCanceled()), report=Mock())
        with (patch.object(google_auth, 'HTTPServer', FakeServer), patch.object(google_auth.webbrowser, 'open', return_value=True),
              patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config', return_value=flow)):
            with self.assertRaises(BackgroundTaskCanceled): google_auth.authorize(self.private, run)
        self.assertEqual(token.read_text(), 'original-docs-token'); flow.fetch_token.assert_not_called()

    def test_load_without_token_or_wrong_token_server_never_builds_service(self):
        with patch('googleapiclient.discovery.build') as build:
            with self.assertRaises(ValueError): google_auth.load_service(self.private)
            (self.private/'google_token.json').write_text(json.dumps({'token_uri': 'https://evil.example/token'}))
            with self.assertRaises(ValueError): google_auth.load_service(self.private)
        build.assert_not_called()

    def test_offline_authorize_never_opens_browser(self):
        with patch.object(google_auth, 'require_online', side_effect=RuntimeError('offline')), patch.object(google_auth.webbrowser, 'open') as browser:
            with self.assertRaises(RuntimeError): google_auth.authorize(self.private, self.run)
        browser.assert_not_called()

    def test_insufficient_scope_and_changed_client_never_refresh_or_build(self):
        values = [{'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': ['https://www.googleapis.com/auth/spreadsheets']},
                  {'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': google_auth.SCOPES, 'client_id': 'other-client'}]
        with patch('googleapiclient.discovery.build') as build, patch('google.oauth2.credentials.Credentials.from_authorized_user_info') as credentials:
            for value in values:
                (self.private/'google_token.json').write_text(json.dumps(value))
                with self.assertRaises(ValueError): google_auth.load_service(self.private)
        build.assert_not_called(); credentials.assert_not_called()

    def test_valid_independent_docs_token_builds_only_docs_without_network_refresh(self):
        value = {'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': google_auth.SCOPES, 'client_id': 'fixture-client',
                 'client_secret': 'fixture-secret', 'refresh_token': 'fixture-refresh', 'token': 'fixture-token', 'expiry': '2099-01-01T00:00:00Z'}
        (self.private/'google_token.json').write_text(json.dumps(value))
        with patch('googleapiclient.discovery.build', return_value='service') as build:
            self.assertEqual(google_auth.load_service(self.private), 'service')
        self.assertEqual(build.call_args.args, ('docs', 'v1')); self.assertFalse(build.call_args.kwargs['cache_discovery'])
        self.assertEqual(self.unrelated.read_text(), 'other-service-unchanged')


if __name__ == '__main__': unittest.main()
