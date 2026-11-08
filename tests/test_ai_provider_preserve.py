"""Regression for the main settings dialog's fresh/partial config projection."""
import copy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from qt_compat import QtWidgets
from model.AIProviderKeys import KEYS_CONFIG_KEY, protect_key, read_provider_key
from model.ConfigHotReload import merge_runtime_changes
from app_plugins.builtin.ai_model_client.settings import ClientSettingsPage


class ProviderPreservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def page(self, config):
        page = ClientSettingsPage(SimpleNamespace(load_config=lambda: copy.deepcopy(config)))
        self.addCleanup(page.widget.deleteLater)
        return page

    def fixture(self):
        return {KEYS_CONFIG_KEY: {p: protect_key('synthetic-' + p + '-original')
                                 for p in ('openai', 'openrouter', 'groq', 'mistral', 'cloudflare')},
                'unrelated': {'business': 'unchanged'}}

    def test_fresh_projection_keeps_all_existing_providers_even_when_untouched(self):
        config = self.fixture(); page = self.page(config); projected = {}
        page.update_config(projected)
        self.assertEqual(projected[KEYS_CONFIG_KEY], config[KEYS_CONFIG_KEY])

    def test_adding_openrouter_to_fresh_projection_does_not_delete_gpt(self):
        config = self.fixture(); config[KEYS_CONFIG_KEY].pop('openrouter')
        page = self.page(config); page.editors['openrouter'].add_key('synthetic-new-router', '新 Router')
        projected = {}; page.update_config(projected)
        self.assertEqual(projected[KEYS_CONFIG_KEY]['openai'], config[KEYS_CONFIG_KEY]['openai'])
        self.assertEqual(read_provider_key(projected, 'openrouter'), 'synthetic-new-router')

    def test_adding_gpt_to_fresh_projection_does_not_delete_router(self):
        config = self.fixture(); config[KEYS_CONFIG_KEY].pop('openai')
        page = self.page(config); page.editors['openai'].add_key('synthetic-new-gpt', '新 GPT')
        projected = {}; page.update_config(projected)
        self.assertEqual(projected[KEYS_CONFIG_KEY]['openrouter'], config[KEYS_CONFIG_KEY]['openrouter'])

    def test_edit_and_delete_only_change_the_selected_provider(self):
        for operation in ('add', 'delete'):
            with self.subTest(operation=operation):
                config = self.fixture(); page = self.page(config); editor = page.editors['openrouter']
                if operation == 'add': editor.add_key('synthetic-router-second', '第二个')
                else: editor.remove_keys([editor.pool['items'][0]['id']])
                projected = {}; page.update_config(projected)
                for provider in ('openai', 'groq', 'cloudflare', 'mistral'):
                    self.assertEqual(projected[KEYS_CONFIG_KEY][provider], config[KEYS_CONFIG_KEY][provider])
                if operation == 'delete': self.assertNotIn('openrouter', projected[KEYS_CONFIG_KEY])
                else: self.assertEqual(len(projected[KEYS_CONFIG_KEY]['openrouter']['items']), 2)

    def test_main_settings_merge_then_reopen_both_providers_survive(self):
        disk = self.fixture()
        for sequence, provider in enumerate(('openrouter', 'openai', 'openrouter')):
            page = self.page(disk)
            baseline = {}; page.update_config(baseline)
            page.editors[provider].add_key('synthetic-' + provider + '-' + str(sequence), '追加')
            projected = {}; page.update_config(projected)
            merged, conflicts = merge_runtime_changes(baseline, projected, disk, disk)
            self.assertEqual(conflicts, [])
            self.assertIn('openai', merged[KEYS_CONFIG_KEY])
            self.assertIn('openrouter', merged[KEYS_CONFIG_KEY])
            self.assertEqual(merged['unrelated'], disk['unrelated'])
            disk = merged

    def test_external_other_provider_change_is_preserved_by_main_settings_merge(self):
        disk = self.fixture(); page = self.page(disk); baseline = {}; page.update_config(baseline)
        page.editors['openrouter'].add_key('synthetic-router-second', '第二个')
        projected = {}; page.update_config(projected)
        external = copy.deepcopy(disk); external[KEYS_CONFIG_KEY]['openai'] = protect_key('synthetic-external-gpt')
        merged, conflicts = merge_runtime_changes(baseline, projected, disk, external)
        self.assertEqual(conflicts, [])
        self.assertEqual(merged[KEYS_CONFIG_KEY]['openai'], external[KEYS_CONFIG_KEY]['openai'])

    def test_existing_unknown_provider_is_not_dropped(self):
        config = self.fixture(); config[KEYS_CONFIG_KEY]['future-provider'] = {'opaque': 'preserve'}
        page = self.page(config); projected = {}; page.update_config(projected)
        self.assertEqual(projected[KEYS_CONFIG_KEY], config[KEYS_CONFIG_KEY])

    def test_empty_untouched_settings_do_not_create_a_key_collection(self):
        page = self.page({}); projected = {}; page.update_config(projected)
        self.assertNotIn(KEYS_CONFIG_KEY, projected)

    def test_real_program_settings_save_keeps_other_keys_on_disk(self):
        from PYUI.main_setting_pyui import MainSettingDialog
        from model.AppDataStorage import data_file
        config = self.fixture(); page = self.page(config)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'; data_file(path).write_json(config)
            with patch('PYUI.main_setting_pyui.CONFIG_PATH', str(path)):
                dialog = MainSettingDialog()
                try:
                    dialog.plugin_host = SimpleNamespace(update_settings_config=page.update_config)
                    dialog._config_form_baseline = copy.deepcopy(dialog.get_config())
                    dialog._config_disk_baseline = copy.deepcopy(config)
                    page.editors['openrouter'].add_key('synthetic-added-router', '新增')
                    self.assertTrue(dialog.save_config())
                    saved = data_file(path).read_json()
                    self.assertEqual(saved[KEYS_CONFIG_KEY]['openai'], config[KEYS_CONFIG_KEY]['openai'])
                    self.assertEqual(len(saved[KEYS_CONFIG_KEY]['openrouter']['items']), 2)
                finally:
                    dialog.plugin_host = None
                    dialog.deleteLater()
