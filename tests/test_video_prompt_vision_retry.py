"""No external API calls: exercise real urllib requests and Qt analyze slot."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.video_prompt_assistant.cases import PromptCaseStore
from app_plugins.builtin.video_prompt_assistant.ui import VideoPromptDialog
from app_plugins.builtin.video_prompt_assistant.vision import analyze_image, SYSTEM_PROMPT


def response(content=None):
    scene = content if content is not None else {'scene': 'person', 'subject': '画面中的人物',
                                              'has_text': False, 'observation': '静态人物'}
    data = {'candidates': [{'content': {'parts': [{'text': json.dumps(scene)}]}}]}
    return io.BytesIO(json.dumps(data).encode('utf8'))


def http_error(code=429, body=b'{"error":{"message":"retry in 4.5s"}}', headers=None):
    return urllib.error.HTTPError('https://generativelanguage.googleapis.com/synthetic',
                                 code, 'synthetic error', headers, io.BytesIO(body))


class VisionRetryTests(unittest.TestCase):
    def manager(self, keys=None):
        manager = Mock()
        manager.request_keys.return_value = keys or ['synthetic-first', 'synthetic-second']
        manager.is_available.return_value = True
        return manager

    def assert_request(self, request, key, image=b'synthetic-jpeg', model='gemini-2.5-flash'):
        payload = json.loads(request.data)
        self.assertIsInstance(payload, dict, 'A later Key must receive the original JSON object, not an error string')
        self.assertEqual(payload['contents'][0]['parts'][0]['text'], SYSTEM_PROMPT)
        self.assertEqual(payload['contents'][0]['parts'][1]['inline_data']['data'],
                         base64.b64encode(image).decode('ascii'))
        self.assertEqual(payload['generationConfig']['response_mime_type'], 'application/json')
        self.assertEqual(request.get_header('X-goog-api-key'), key)
        self.assertEqual(request.method, 'POST')
        self.assertIn(model + ':generateContent', request.full_url)

    def test_http_error_does_not_replace_the_next_keys_image_request(self):
        for code in (403, 429, 500, 503):
            with self.subTest(code=code):
                manager = self.manager()
                error_body = b'{"error":{"message":"synthetic quota failure"}}'
                with patch('urllib.request.urlopen', side_effect=[
                    http_error(code, error_body, {'Retry-After': '7'}), response()
                ]) as opening:
                    result = analyze_image(b'synthetic-jpeg', gemini_keys=manager)
                self.assertEqual(result['scene'], 'person')
                self.assertEqual(opening.call_count, 2)
                for call, key in zip(opening.call_args_list, manager.request_keys.return_value):
                    self.assert_request(call.args[0], key)
                    self.assertEqual(call.kwargs['timeout'], 90)
                self.assertEqual(opening.call_args_list[0].args[0].data,
                                 opening.call_args_list[1].args[0].data)
                manager.report_failure.assert_called_once_with('synthetic-first', 'gemini-2.5-flash',
                                                               code, '7', error_body.decode())
                manager.report_success.assert_called_once_with('synthetic-second', 'gemini-2.5-flash')

    def test_three_keys_and_retry_after_from_error_body_keep_request_immutable(self):
        manager = self.manager(['synthetic-first', 'synthetic-second', 'synthetic-third'])
        with patch('urllib.request.urlopen', side_effect=[http_error(), http_error(503), response()]) as opening:
            analyze_image(b'synthetic-jpeg', gemini_keys=manager)
        self.assertEqual(opening.call_count, 3)
        self.assertEqual({call.args[0].data for call in opening.call_args_list},
                         {opening.call_args_list[0].args[0].data})
        self.assert_request(opening.call_args_list[2].args[0], 'synthetic-third')
        self.assertEqual(manager.report_failure.call_args_list[0].args[3], '4.5')

    def test_network_error_then_http_error_still_allows_last_key(self):
        manager = self.manager(['synthetic-first', 'synthetic-second', 'synthetic-third'])
        with patch('urllib.request.urlopen', side_effect=[urllib.error.URLError('synthetic'),
                                                         http_error(), response()]) as opening:
            analyze_image(b'synthetic-jpeg', gemini_keys=manager)
        self.assert_request(opening.call_args_list[-1].args[0], 'synthetic-third')
        self.assertEqual(manager.report_failure.call_count, 2)

    def test_all_failures_have_no_raw_body_credentials_or_image_in_error(self):
        manager = self.manager()
        body = b'synthetic-sensitive-server-detail'
        with patch('urllib.request.urlopen', side_effect=[http_error(429, body), http_error(403, body)]):
            with self.assertRaises(RuntimeError) as raised:
                analyze_image(b'synthetic-jpeg', gemini_keys=manager)
        message = str(raised.exception)
        self.assertIn('Key 1: HTTP 429', message)
        self.assertIn('Key 2: HTTP 403', message)
        for private in ('synthetic-sensitive', 'synthetic-first', 'synthetic-second', 'synthetic-jpeg'):
            self.assertNotIn(private, message)
        manager.report_success.assert_not_called()

    def test_explicit_keys_without_manager_still_work(self):
        with patch('urllib.request.urlopen', side_effect=[http_error(), response()]) as opening:
            self.assertEqual(analyze_image(b'synthetic-jpeg', ['synthetic-first', 'synthetic-second'])['scene'], 'person')
        self.assert_request(opening.call_args_list[-1].args[0], 'synthetic-second')

    def test_unavailable_key_is_skipped_without_request(self):
        manager = self.manager()
        manager.is_available.side_effect = [False, True]
        with patch('urllib.request.urlopen', return_value=response()) as opening:
            analyze_image(b'synthetic-jpeg', gemini_keys=manager)
        self.assertEqual(opening.call_count, 1)
        self.assert_request(opening.call_args.args[0], 'synthetic-second')

    def test_empty_broker_does_not_open_network(self):
        manager = self.manager()
        manager.request_keys.return_value = []
        manager.unavailable_message.return_value = 'synthetic unavailable'
        with patch('urllib.request.urlopen') as opening:
            with self.assertRaisesRegex(ValueError, 'synthetic unavailable'):
                analyze_image(b'synthetic-jpeg', gemini_keys=manager)
        opening.assert_not_called()

    def test_real_key_broker_keeps_cooldown_without_changing_retry_payload(self):
        from model.GeminiKeyManager import GeminiKeyManager
        manager = GeminiKeyManager({'gemini_api_keys': ['synthetic-first', 'synthetic-second']},
                                   clock=lambda: 1000.0)
        with patch('urllib.request.urlopen', side_effect=[
            http_error(429, headers={'Retry-After': '30'}), response()
        ]) as opening:
            result = analyze_image(b'synthetic-jpeg', gemini_keys=manager)
        self.assertEqual(result['scene'], 'person')
        self.assert_request(opening.call_args_list[-1].args[0], 'synthetic-second')
        self.assertFalse(manager.is_available('synthetic-first', 'gemini-2.5-flash'))
        self.assertTrue(manager.is_available('synthetic-second', 'gemini-2.5-flash'))


class AnalyzeContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = SimpleNamespace(load_config=lambda: {}, log=lambda _msg: None)
        self.dialog = VideoPromptDialog(self.context, store=PromptCaseStore(Path(self.temp.name) / 'cases.json'))
        self.dialog.image_jpeg = b'synthetic-jpeg'
        self.addCleanup(self.dialog.deleteLater)

    def test_missing_manager_or_none_gives_guidance_without_worker_or_upload(self):
        for explicit_none in (False, True):
            with self.subTest(explicit_none=explicit_none):
                if explicit_none:
                    self.context.gemini_keys = None
                with patch.object(QtWidgets.QMessageBox, 'information') as information, \
                     patch.object(QtWidgets.QMessageBox, 'question') as consent, \
                     patch('app_plugins.builtin.video_prompt_assistant.ui.AnalyzeThread') as worker, \
                     patch('urllib.request.urlopen') as opening:
                    self.dialog.analyze()
                information.assert_called_once()
                self.assertIn('Gemini', information.call_args.args[2])
                self.assertIn('手动', information.call_args.args[2])
                consent.assert_not_called()
                worker.assert_not_called()
                opening.assert_not_called()
                self.assertIsNone(self.dialog.worker)
                self.assertTrue(self.dialog.analyze_button.isEnabled())
                self.assertEqual(self.dialog.suggestion_list.count(), 3)

    def test_existing_manager_with_no_keys_uses_broker_guidance(self):
        self.context.gemini_keys = Mock()
        self.context.gemini_keys.request_keys.return_value = []
        self.context.gemini_keys.unavailable_message.return_value = 'synthetic unavailable keys'
        with patch.object(QtWidgets.QMessageBox, 'information') as information, \
             patch('app_plugins.builtin.video_prompt_assistant.ui.AnalyzeThread') as worker:
            self.dialog.analyze()
        self.assertEqual(information.call_args.args[2], 'synthetic unavailable keys')
        worker.assert_not_called()

    def test_valid_manager_preserves_consent_before_any_worker(self):
        self.context.gemini_keys = Mock()
        self.context.gemini_keys.request_keys.return_value = ['synthetic-first']
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No) as consent, \
             patch('app_plugins.builtin.video_prompt_assistant.ui.AnalyzeThread') as worker:
            self.dialog.analyze()
        consent.assert_called_once()
        worker.assert_not_called()
        self.assertFalse(self.dialog.cloud_approved)

    def test_missing_image_and_busy_checks_remain_before_key_lookup(self):
        self.dialog.image_jpeg = b''
        self.dialog.analyze()
        self.assertIn('请先加载', self.dialog.status.text())
        self.dialog.image_jpeg = b'synthetic-jpeg'
        with patch.object(self.dialog, 'is_busy', return_value=True):
            self.dialog.analyze()
        self.assertIn('正在分析', self.dialog.status.text())

