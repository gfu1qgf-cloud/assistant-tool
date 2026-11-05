"""Settings-page UI and bounded automatic model loading; no real API calls."""
import copy
import os
from unittest import TestCase
from unittest.mock import Mock, patch

from tests.test_ai_model_client import UITests, FakeContext, Response
from model.BackgroundTasks import BackgroundTaskManager
from model.AIProviderClient import AIProviderError
from app_plugins.builtin.ai_model_client.settings import ClientSettingsPage
from app_plugins.builtin.ai_model_client.catalog import openai_identity


MODELS = [{'id': name, 'name': name, 'vision': None, 'free': None}
          for name in ('gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.1-terra')]


class SettingsCatalogTests(TestCase):
    setUpClass = UITests.__dict__['setUpClass']
    setUp = UITests.setUp
    tearDown = UITests.tearDown
    wait = UITests.wait

    def select_openai(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))

    def test_program_settings_show_only_selected_service_and_keep_unsaved_fields(self):
        page = ClientSettingsPage(self.context)
        page.widget.resize(680, 540); page.widget.show(); self.app.processEvents()
        try:
            self.assertEqual(page.service.currentData(), 'openai')
            self.assertTrue(page.edits['openai'].isVisible())
            self.assertFalse(page.edits['groq'].isVisible())
            page.edits['openai'].setText('synthetic-unsaved')
            page.service.setCurrentIndex(page.service.findData('cloudflare'))
            self.app.processEvents()
            self.assertFalse(page.edits['openai'].isVisible())
            self.assertTrue(page.account.isVisible())
            self.assertTrue(page.cf_free_only.isVisible())
            self.assertFalse(page.free_only.isVisible())
            page.service.setCurrentIndex(page.service.findData('openai'))
            self.assertEqual(page.edits['openai'].text(), 'synthetic-unsaved')
            page.service.setCurrentIndex(page.service.findData('gemini'))
            self.assertTrue(page.gemini_button.isVisible())
            self.assertTrue(all(not edit.isVisible() for edit in page.edits.values()))
        finally:
            page.widget.close(); page.widget.deleteLater()

    def test_available_credentials_auto_load_multiple_models_once_then_manual_refresh(self):
        self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-first'}
        with patch.object(self.dialog.client, 'list_models', return_value=MODELS) as query:
            self.select_openai()
            self.wait(lambda: self.dialog.handle is None and 'openai' in self.dialog.catalogs)
            self.assertEqual(self.dialog.model.count(), 3)
            self.assertIn('3 个', self.dialog.catalog_hint.text())
            self.assertEqual(query.call_count, 1)
            self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('groq'))
            self.select_openai(); self.app.processEvents()
            self.assertEqual(query.call_count, 1)
            self.dialog.refresh_models()
            self.wait(lambda: self.dialog.handle is None)
            self.assertEqual(query.call_count, 2)

    def test_model_list_failure_shows_reason_never_auto_loops_and_does_not_block_chat(self):
        self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-first'}
        with patch.object(self.dialog.client, 'list_models', side_effect=AIProviderError('HTTP 403：模型列表权限不足')) as query:
            self.select_openai()
            self.wait(lambda: self.dialog.handle is None and 'openai' in self.dialog.catalog_errors)
            self.assertIn('403', self.dialog.catalog_hint.text())
            self.assertIn('直接填写模型聊天', self.dialog.catalog_hint.text())
            self.assertTrue(self.dialog.send_button.isEnabled())
            for _ in range(3):
                self.dialog.ensure_openai_catalog(); self.app.processEvents()
            self.assertEqual(query.call_count, 1)
            self.assertEqual(self.dialog.model_id(), 'gpt-6-luna')

    def test_main_settings_save_new_key_triggers_reload_and_does_not_reuse_old_catalog(self):
        self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-first'}
        with patch.object(self.dialog.client, 'list_models', side_effect=[MODELS, MODELS[:1]]) as query:
            self.select_openai()
            self.wait(lambda: self.dialog.handle is None and 'openai' in self.dialog.catalogs)
            self.context.config['ai_provider_keys']['openai'] = 'dpapi:synthetic-second'
            self.dialog.settings_changed()
            self.wait(lambda: query.call_count == 2 and self.dialog.handle is None)
            self.assertEqual(self.dialog.model.count(), 1)
            self.assertEqual(self.dialog.catalog_identities['openai'], openai_identity(self.context.config))

    def test_switching_back_to_previous_key_reloads_and_removal_clears_old_catalog(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(self.dialog.client, 'list_models', return_value=MODELS) as query:
            self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-first'}
            self.select_openai()
            self.wait(lambda: self.dialog.handle is None and query.call_count == 1)
            for index, key in enumerate(('dpapi:synthetic-second', 'dpapi:synthetic-first'), start=2):
                self.context.config['ai_provider_keys']['openai'] = key
                self.dialog.settings_changed()
                self.wait(lambda: self.dialog.handle is None and query.call_count == index)
                self.assertEqual(self.dialog.model.count(), 3)
            self.context.config['ai_provider_keys'].clear()
            self.dialog.settings_changed(); self.app.processEvents()
            self.assertEqual(self.dialog.model.count(), 0)
            self.assertNotIn('openai', self.dialog.catalogs)
            self.assertEqual(query.call_count, 3)

    def test_no_key_no_automatic_request_and_default_not_described_as_account_list(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(self.dialog.client, 'list_models') as query:
            self.select_openai(); self.app.processEvents()
            query.assert_not_called()
            self.assertIn('尚未读取', self.dialog.catalog_hint.text())
            self.assertEqual(self.dialog.model.count(), 0)

    def test_old_credential_catalog_completion_is_discarded(self):
        handle = object()
        self.dialog.handle = handle
        self.dialog.operation = {'type': 'models', 'provider': 'openai', 'identity': 'obsolete'}
        self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-new'}
        try:
            self.dialog.completed(handle, {'provider': 'openai', 'models': MODELS})
            self.assertNotIn('openai', self.dialog.catalogs)
            self.assertIn('忽略旧密钥', self.dialog.last_status)
        finally:
            self.dialog.handle = self.dialog.operation = None

    def test_empty_list_remains_empty_and_preserves_explicit_model(self):
        self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-first'}
        with patch.object(self.dialog.client, 'list_models', return_value=[]):
            self.select_openai()
            self.dialog.model.setCurrentText('gpt-6.1-sol')
            self.wait(lambda: self.dialog.handle is None and 'openai' in self.dialog.catalogs)
            self.assertEqual(self.dialog.model.count(), 0)
            self.assertEqual(self.dialog.model_id(), 'gpt-6.1-sol')
            self.assertIn('未返回', self.dialog.catalog_hint.text())

    def test_switch_model_reaches_official_http_payload_and_records_reported_model(self):
        self.context.config['ai_provider_keys'] = {'openai': 'dpapi:synthetic-first'}
        sent = []
        def transport(method, url, **kwargs):
            self.assertEqual(url, 'https://api.openai.com/v1/responses')
            model = kwargs['json']['model']; sent.append(model)
            return Response({'status':'completed', 'model':model+'-fixture-snapshot',
                'output':[{'type':'message','content':[{'type':'output_text','text':'模拟回答'}]}],
                'usage':{'input_tokens':10,'output_tokens':3}})
        with patch.object(self.dialog.client, 'list_models', return_value=MODELS), \
             patch.object(self.dialog.client, '_key', return_value='synthetic-key'), \
             patch('model.AIProviderClient.requests.request', side_effect=transport):
            self.select_openai()
            self.wait(lambda: self.dialog.handle is None and 'openai' in self.dialog.catalogs)
            self.dialog.streaming.setChecked(False)
            for model in ('gpt-6.1-sol', 'gpt-6.1-terra'):
                self.dialog.model.setCurrentIndex(next(i for i in range(self.dialog.model.count())
                    if self.dialog.model.itemData(i)['id'] == model))
                self.dialog.input.setPlainText('模型切换测试')
                self.dialog.send()
                self.wait(lambda: self.dialog.handle is None)
                messages = self.store.messages(self.dialog.current_id)
                self.assertEqual(messages[-2]['model'],model)
                self.assertEqual(messages[-1]['metadata']['requested_model'],model)
                self.assertEqual(messages[-1]['model'],model+'-fixture-snapshot')
                self.assertIn('接口返回：'+model+'-fixture-snapshot',self.dialog.status.text())
            self.assertEqual(sent,['gpt-6.1-sol','gpt-6.1-terra'])
            self.assertIn('请求模型：gpt-6.1-terra', self.dialog.transcript.toPlainText())
