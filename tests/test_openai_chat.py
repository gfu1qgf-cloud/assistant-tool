import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_ai_model_client import Job, Response, FakeContext
from tests.test_ai_client_stream import SSEResponse
from model.AIProviderClient import AIProviderClient, AIProviderError, validate_model
from model.AIProviderKeys import read_provider_key, protect_key
from model.AIProviderStream import ChatStream
from model.BackgroundTasks import BackgroundTaskManager
from app_plugins.builtin.ai_model_client.settings import ClientSettingsPage
from app_plugins.builtin.ai_model_client.store import ChatStore
from app_plugins.builtin.ai_model_client.ui import AIClientDialog
from qt_compat import QtWidgets


def reply(text='你好', status='completed', reason=None):
    return {'status': status, 'model': 'gpt-6-luna',
            'output': [{'type': 'reasoning', 'summary': []}, {'type': 'message',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'incomplete_details': {'reason': reason} if reason else None,
            'usage': {'input_tokens': 12, 'output_tokens': 34}}


class OpenAIChatTests(unittest.TestCase):
    def setUp(self):
        self.client = AIProviderClient()
        self.job = Job()
        self.messages = [{'role': 'user', 'text': '你好', 'images': [{'mime': 'image/png', 'data': 'YWJj'}]},
                         {'role': 'assistant', 'text': 'Hello', 'images': []},
                         {'role': 'user', 'text': '继续', 'images': []}]
        self.patches = [patch('model.AIProviderClient.require_online'),
                        patch.object(self.client, '_key', return_value='synthetic-openai-test-key')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def chat(self, response, **options):
        with patch('model.AIProviderClient.requests.request', return_value=response) as request:
            result = self.client.chat('openai', 'gpt-6-luna', self.messages,
                                      {'system': '规则', 'max_tokens': 1024, **options}, {}, self.job)
        self.assertTrue(response.closed)
        self.assertEqual(request.call_count, 1)
        return result, request.call_args

    def test_official_responses_multiturn_images_and_usage(self):
        with patch.object(self.client, 'list_models', side_effect=AssertionError('no preflight')):
            result, call = self.chat(Response(reply()))
        self.assertEqual(call.args[1], 'https://api.openai.com/v1/responses')
        self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer synthetic-openai-test-key')
        self.assertFalse(call.kwargs['allow_redirects'])
        payload = call.kwargs['json']
        self.assertFalse(payload['store'])
        self.assertNotIn('tools', payload)
        self.assertNotIn('max_tokens', payload)
        self.assertEqual(payload['instructions'], '规则')
        self.assertEqual(payload['max_output_tokens'], 1024)
        self.assertEqual(payload['input'][0]['content'][1]['image_url'], 'data:image/png;base64,YWJj')
        self.assertEqual(payload['input'][1]['content'][0]['type'], 'output_text')
        self.assertEqual(result['text'], '你好')
        self.assertEqual(result['usage'], {'input': 12, 'output': 34})
        self.assertEqual(result['requested_model'],'gpt-6-luna')
        self.assertEqual(result['reported_model'],'gpt-6-luna')

    def test_stream_deltas_no_duplicate_final_and_terminal_without_done(self):
        result, _ = self.chat(SSEResponse([
            {'type': 'response.output_text.delta', 'delta': '你'},
            {'type': 'response.output_text.delta', 'delta': '好'},
            {'type': 'response.completed', 'response': reply()},
        ]), stream=True)
        self.assertEqual(result['text'], '你好')
        self.assertEqual(result['usage']['output'], 34)
        self.assertEqual(result['reported_model'],'gpt-6-luna')

    def test_missing_reported_model_is_not_faked_from_requested_model(self):
        for streaming in (False, True):
            data = reply(); data.pop('model')
            response = SSEResponse([{'type':'response.completed','response':data}]) if streaming else Response(data)
            result, _ = self.chat(response, stream=streaming)
            self.assertEqual(result['requested_model'],'gpt-6-luna')
            self.assertEqual(result['reported_model'],'')

    def test_complete_only_stream_and_incomplete_warning(self):
        for stream in (False, True):
            data = reply('部分文字', 'incomplete', 'max_output_tokens')
            response = SSEResponse([{'type': 'response.incomplete', 'response': data}]) if stream else Response(data)
            result, _ = self.chat(response, stream=stream)
            self.assertEqual(result['text'], '部分文字')
            self.assertIn('输出上限', result['warning'])

    def test_missing_terminal_and_failure_are_not_success(self):
        for events in ([{'type': 'response.output_text.delta', 'delta': '部分'}],
                       [{'type': 'response.output_text.delta', 'delta': '部分'}, '[DONE]'],
                       [{'type': 'response.failed', 'response': {'status': 'failed'}}],
                       [{'type': 'response.completed', 'response': reply('', 'in_progress')}],
                       [{'type': 'response.refusal.done', 'refusal': '不支持'}]):
            with self.subTest(events=events), self.assertRaises(AIProviderError):
                self.chat(SSEResponse(events), stream=True)

    def test_no_text_and_refusal_not_success(self):
        for data in (reply(''), {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'refusal', 'refusal': '不支持'}]}]}):
            with self.assertRaises(AIProviderError):
                self.chat(Response(data))

    def test_http_failures_never_resubmit(self):
        for status in (401, 403, 429, 500):
            self.client._cooldowns.clear()
            with self.subTest(status=status), self.assertRaises(AIProviderError):
                self.chat(Response({'error': {'message': 'synthetic error'}}, status))

    def test_catalog_filters_non_chat_and_does_not_claim_free_or_vision(self):
        response = Response({'data': [{'id': name} for name in (
            'gpt-6-luna', 'gpt-6.1-sol', 'gpt-5.3-codex', 'gpt-image-1',
            'gpt-realtime', 'tts-1', 'text-embedding-3-small')]})
        with patch('model.AIProviderClient.requests.request', return_value=response) as request:
            rows = self.client.list_models('openai', {}, self.job)
        self.assertEqual(request.call_args.args[1], 'https://api.openai.com/v1/models')
        self.assertEqual({row['id'] for row in rows}, {'gpt-6-luna', 'gpt-6.1-sol'})
        self.assertTrue(all(row['free'] is None and row['vision'] is None for row in rows))

    def test_non_official_origin_blocked_before_key_transmission(self):
        with patch('model.AIProviderClient.requests.request') as request:
            for url in ('https://evil.example/v1/responses', 'http://api.openai.com/v1/responses',
                        'https://api.openai.com:123/v1/responses'):
                with self.assertRaises(ValueError):
                    self.client._request('openai', 'synthetic', '', url, self.job)
            request.assert_not_called()

    def test_environment_and_dpapi_key_roundtrip(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-only-key'}):
            self.assertEqual(read_provider_key({}, 'openai'), 'synthetic-only-key')
        encrypted = protect_key('synthetic-only-key')
        self.assertNotIn('synthetic-only-key', encrypted)
        self.assertEqual(read_provider_key({'ai_provider_keys': {'openai': encrypted}}, 'openai'), 'synthetic-only-key')

    def test_no_codex_or_non_chat_route(self):
        for model in ('gpt-5.3-codex', 'gpt-image-1', 'gpt-realtime', 'tts-1'):
            with self.assertRaises(ValueError):
                validate_model('openai', model)

    def test_settings_preserve_other_keys_and_encrypt_new_openai(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        context = FakeContext(BackgroundTaskManager())
        page = ClientSettingsPage(context)
        config = {'ai_provider_keys': {'mistral': 'existing-encrypted-value'}}
        page.editors['openai'].add_key('synthetic-only-key','测试Key')
        page.update_config(config)
        self.assertEqual(config['ai_provider_keys']['mistral'], 'existing-encrypted-value')
        self.assertTrue(config['ai_provider_keys']['openai']['items'][0]['encrypted'].startswith('dpapi:'))
        page.widget.close()

    def test_dialog_openai_selector_default_and_history_preserved(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        manager = BackgroundTaskManager()
        context = FakeContext(manager)
        with tempfile.TemporaryDirectory() as folder:
            store = ChatStore(Path(folder)/'history')
            dialog = AIClientDialog(context, store)
            try:
                dialog.provider.setCurrentIndex(dialog.provider.findData('openai'))
                self.assertEqual(dialog.model_id(), 'gpt-6-luna')
                self.assertIn('按用量计费', dialog.billing_hint.text())
                dialog.input.setPlainText('保留我的草稿')
                dialog.save_draft()
                row = store.get(dialog.current_id)
                self.assertEqual(row['provider'], 'openai')
                self.assertEqual(row['draft'], '保留我的草稿')
                self.assertEqual(context.config['gemini_api_keys'], ['synthetic-legacy-keep'])
            finally:
                dialog.close()
                dialog.deleteLater()
                manager.shutdown()
                manager.deleteLater()
                app.processEvents()


if __name__ == '__main__':
    unittest.main()
