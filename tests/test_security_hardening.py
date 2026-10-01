"""No network/credential access: regression coverage for security boundaries."""
import ast
from email.message import Message
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from urllib.request import Request

from model.AppLogger import RedactingFormatter
from model.GoogleDriveDownloader import (allowed_download_url, GoogleDriveRedirectHandler,
    DownloadError, open_download_response, safe_filename)
from model.SensitiveData import redact_sensitive_text, register_sensitive_values
from model.VideoCompressor import read_shana_preset
from defusedxml.common import DTDForbidden

ROOT = Path(__file__).resolve().parents[1]


class SecurityHardeningTests(unittest.TestCase):
    def test_feature_adapter_preserves_tensor_and_rejects_missing_projection(self):
        from model.ModelFeatures import feature_tensor
        tensor = object()
        self.assertIs(feature_tensor(tensor), tensor)
        self.assertIs(feature_tensor(SimpleNamespace(pooler_output=tensor)), tensor)
        with self.assertRaises(RuntimeError):
            feature_tensor(SimpleNamespace(pooler_output=None))

    def test_download_accepts_only_google_https(self):
        for url in ('https://drive.google.com/uc?id=abc', 'https://docs.google.com/document/d/abc/export',
                    'https://drive.usercontent.google.com/download?id=abc',
                    'https://doc-123.googleusercontent.com/file', 'https://accounts.google.com/login'):
            self.assertTrue(allowed_download_url(url), url)
        for url in ('http://drive.google.com/x', 'https://localhost/', 'https://127.0.0.1/',
                    'file:///C:/config.json', 'https://drive.google.com.evil.test/x',
                    'https://evilgoogleusercontent.com/x', 'https://user:password@drive.google.com/x',
                    'https://drive.google.com:8443/x', 'https://drive.google.com:bad/x'):
            self.assertFalse(allowed_download_url(url), url)

    def test_redirect_is_checked_before_following(self):
        handler = GoogleDriveRedirectHandler()
        request = Request('https://drive.google.com/uc?id=abc')
        with self.assertRaises(DownloadError):
            handler.redirect_request(request, None, 302, '', {}, 'http://127.0.0.1/private')
        result = handler.redirect_request(request, None, 302, '', {},
                                            'https://drive.usercontent.google.com/download?id=abc')
        self.assertIn('google.com', result.full_url)

    def test_confirmation_form_cannot_fetch_local_network(self):
        response = Mock()
        response.headers = Message()
        response.headers['Content-Type'] = 'text/html'
        response.read.return_value = b'<form id="download-form" action="http://127.0.0.1/private"><input name="id" value="abc"></form>'
        opener = Mock()
        opener.open.return_value = response
        with self.assertRaises(DownloadError):
            open_download_response(opener, 'https://drive.google.com/uc?id=abc', 10)
        self.assertEqual(opener.open.call_count, 1)
        response.close.assert_called_once()

    def test_download_filename_stays_a_basename(self):
        for name in ('../../secret', '..\\..\\secret', 'C:\\Windows\\secret', 'CON', 'NUL.txt'):
            safe, _ = safe_filename(name, 'fallback')
            self.assertNotIn('/', safe)
            self.assertNotIn('\\', safe)
            self.assertNotIn(':', safe)
            self.assertEqual(Path(safe).name, safe)

    def test_redacts_arguments_headers_lists_and_exception_text(self):
        secret = 'private-test-credential-12345'
        register_sensitive_values([secret])
        try:
            raise ValueError('upstream echoed ' + secret)
        except ValueError:
            record = logging.LogRecord('test', logging.ERROR, __file__, 1,
                'api_key=%s Authorization: Bearer abcdefghijklmnopqrstuvwxyz', (secret,), sys.exc_info())
        output = RedactingFormatter('%(message)s').format(record)
        self.assertNotIn(secret, output)
        self.assertNotIn('abcdefghijklmnopqrstuvwxyz', output)
        self.assertIn('ValueError', output)
        self.assertIn('Traceback', output)
        for text in ('{"gemini_api_keys":["one-secret", "two-secret"]}',
                     'refresh_token="refresh-secret"', "client_secret='oauth-secret'"):
            output = redact_sensitive_text(text)
            self.assertNotIn('-secret', output)
        self.assertEqual(redact_sensitive_text('normal diagnostic: 17 files'), 'normal diagnostic: 17 files')

    def test_normal_preset_works_but_dtd_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'preset.xml'
            path.write_text('<root><prefixtextBox>[SHANA]</prefixtextBox><encparamBox>-c:v libx264</encparamBox></root>')
            self.assertEqual(read_shana_preset(path), ('[SHANA]', '', '', '-c:v libx264'))
            path.write_text('<!DOCTYPE root [<!ENTITY x "expanded">]><root><prefixtextBox>&x;</prefixtextBox></root>')
            with self.assertRaises(DTDForbidden):
                read_shana_preset(path)

    def test_directory_opening_does_not_use_a_shell(self):
        tree = ast.parse((ROOT/'QtPlus'/'MyTreeView.py').read_text(encoding='utf-8-sig'))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertFalse(any(isinstance(node.func, ast.Attribute) and node.func.attr in ('system','popen')
                             and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os' for node in calls))
        popen = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == 'Popen']
        self.assertEqual(len(popen), 2)
        self.assertTrue(all(isinstance(node.args[0], ast.List) for node in popen))

    def test_actions_use_immutable_refs_and_no_checkout_credentials(self):
        import re
        for path in (ROOT/'.github'/'workflows').glob('*.yml'):
            text = path.read_text()
            for ref in re.findall(r'uses:\s*([^\s]+)', text):
                if ref.startswith('./'):
                    # A local reusable workflow is pinned to the caller commit.
                    self.assertEqual(ref, './.github/workflows/codeql.yml')
                    self.assertTrue((ROOT / ref).is_file())
                    continue
                self.assertRegex(ref, r'^[\w./-]+@[0-9a-f]{40}$')
            self.assertNotIn('pull_request_target:', text)
            self.assertIn('persist-credentials: false', text)

    def test_codeql_is_reusable_and_runs_after_release_publication(self):
        codeql = (ROOT / '.github/workflows/codeql.yml').read_text()
        release = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertIn('  workflow_call:', codeql)
        self.assertIn('  workflow_dispatch:', codeql)
        self.assertIn('needs: build-and-release', release)
        self.assertIn('uses: ./.github/workflows/codeql.yml', release)
        self.assertIn('security-events: write', release)


if __name__ == '__main__':
    unittest.main()
