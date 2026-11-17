"""No accounts, API requests, real documents or persistent user data."""
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZIP_DEFLATED, ZipFile

from model.OAuthLoopback import OAuthLoopbackServer
from model import OdsHelper as ods


class OAuthConnectionTests(unittest.TestCase):
    def test_every_accepted_peer_has_a_read_timeout(self):
        peer = Mock()
        server = object.__new__(OAuthLoopbackServer)
        with patch.object(HTTPServer, 'get_request', return_value=(peer, ('127.0.0.1', 1234))):
            self.assertEqual(server.get_request(), (peer, ('127.0.0.1', 1234)))
        peer.settimeout.assert_called_once_with(1.0)

    def test_failed_socket_setup_closes_connection(self):
        peer = Mock(); peer.settimeout.side_effect = OSError('fixture')
        with patch.object(HTTPServer, 'get_request', return_value=(peer, ('127.0.0.1', 1234))):
            with self.assertRaises(OSError): object.__new__(OAuthLoopbackServer).get_request()
        peer.close.assert_called_once()

    def test_whole_request_deadline_is_canceled_after_handler(self):
        server = object.__new__(OAuthLoopbackServer)
        with patch('model.OAuthLoopback.threading.Timer') as timer, patch.object(HTTPServer, 'process_request') as process:
            server.process_request(Mock(), ('127.0.0.1', 1))
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        process.assert_called_once()

    def test_silent_peer_does_not_block_handler_forever(self):
        # Windows socketpair opens a loopback connection, which the release
        # worker correctly forbids. Model the accepted socket's timeout here;
        # the get_request test separately verifies the real timeout is set.
        peer = Mock()
        reader = Mock(); reader.readline.side_effect = TimeoutError('silent peer')
        peer.makefile.return_value = reader
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
        started = time.monotonic()
        Callback(peer, ('127.0.0.1', 1), object())
        reader.readline.assert_called_once()
        self.assertLess(time.monotonic() - started, 2)

    def test_both_legacy_flows_explicitly_enable_pkce(self):
        import ast
        import inspect
        from model import GoogleDriveBrowser
        from app_plugins.builtin.document_formatter import google_auth
        for module in (GoogleDriveBrowser, google_auth):
            tree = ast.parse(inspect.getsource(module))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == 'from_client_config']
            self.assertEqual(len(calls), 1)
            self.assertTrue(any(k.arg == 'autogenerate_code_verifier' and isinstance(k.value, ast.Constant)
                                and k.value.value is True for k in calls[0].keywords))
            self.assertIs(module.OAuthLoopbackServer, OAuthLoopbackServer)

    def test_real_sdk_generates_s256_challenge_without_network(self):
        from google_auth_oauthlib.flow import InstalledAppFlow
        from urllib.parse import urlsplit, parse_qs
        client = {'installed': {'client_id': 'fixture', 'client_secret': 'fixture',
                  'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                  'token_uri': 'https://oauth2.googleapis.com/token'}}
        flow = InstalledAppFlow.from_client_config(client, ['openid'], autogenerate_code_verifier=True)
        url, state = flow.authorization_url()
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertTrue(flow.code_verifier); self.assertTrue(state)


class TaskArchiveTests(unittest.TestCase):
    def archive(self, content):
        value = io.BytesIO()
        with ZipFile(value, 'w', ZIP_DEFLATED) as z: z.writestr('content.xml', content)
        return value.getvalue()

    def test_oversized_bytes_rejected_before_parser(self):
        with patch.object(ods, 'MAX_TASK_ODS_BYTES', 16), patch.object(ods, 'load') as load:
            with self.assertRaisesRegex(ValueError, '32MiB'):
                ods._load_task_document('unused', b'x' * 17)
        load.assert_not_called()

    def test_small_compressed_archive_cannot_expand_without_limit(self):
        value = self.archive(b'x' * 2048)
        with patch.object(ods, 'MAX_TASK_ODS_EXPANDED_BYTES', 1024), patch.object(ods, 'load') as load:
            with self.assertRaisesRegex(ValueError, '128MiB'):
                ods._load_task_document('unused', value)
        load.assert_not_called()

    def test_validated_snapshot_is_the_parsed_snapshot(self):
        value = self.archive(b'<fixture/>')
        with patch.object(ods, 'load', side_effect=lambda stream: stream.read()) as load:
            self.assertEqual(ods._load_task_document('unused', value), value)
        load.assert_called_once()

    def test_file_and_byte_paths_have_same_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.ods'; path.write_bytes(b'x' * 17)
            with patch.object(ods, 'MAX_TASK_ODS_BYTES', 16), patch.object(ods, 'load') as load:
                with self.assertRaisesRegex(ValueError, '32MiB'): ods._load_task_document(path)
        load.assert_not_called()

    def test_non_zip_rejected_before_odf_parser(self):
        from zipfile import BadZipFile
        with patch.object(ods, 'load') as load:
            with self.assertRaises(BadZipFile): ods._load_task_document('unused', b'not a zip')
        load.assert_not_called()


if __name__ == '__main__': unittest.main()
